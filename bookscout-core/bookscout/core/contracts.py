from dataclasses import dataclass, field as dc_field
from datetime import datetime
from threading import Event
from typing import Any, Generic, Protocol, TypeVar

I = TypeVar("I")
O = TypeVar("O")


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    field: str | None = None
    retryable: bool = False
    details: dict[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult(Generic[O]):
    value: O | None = None
    error: ToolError | None = None

    def __post_init__(self):
        if (self.value is None) == (self.error is None):
            raise ValueError("ToolResult должен содержать ровно value или error")


@dataclass(frozen=True)
class ToolContext:
    job_id: str
    request_id: str
    deadline: datetime | None
    cancellation_token: Event


class Tool(Protocol):
    name: str
    version: str
    input_schema: dict[str, Any]

    def execute(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult[Any]: ...
