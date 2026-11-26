class BookScoutError(Exception):
    code = "bookscout_error"

    def __init__(self, message, *, field=None, details=None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details or {}


class InvalidTransition(BookScoutError):
    code = "job_invalid_transition"


class UnknownOperation(BookScoutError):
    code = "unknown_operation"


class ValidationError(BookScoutError):
    code = "validation_error"


class ConcurrentToolExecution(BookScoutError):
    code = "concurrent_tool_execution"


class ToolContractViolation(BookScoutError):
    code = "tool_contract_violation"


class JobNotFound(BookScoutError):
    code = "job_not_found"


class IdempotencyConflict(BookScoutError):
    code = "idempotency_key_reused"
