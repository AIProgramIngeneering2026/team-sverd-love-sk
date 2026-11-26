from typing import Protocol
from .errors import IdempotencyConflict
from .job import Job


class JobRepository(Protocol):
    def add(self, job: Job) -> None: ...
    def create_or_get(self, job: Job, idempotency_key: str | None, request_hash: str | None) -> tuple[Job, bool]: ...
    def get_by_idempotency_key(self, key: str, request_hash: str) -> Job | None: ...
    def get(self, job_id: str) -> Job | None: ...
    def list_all(self) -> list[Job]: ...
    def save(self, job: Job) -> None: ...


class InMemoryJobRepository:
    """In-memory adapter for fast isolated unit tests."""
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._keys: dict[str, tuple[str, str]] = {}

    def add(self, job: Job):
        if job.id in self._jobs:
            raise ValueError(f"Job {job.id} already exists")
        self._jobs[job.id] = job

    def create_or_get(self, job: Job, idempotency_key: str | None, request_hash: str | None):
        if idempotency_key is not None and idempotency_key in self._keys:
            old_hash, old_job_id = self._keys[idempotency_key]
            if old_hash != request_hash:
                raise IdempotencyConflict(
                    "Idempotency-Key уже использован с другим телом запроса",
                    field="Idempotency-Key",
                    details={"idempotency_key": idempotency_key},
                )
            return self._jobs[old_job_id], False
        self.add(job)
        if idempotency_key is not None:
            self._keys[idempotency_key] = (request_hash or "", job.id)
        return job, True

    def get_by_idempotency_key(self, key: str, request_hash: str):
        entry = self._keys.get(key)
        if entry is None:
            return None
        old_hash, old_job_id = entry
        if old_hash != request_hash:
            raise IdempotencyConflict(
                "Idempotency-Key уже использован с другим телом запроса",
                field="Idempotency-Key", details={"idempotency_key": key},
            )
        return self._jobs[old_job_id]

    def get(self, job_id: str):
        return self._jobs.get(job_id)

    def list_all(self):
        return list(self._jobs.values())

    def save(self, job: Job):
        if job.id not in self._jobs:
            raise KeyError(job.id)
        self._jobs[job.id] = job
