from dataclasses import dataclass
from typing import Any, Callable
from .errors import UnknownOperation, ValidationError
from .contracts import Tool
from .job import JobState


@dataclass(frozen=True)
class PipelineStep:
    operation: str
    state: JobState
    input_factory: Callable[[dict[str, Any]], dict[str, Any]]
    success_state: JobState | None
    failure_state: JobState = JobState.FAILED
    timeout_state: JobState | None = None


class ToolRegistry:
    """Resolves operation names to Tools and stores workflow configuration."""
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._workflows: dict[str, tuple[PipelineStep, ...]] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, operation: str) -> Tool:
        try:
            return self._tools[operation]
        except KeyError:
            raise UnknownOperation(
                f"Неизвестная операция: {operation}", field="operation",
                details={"operation": operation, "allowed_operations": sorted(self._tools)},
            ) from None

    def register_workflow(self, name: str, steps: list[PipelineStep]):
        if not steps:
            raise ValueError("Workflow must contain at least one step")
        if name in self._workflows:
            raise ValueError(f"Workflow already registered: {name}")
        for step in steps:
            self.get(step.operation)
        self._workflows[name] = tuple(steps)

    def get_workflow(self, name: str) -> tuple[PipelineStep, ...]:
        try:
            return self._workflows[name]
        except KeyError:
            raise UnknownOperation(
                f"Неизвестная операция: {name}", field="operation",
                details={"operation": name, "allowed_operations": sorted(self._workflows)},
            ) from None

    def validate_input(self, operation: str, payload: dict[str, Any]):
        """Validate the small JSON Schema subset used by the current MVP."""
        schema = self.get(operation).input_schema
        if not isinstance(payload, dict):
            raise ValidationError("Ожидался JSON-объект", field="$" )
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in payload:
                raise ValidationError(f"Отсутствует обязательное поле: {key}", field=key,
                                      details={"rule": "required"})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(payload) - set(properties))
            if extras:
                raise ValidationError(f"Неизвестные поля: {', '.join(extras)}", field=extras[0],
                                      details={"rule": "additionalProperties"})
        types = {"string": str, "integer": int, "number": (int, float),
                 "boolean": bool, "object": dict, "array": list}
        for key, value in payload.items():
            spec = properties.get(key)
            if spec is None:
                continue
            expected = spec.get("type")
            expected_type = types.get(expected)
            wrong_type = expected_type and not isinstance(value, expected_type)
            if expected == "integer" and isinstance(value, bool):
                wrong_type = True
            if wrong_type:
                raise ValidationError(f"Поле {key} должно иметь тип {expected}", field=key,
                                      details={"rule": "type", "expected": expected,
                                               "actual": type(value).__name__})
            max_length = spec.get("maxLength")
            if isinstance(value, str) and max_length is not None and len(value) > max_length:
                raise ValidationError(f"Поле {key} превышает допустимую длину", field=key,
                                      details={"rule": "maxLength", "expected": max_length,
                                               "actual": len(value)})
