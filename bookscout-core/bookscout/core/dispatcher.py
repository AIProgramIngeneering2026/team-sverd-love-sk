from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .contracts import ToolContext, ToolError, ToolResult
from .errors import BookScoutError, InvalidTransition, JobNotFound, ToolContractViolation
from .job import Job, JobState, TERMINAL_STATES
from .registry import ToolRegistry
from .repository import JobRepository


class Dispatcher:
    """Runs configured workflows; concrete Tool classes are unknown here."""
    def __init__(self, registry: ToolRegistry, jobs: JobRepository, *,
                 deadline_seconds: int = 15, recover_interrupted: bool = True):
        self.registry = registry
        self.jobs = jobs
        self.deadline_seconds = deadline_seconds
        if recover_interrupted:
            for job in self.jobs.list_all():
                previous_state = job.state
                job.recover_after_restart()
                if job.state != previous_state:
                    self.jobs.save(job)

    def dispatch(self, operation: str, payload: dict[str, Any], *,
                 request_id: str | None = None, idempotency_key: str | None = None,
                 request_hash: str | None = None) -> Job:
        request_id = request_id or str(uuid4())
        if idempotency_key is not None:
            existing = self.jobs.get_by_idempotency_key(idempotency_key, request_hash or "")
            if existing is not None:
                return existing
        steps = self.registry.get_workflow(operation)

        # Validate public input before creating/persisting a Job.
        first_input = steps[0].input_factory(dict(payload))
        self.registry.validate_input(steps[0].operation, first_input)

        job = Job(request_id=request_id)
        job, created = self.jobs.create_or_get(job, idempotency_key, request_hash)
        if not created:
            return job
        job.transition(steps[0].state)
        self.jobs.save(job)
        context = ToolContext(
            job_id=job.id,
            request_id=request_id,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.deadline_seconds),
            cancellation_token=job.cancellation_token,
        )
        data: dict[str, Any] = dict(payload)

        for step in steps:
            if job.state == JobState.CANCELLED:
                return job
            if job.state != step.state:
                raise InvalidTransition(
                    "Workflow и состояние Job рассинхронизированы",
                    details={"job_id": job.id, "expected": step.state.value,
                             "actual": job.state.value},
                )
            tool = self.registry.get(step.operation)
            tool_input = step.input_factory(data)
            try:
                self.registry.validate_input(step.operation, tool_input)
                with job.tool_execution():
                    if job.cancellation_token.is_set():
                        return job
                    result = tool.execute(tool_input, context)
                if not isinstance(result, ToolResult):
                    raise ToolContractViolation(
                        f"{tool.name} вернул объект не типа ToolResult"
                    )
            except BookScoutError as exc:
                result = ToolResult(error=ToolError(
                    code=exc.code, message=exc.message, field=exc.field,
                    details=exc.details,
                ))
            except Exception as exc:
                result = ToolResult(error=ToolError(
                    code="INTERNAL_ERROR",
                    message="Внутренняя ошибка инструмента",
                ))

            # Cancellation or another terminal outcome wins over a late Tool response.
            if job.state == JobState.CANCELLED or job.state in TERMINAL_STATES:
                return job

            if result.error is not None:
                error = result.error
                if error.code == "STORE_TIMEOUT" and step.timeout_state is not None:
                    job.transition(step.timeout_state, error_code=error.code,
                                   error_message=error.message)
                    self.jobs.save(job)
                else:
                    job.transition(step.failure_state, error_code=error.code,
                                   error_message=error.message)
                    self.jobs.save(job)
                    if step.failure_state == JobState.NORMALIZING:
                        job.transition(JobState.FAILED, error_code=error.code,
                                       error_message=error.message)
                        self.jobs.save(job)
                return job

            data[step.operation] = result.value
            if job.state == JobState.FETCHING_OFFERS:
                fetched = result.value
                sources = fetched.get("source_results", [])
                offers = fetched.get("offers", [])
                if (not offers and sources and all(
                    item.get("error_code") == "STORE_TIMEOUT" for item in sources
                ) and step.timeout_state is not None):
                    job.transition(step.timeout_state, error_code="STORE_TIMEOUT",
                                   error_message="Все источники превысили timeout")
                    self.jobs.save(job)
                    return job
            if step.success_state is not None:
                job.transition(step.success_state)
                self.jobs.save(job)

        normalized = data[steps[-1].operation]
        outcome = normalized.get("job_outcome", "SUCCEEDED")
        terminal = JobState(outcome)
        if terminal not in {JobState.SUCCEEDED, JobState.PARTIAL,
                            JobState.NO_RESULTS, JobState.FAILED}:
            raise ToolContractViolation(f"Недопустимый итог Job: {outcome}")
        failure = normalized.get("failure") or {}
        job.transition(terminal, result=normalized,
                       error_code=failure.get("code"),
                       error_message=failure.get("message"))
        self.jobs.save(job)
        return job

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFound("Job не найдена", field="job_id")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        job.cancel()
        self.jobs.save(job)
        return job
