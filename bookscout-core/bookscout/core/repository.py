from typing import Protocol
from .job import Job


class JobRepository(Protocol):
    def add(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job | None: ...
    def list_all(self) -> list[Job]: ...


class InMemoryJobRepository:
    """In-memory adapter; replace with SQLite without changing the core."""
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def add(self, job: Job):
        if job.id in self._jobs:
            raise ValueError(f"Job {job.id} already exists")
        self._jobs[job.id] = job

    def get(self, job_id: str):
        return self._jobs.get(job_id)

    def list_all(self):
        return list(self._jobs.values())
