"""Python function tools for the public ResearchHarness embedding API."""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable, Sequence as AbcSequence
from types import UnionType
from typing import Any, Literal, Sequence, Union, get_args, get_origin, get_type_hints

from agent_base.tools.tooling import ToolBase


TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
CONTEXT_PARAMETER_NAMES = frozenset({"workspace_root", "runtime_deadline", "model_name"})


class FunctionTool(ToolBase):
    """ToolBase adapter for a validated Python function."""

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        timeout_seconds: float | int | None = None,
    ):
        self.func = func
        self._context_parameters: set[str] = set()
        self.timeout_seconds = _validate_timeout_seconds(timeout_seconds)
        self.name = _resolve_tool_name(func, name)
        self.description = _resolve_tool_description(func, description)
        self.parameters = _schema_from_signature(func, self._context_parameters)
        super().__init__()

    def call(self, params: str | dict[str, Any], **kwargs: Any) -> Any:
        parsed = self.parse_json_args(params)
        call_kwargs = dict(parsed)
        runtime_deadline = kwargs.get("runtime_deadline")
        if self.timeout_seconds is not None:
            tool_deadline = time.time() + self.timeout_seconds
            runtime_deadline = min(float(runtime_deadline), tool_deadline) if runtime_deadline is not None else tool_deadline
            if runtime_deadline <= time.time():
                return f"[{self.name}] Timeout before tool execution could start."
        for name in self._context_parameters:
            if name in kwargs:
                call_kwargs[name] = runtime_deadline if name == "runtime_deadline" else kwargs[name]
        return self.func(**call_kwargs)


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    timeout_seconds: float | int | None = None,
) -> Callable[..., Any]:
    """Mark a Python function as a ResearchHarness custom tool.

    The decorated function remains directly callable. ResearchHarness converts it
    into a ToolBase instance when passed to create_agent(tools=[...]).
    """

    def decorate(inner: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(inner):
            raise TypeError("@tool can only decorate a callable.")
        validated_timeout = _validate_timeout_seconds(timeout_seconds)
        setattr(
            inner,
            "__researchharness_tool__",
            {"name": name, "description": description, "timeout_seconds": validated_timeout},
        )
        return inner

    if func is None:
        return decorate
    return decorate(func)


def build_custom_tool_map(custom_tools: Sequence[Any] | None) -> dict[str, ToolBase]:
    """Validate and instantiate user-provided custom tools."""

    resolved: dict[str, ToolBase] = {}
    for item in custom_tools or []:
        tool_obj = _coerce_custom_tool(item)
        if tool_obj.name in resolved:
            raise ValueError(f"Duplicate custom tool name: {tool_obj.name}")
        resolved[tool_obj.name] = tool_obj
    return resolved


def _coerce_custom_tool(item: Any) -> ToolBase:
    if isinstance(item, ToolBase):
        return item
    if callable(item):
        metadata = getattr(item, "__researchharness_tool__", None)
        if not isinstance(metadata, dict):
            raise ValueError(
                f"Custom tool function {getattr(item, '__name__', item)!r} must be decorated with @researchharness.tool."
            )
        return FunctionTool(
            item,
            name=metadata.get("name"),
            description=metadata.get("description"),
            timeout_seconds=metadata.get("timeout_seconds"),
        )
    raise ValueError(f"Custom tool must be a decorated function or ToolBase instance, got {type(item).__name__}.")


def _validate_timeout_seconds(timeout_seconds: float | int | None) -> float | None:
    if timeout_seconds is None:
        return None
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Custom tool timeout_seconds must be a positive number.") from exc
    if timeout <= 0:
        raise ValueError("Custom tool timeout_seconds must be > 0.")
    return timeout


def _resolve_tool_name(func: Callable[..., Any], override: str | None) -> str:
    name = str(override or getattr(func, "__name__", "")).strip()
    if not name:
        raise ValueError("Custom tool name must be non-empty.")
    if not TOOL_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid custom tool name {name!r}. Use 1-64 characters: letters, numbers, underscore, or hyphen; start with a letter or underscore."
        )
    return name


def _resolve_tool_description(func: Callable[..., Any], override: str | None) -> str:
    description = str(override or inspect.getdoc(func) or "").strip()
    if not description:
        raise ValueError(f"Custom tool {getattr(func, '__name__', '<callable>')!r} must have a docstring or description.")
    return description


def _schema_from_signature(func: Callable[..., Any], context_parameters: set[str]) -> dict[str, Any]:
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception as exc:
        raise ValueError(f"Could not resolve type hints for custom tool {func.__name__}: {exc}") from exc

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in signature.parameters.values():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"Custom tool {func.__name__} may not use *args or **kwargs.")
        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            raise ValueError(f"Custom tool {func.__name__} may not use positional-only parameters.")
        if param.name in CONTEXT_PARAMETER_NAMES:
            if param.kind is not inspect.Parameter.KEYWORD_ONLY:
                raise ValueError(f"Context parameter {param.name!r} in custom tool {func.__name__} must be keyword-only.")
            context_parameters.add(param.name)
            continue
        if param.name not in hints:
            raise ValueError(f"Custom tool {func.__name__} parameter {param.name!r} must have a type annotation.")
        schema, nullable = _annotation_to_schema(hints[param.name], f"{func.__name__}.{param.name}")
        if param.default is inspect.Parameter.empty and not nullable:
            required.append(param.name)
        elif param.default is not inspect.Parameter.empty:
            schema["default"] = param.default
        properties[param.name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _annotation_to_schema(annotation: Any, label: str) -> tuple[dict[str, Any], bool]:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if annotation is Any:
        raise ValueError(f"Custom tool parameter {label} may not use Any; use a concrete JSON-compatible type.")
    if origin in (UnionType, Union):
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            schema, _ = _annotation_to_schema(non_none[0], label)
            return schema, True
        raise ValueError(f"Custom tool parameter {label} uses an unsupported union type.")
    if origin is Literal:
        values = list(args)
        if not values:
            raise ValueError(f"Custom tool parameter {label} uses an empty Literal.")
        value_types = {type(value) for value in values}
        if len(value_types) != 1 or next(iter(value_types)) not in {str, int, float, bool}:
            raise ValueError(f"Custom tool parameter {label} uses unsupported Literal values.")
        schema, _ = _annotation_to_schema(type(values[0]), label)
        schema["enum"] = values
        return schema, False
    if annotation is str:
        return {"type": "string"}, False
    if annotation is int:
        return {"type": "integer"}, False
    if annotation is float:
        return {"type": "number"}, False
    if annotation is bool:
        return {"type": "boolean"}, False
    if annotation is dict:
        return {"type": "object"}, False
    if annotation in (list, tuple):
        return {"type": "array"}, False
    if origin in (list, tuple, Sequence, AbcSequence):
        item_schema: dict[str, Any] = {}
        if args and args[0] is not Ellipsis:
            item_schema, _ = _annotation_to_schema(args[0], label)
        return {"type": "array", "items": item_schema}, False
    if origin is dict:
        key_type = args[0] if args else str
        if key_type is not str:
            raise ValueError(f"Custom tool parameter {label} dict keys must be str.")
        return {"type": "object"}, False
    raise ValueError(f"Custom tool parameter {label} has unsupported type annotation: {annotation!r}")
