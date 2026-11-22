from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Event, RLock
from uuid import uuid4

from .errors import ConcurrentToolExecution, InvalidTransition


class JobState(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    RESOLVING_BOOK = "RESOLVING_BOOK"
    FETCHING_OFFERS = "FETCHING_OFFERS"
    NORMALIZING = "NORMALIZING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    NO_RESULTS = "NO_RESULTS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_STATES = {
    JobState.SUCCEEDED, JobState.PARTIAL, JobState.NO_RESULTS,
    JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT,
}

ALLOWED_TRANSITIONS = {
    JobState.CREATED: {JobState.VALIDATING, JobState.CANCELLED},
    JobState.VALIDATING: {JobState.RESOLVING_BOOK, JobState.FAILED, JobState.CANCELLED},
    JobState.RESOLVING_BOOK: {JobState.FETCHING_OFFERS, JobState.FAILED, JobState.CANCELLED},
    JobState.FETCHING_OFFERS: {JobState.NORMALIZING, JobState.TIMED_OUT, JobState.CANCELLED},
    JobState.NORMALIZING: {JobState.SUCCEEDED, JobState.PARTIAL, JobState.NO_RESULTS, JobState.FAILED},
    JobState.SUCCEEDED: set(), JobState.PARTIAL: set(), JobState.NO_RESULTS: set(),
    JobState.FAILED: set(), JobState.CANCELLED: set(), JobState.TIMED_OUT: set(),
}


def utc_now():
    return datetime.now(timezone.utc)


@dataclass
class Job:
    request_id: str
    id: str = field(default_factory=lambda: str(uuid4()))
    state: JobState = JobState.CREATED
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    cancellation_token: Event = field(default_factory=Event, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _tool_running: bool = field(default=False, init=False, repr=False)

    def transition(self, target, *, result=None, error_code=None, error_message=None):
        """Apply a legal transition or raise an explicit domain error."""
        with self._lock:
            allowed = ALLOWED_TRANSITIONS[self.state]
            if target not in allowed:
                raise InvalidTransition(
                    f"Переход {self.state.value} -> {target.value} запрещён",
                    field="target_state",
                    details={
                        "job_id": self.id,
                        "current_state": self.state.value,
                        "requested_state": target.value,
                        "allowed_next_states": sorted(s.value for s in allowed),
                    },
                )
            self.state = target
            if result is not None:
                self.result = result
            if error_code is not None:
                self.error_code = error_code
                self.error_message = error_message
            self.updated_at = utc_now()

    def cancel(self):
        with self._lock:
            if self.state == JobState.CANCELLED:
                return
            if self.state not in {
                JobState.CREATED, JobState.VALIDATING,
                JobState.RESOLVING_BOOK, JobState.FETCHING_OFFERS,
            }:
                raise InvalidTransition(
                    f"Нельзя отменить Job в состоянии {self.state.value}",
                    field="target_state",
                    details={"job_id": self.id, "current_state": self.state.value},
                )
            self.cancellation_token.set()
            self.transition(JobState.CANCELLED)

    @contextmanager
    def tool_execution(self):
        """At most one Tool may be active for a Job at any moment."""
        with self._lock:
            if self._tool_running:
                raise ConcurrentToolExecution(
                    "Для одной Job уже выполняется другой Tool",
                    details={"job_id": self.id},
                )
            if self.state in TERMINAL_STATES:
                raise InvalidTransition("Нельзя запускать Tool для завершённой Job")
            self._tool_running = True
        try:
            yield
        finally:
            with self._lock:
                self._tool_running = False

    def recover_after_restart(self):
        """Explicit recovery-only transition for persisted in-flight jobs."""
        with self._lock:
            if self.state in TERMINAL_STATES:
                return
            self.cancellation_token.set()
            self.state = JobState.FAILED
            self.error_code = "APP_RESTARTED"
            self.error_message = "Приложение завершилось до окончания Job"
            self.updated_at = utc_now()
