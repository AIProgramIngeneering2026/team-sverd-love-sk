from threading import Event, Thread
import pytest

from bookscout.app import build_dispatcher
from bookscout.core.contracts import ToolContext
from bookscout.core.dispatcher import Dispatcher
from bookscout.core.errors import ConcurrentToolExecution, InvalidTransition, UnknownOperation, ValidationError
from bookscout.core.job import Job, JobState
from bookscout.core.registry import ToolRegistry
from bookscout.core.repository import InMemoryJobRepository
from bookscout.tools.builtin import ValidateIsbnTool

VALID_ISBN = "9785170900008"


def test_happy_path_from_created_to_succeeded():
    dispatcher = build_dispatcher()
    job = dispatcher.dispatch("books.search", {"isbn": VALID_ISBN})
    assert job.state is JobState.SUCCEEDED
    assert job.result["offers"]
    assert job.result["offers"][0]["currency"] == "RUB"


# Z1 — a Job cannot run two Tools at once.
def test_z1_rejects_concurrent_tool_execution():
    job = Job(request_id="z1")
    entered, release = Event(), Event()
    errors = []

    def first():
        with job.tool_execution():
            entered.set()
            release.wait(timeout=2)

    def second():
        entered.wait(timeout=2)
        try:
            with job.tool_execution():
                pass
        except Exception as exc:
            errors.append(exc)
        finally:
            release.set()

    a, b = Thread(target=first), Thread(target=second)
    a.start(); b.start(); a.join(timeout=3); b.join(timeout=3)
    assert len(errors) == 1 and isinstance(errors[0], ConcurrentToolExecution)


# Z2 — recovery converts persisted in-flight work into FAILED / APP_RESTARTED.
def test_z2_recovery_marks_interrupted_job_failed():
    repo = InMemoryJobRepository()
    job = Job(request_id="z2")
    repo.add(job)
    job.transition(JobState.VALIDATING)
    Dispatcher(ToolRegistry(), repo, recover_interrupted=True)
    assert job.state is JobState.FAILED
    assert job.error_code == "APP_RESTARTED"


# Z3 — cancellation is terminal; a late success cannot resurrect the Job.
def test_z3_cancelled_job_cannot_become_succeeded():
    job = Job(request_id="z3")
    job.transition(JobState.VALIDATING)
    job.cancel()
    with pytest.raises(InvalidTransition):
        job.transition(JobState.SUCCEEDED)
    assert job.state is JobState.CANCELLED


# Z4 — FAILED cannot later become SUCCEEDED without a new client command.
def test_z4_failed_job_cannot_have_second_terminal_outcome():
    job = Job(request_id="z4")
    job.transition(JobState.VALIDATING)
    job.transition(JobState.FAILED, error_code="TEST_FAILURE")
    with pytest.raises(InvalidTransition):
        job.transition(JobState.SUCCEEDED)
    assert job.state is JobState.FAILED


# Z5 — SUCCEEDED is reachable only after NORMALIZING.
def test_z5_success_without_work_is_rejected():
    job = Job(request_id="z5")
    with pytest.raises(InvalidTransition):
        job.transition(JobState.SUCCEEDED)
    assert job.state is JobState.CREATED


# Z6 — terminal successful Job cannot be executed again.
def test_z6_successful_job_cannot_restart():
    job = Job(request_id="z6")
    for state in (JobState.VALIDATING, JobState.RESOLVING_BOOK,
                  JobState.FETCHING_OFFERS, JobState.NORMALIZING,
                  JobState.SUCCEEDED):
        job.transition(state)
    with pytest.raises(InvalidTransition):
        job.transition(JobState.VALIDATING)
    assert job.state is JobState.SUCCEEDED


def test_unknown_operation_is_explicit_error():
    with pytest.raises(UnknownOperation) as exc:
        build_dispatcher().dispatch("books.unknown", {"isbn": VALID_ISBN})
    assert exc.value.code == "unknown_operation"


def test_invalid_input_returns_field_and_does_not_create_job():
    dispatcher = build_dispatcher()
    with pytest.raises(ValidationError) as exc:
        dispatcher.dispatch("books.search", {"isbn": 123})
    assert exc.value.field == "isbn"
    assert dispatcher.jobs.list_all() == []


def test_isbn_checksum_is_checked_inside_tool():
    tool = ValidateIsbnTool()
    context = ToolContext("job", "request", None, Event())
    result = tool.execute({"isbn": "9785170900001"}, context)
    assert result.error.code == "ISBN_CHECKSUM_INVALID"
    assert result.error.field == "isbn"


def test_registry_resolves_tool_by_name():
    registry = ToolRegistry()
    tool = ValidateIsbnTool()
    registry.register(tool)
    assert registry.get("validate_isbn") is tool


def test_missing_required_parameter_is_rejected_before_job_creation():
    dispatcher = build_dispatcher()
    with pytest.raises(ValidationError) as exc:
        dispatcher.dispatch("books.search", {})
    assert exc.value.field == "isbn"
    assert dispatcher.jobs.list_all() == []
