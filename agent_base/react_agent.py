import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Type

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
import tiktoken
from agent_base.base import BaseAgent
from agent_base.console_utils import ConsoleEventPrinter
from agent_base.context_compact import compact_messages, should_compact_messages
from agent_base.model_profiles import resolve_model_profile
from agent_base.provider_compat import apply_sampling_params
from agent_base.prompt import composed_system_prompt
from agent_base.session_state import AgentSessionState, CompactionRecord, persist_session_state, resolve_session_state_path
from agent_base.trace_utils import FlatTraceWriter
from agent_base.tools.tooling import normalize_workspace_root
from agent_base.tools.tool_extra import StrReplaceEditor
from agent_base.tools.tool_file import Edit, Glob, Grep, Read, ReadImage, ReadPDF, Write
from agent_base.tools.tool_runtime import Bash, TerminalInterrupt, TerminalKill, TerminalRead, TerminalStart, TerminalWrite
from agent_base.tools.tool_user import AskUser
from agent_base.tools.tool_web import ScholarSearch, WebFetch, WebSearch
from agent_base.utils import (
    PROJECT_ROOT,
    MissingRequiredEnvError,
    append_saved_image_paths_to_prompt,
    env_flag,
    image_input_content_parts,
    load_dotenv,
    read_role_prompt_files,
    require_required_env,
    safe_jsonable,
    stage_image_file_for_input,
)

import datetime
import random
import time

AVAILABLE_TOOLS = [
    Glob(),
    Grep(),
    Read(),
    ReadPDF(),
    ReadImage(),
    Write(),
    Edit(),
    Bash(),
    WebSearch(),
    ScholarSearch(),
    WebFetch(),
    AskUser(),
    TerminalStart(),
    TerminalWrite(),
    TerminalRead(),
    TerminalInterrupt(),
    TerminalKill(),
]
AVAILABLE_TOOL_MAP = {tool.name: tool for tool in AVAILABLE_TOOLS}
OPTIONAL_TOOLS = [
    StrReplaceEditor(),
]
OPTIONAL_TOOL_MAP = {tool.name: tool for tool in OPTIONAL_TOOLS}
ALL_TOOL_MAP = {**AVAILABLE_TOOL_MAP, **OPTIONAL_TOOL_MAP}
DEFAULT_IMAGE_TOKEN_ESTIMATE = 1536
DEFAULT_MODEL_NAME = "gpt-5.4"
DEFAULT_MAX_LLM_CALLS = 100
DEFAULT_MAX_ROUNDS = 100
DEFAULT_MAX_RUNTIME_SECONDS = 150 * 60
DEFAULT_MAX_OUTPUT_TOKENS = 10000
DEFAULT_MAX_INPUT_TOKENS = 320000
DEFAULT_MAX_RETRIES = 10
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.95
DEFAULT_PRESENCE_PENALTY = 1.1
DEFAULT_LLM_TIMEOUT_SECONDS = 600.0
MAX_PARALLEL_READ_TOOL_CALLS = 3
PARALLEL_READ_TOOL_NAMES = frozenset(
    {
        "Glob",
        "Grep",
        "Read",
        "ReadImage",
        "WebSearch",
        "ScholarSearch",
        "WebFetch",
    }
)


def default_model_name() -> str:
    return os.environ.get("MODEL_NAME", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME


class LLMHardTimeoutError(TimeoutError):
    pass


@contextmanager
def llm_hard_timeout(timeout_seconds: float):
    if (
        timeout_seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
    ):
        yield
        return

    def _handle_timeout(signum, frame):
        raise LLMHardTimeoutError(f"LLM request exceeded hard timeout of {timeout_seconds:.1f}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def today_date():
    return datetime.date.today().strftime("%Y-%m-%d")


def max_llm_calls_per_run() -> int:
    return int(os.getenv("MAX_LLM_CALL_PER_RUN", str(DEFAULT_MAX_LLM_CALLS)))


def max_agent_rounds() -> int:
    return int(os.getenv("MAX_AGENT_ROUNDS", str(DEFAULT_MAX_ROUNDS)))


def max_agent_runtime_seconds() -> int:
    return int(os.getenv("MAX_AGENT_RUNTIME_SECONDS", str(DEFAULT_MAX_RUNTIME_SECONDS)))


def llm_max_output_tokens() -> int:
    return int(os.getenv("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS)))


def remaining_runtime_seconds(runtime_deadline: Optional[float]) -> Optional[float]:
    if runtime_deadline is None:
        return None
    return runtime_deadline - time.time()


def debug_enabled() -> bool:
    return env_flag("DEBUG_AGENT")


def assistant_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            else:
                text_parts.append(str(part))
        return "".join(text_parts)
    return str(content)


def message_trace_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    text_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(str(part.get("text", "")))
        elif part_type == "image_url":
            image_url = part.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
            url_text = str(url)
            if url_text.startswith("data:image/"):
                url_text = url_text.split(",", 1)[0] + ",...(base64 omitted)"
            text_parts.append(f"[image_url: {url_text}]")
        else:
            text_parts.append(str(part))
    return "\n".join(text for text in text_parts if text)


def _message_has_image_content(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)


def _last_assistant_message_index(messages: Sequence[dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == "assistant":
            return index
    return -1


def _image_reference_summary(part: dict[str, Any]) -> str:
    image_url = part.get("image_url", {})
    url = image_url.get("url", "") if isinstance(image_url, dict) else ""
    url_text = str(url)
    if url_text.startswith("data:image/"):
        return url_text.split(",", 1)[0] + ",...(base64 omitted)"
    elif len(url_text) > 180:
        return url_text[:180] + "...(truncated)"
    return url_text or "unavailable"


def _image_path_hint_from_text(text: str) -> str:
    patterns = (
        r"\[User-provided image saved at ([^\]\n]+)\]",
        r"Local image path:\s*([^\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def _omitted_image_part_text(part: dict[str, Any], *, saved_path_hint: str = "") -> str:
    url_text = _image_reference_summary(part)
    path_text = f" Saved local path: {saved_path_hint}." if saved_path_hint else ""
    return (
        "[Previous image omitted from this model request to avoid repeatedly resending image bytes. "
        f"Original image reference: {url_text}. "
        f"{path_text} "
        "The nearby conversation text or tool metadata records saved local paths when available; "
        "use ReadImage on the saved path if visual details are needed again.]"
    )


def _replace_image_parts_with_text(content: Any, *, message_index: int) -> tuple[Any, list[dict[str, Any]]]:
    if not isinstance(content, list):
        return content, []
    replacement: list[Any] = []
    omitted_images: list[dict[str, Any]] = []
    image_index = 0
    last_text_path_hint = ""
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            path_hint = _image_path_hint_from_text(str(part.get("text", "")))
            if path_hint:
                last_text_path_hint = path_hint
        if isinstance(part, dict) and part.get("type") == "image_url":
            omitted_images.append(
                {
                    "message_index": message_index,
                    "image_index": image_index,
                    "reference_summary": _image_reference_summary(part),
                    "saved_path_hint": last_text_path_hint,
                }
            )
            replacement.append({"type": "text", "text": _omitted_image_part_text(part, saved_path_hint=last_text_path_hint)})
        else:
            replacement.append(safe_jsonable(part))
        if isinstance(part, dict) and part.get("type") == "image_url":
            image_index += 1
    return replacement, omitted_images


def prepare_messages_for_llm(messages: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return request messages with old image bytes replaced by text references.

    Image content parts are only needed immediately after they enter the
    conversation. Older image parts stay represented as text so the agent can
    re-read saved paths with ReadImage without resending the image every round.
    """
    last_assistant_index = _last_assistant_message_index(messages)
    request_messages: list[dict[str, Any]] = []
    omitted_images: list[dict[str, Any]] = []
    for index, raw_message in enumerate(messages):
        message = safe_jsonable(raw_message)
        if not isinstance(message, dict):
            request_messages.append({"role": "user", "content": str(message)})
            continue
        if index <= last_assistant_index and _message_has_image_content(message):
            message = dict(message)
            message["content"], message_omitted_images = _replace_image_parts_with_text(
                message.get("content"),
                message_index=index,
            )
            omitted_images.extend(message_omitted_images)
        request_messages.append(message)
    image_aging = {
        "omitted_image_count": len(omitted_images),
        "omitted_images": omitted_images,
    }
    return request_messages, image_aging


def assistant_reasoning_content(message: Any) -> Optional[Any]:
    if hasattr(message, "model_dump"):
        try:
            dumped = safe_jsonable(message.model_dump())
            if isinstance(dumped, dict) and "reasoning_content" in dumped:
                return dumped.get("reasoning_content")
        except Exception:
            pass
    model_extra = getattr(message, "model_extra", None)
    if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
        return safe_jsonable(model_extra.get("reasoning_content"))
    raw_reasoning = getattr(message, "reasoning_content", None)
    if raw_reasoning is None:
        return None
    return safe_jsonable(raw_reasoning)


def assistant_has_meaningful_text(content: Any) -> bool:
    return bool(assistant_text_content(content).strip())


def input_tokens_from_usage(usage: Any) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def llm_call_trace_payload(
    *,
    request_messages: Sequence[dict[str, Any]],
    image_aging: Optional[dict[str, Any]] = None,
    response: Any,
    model_name: str,
    native_tools: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "model_name": model_name,
        "request_messages": safe_jsonable(list(request_messages)),
        "tools_enabled": bool(native_tools),
        "native_tools": safe_jsonable(list(native_tools)),
        "response": safe_jsonable(response),
    }
    if image_aging and int(image_aging.get("omitted_image_count", 0) or 0) > 0:
        payload["image_aging"] = safe_jsonable(image_aging)
    return payload


def compaction_trace_payload(
    *,
    trigger_reason: str,
    outcome: Any,
) -> dict[str, Any]:
    return {
        "trigger_reason": trigger_reason,
        "status": getattr(outcome, "status", ""),
        "error": getattr(outcome, "error", ""),
        "prior_token_estimate": getattr(outcome, "prior_token_estimate", 0),
        "new_token_estimate": getattr(outcome, "new_token_estimate", 0),
        "compacted_group_count": getattr(outcome, "compacted_group_count", 0),
        "kept_group_count": getattr(outcome, "kept_group_count", 0),
        "existing_memory_text": getattr(outcome, "existing_memory_text", ""),
        "summary_request": safe_jsonable(getattr(outcome, "summary_request", []) or []),
        "summary_response": safe_jsonable(getattr(outcome, "summary_response", {}) or {}),
        "summary_text": getattr(outcome, "summary_text", ""),
        "pre_messages": safe_jsonable(getattr(outcome, "pre_messages", []) or []),
        "post_messages": safe_jsonable(getattr(outcome, "post_messages", []) or []),
    }


def legacy_protocol_error(content: str) -> Optional[str]:
    stripped = content.lstrip()
    if stripped.startswith("<tool_call>"):
        return "assistant emitted deprecated text <tool_call> protocol"
    if stripped.startswith("<tool_response>"):
        return "assistant emitted deprecated text <tool_response> protocol"
    if stripped.startswith("<think>"):
        return "assistant emitted deprecated text <think> protocol"
    if stripped.startswith("<answer>"):
        return "assistant emitted deprecated text <answer> protocol"
    return None


def tool_schema(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def resolved_tool_names(function_list: Optional[Sequence[str]]) -> list[str]:
    if function_list is None:
        return list(AVAILABLE_TOOL_MAP.keys())
    resolved: list[str] = []
    for raw_name in function_list:
        name = str(raw_name).strip()
        if name:
            resolved.append(name)
    return resolved


def available_tool_schemas(function_list: Optional[Sequence[str]] = None) -> list[dict[str, Any]]:
    names = resolved_tool_names(function_list)
    unknown_tools = [name for name in names if name not in ALL_TOOL_MAP]
    if unknown_tools:
        raise ValueError(f"Unknown tools requested: {unknown_tools}")
    return [tool_schema(ALL_TOOL_MAP[name]) for name in names]


def resolve_extra_tool_names(extra_tools: Optional[Sequence[str]]) -> list[str]:
    resolved: list[str] = []
    for raw_name in extra_tools or []:
        name = str(raw_name).strip()
        if not name:
            continue
        if name not in OPTIONAL_TOOL_MAP:
            raise ValueError(f"Unknown extra tool requested: {name}")
        if name not in resolved:
            resolved.append(name)
    return resolved


def default_tool_names(*, include_ask_user: bool = True, extra_tools: Optional[Sequence[str]] = None) -> list[str]:
    names = [name for name in AVAILABLE_TOOL_MAP if include_ask_user or name != "AskUser"]
    for name in resolve_extra_tool_names(extra_tools):
        if name not in names:
            names.append(name)
    return names


def normalized_tool_call(tool_call: Any) -> dict[str, Any]:
    return {
        "id": getattr(tool_call, "id", ""),
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


def tool_result_message_content(result: Any) -> str:
    if isinstance(result, dict) and result.get("kind") == "image_tool_result":
        return str(result.get("text", "")).strip() or "ReadImage returned no metadata."
    if isinstance(result, (dict, list)):
        return json.dumps(safe_jsonable(result), ensure_ascii=False)
    return str(result)


def model_supports_runtime_image_parts(model_name: str) -> bool:
    normalized = str(model_name or "").strip().casefold()
    if "deepseek" in normalized:
        return False
    return True


def image_context_message(result: Any, model_name: str) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict) or result.get("kind") != "image_tool_result":
        return None
    image_url = str(result.get("image_url", "")).strip()
    if not image_url and model_supports_runtime_image_parts(model_name):
        return None
    metadata_text = str(result.get("text", "")).strip()
    text = (
        "Runtime image context from ReadImage.\n"
        "Use the attached image as evidence produced by that tool call when deciding the next step or final result.\n"
        "Do not assume that all required tool work is complete merely because an image is attached."
    )
    if metadata_text:
        text += "\n\nReadImage metadata:\n" + metadata_text
    if not model_supports_runtime_image_parts(model_name):
        text += (
            "\n\nThis model endpoint does not accept runtime image content parts, so only the "
            "ReadImage metadata is forwarded in conversation history. Do not invent visual details "
            "that are not supported by the metadata."
        )
        return {"role": "user", "content": text}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
        ],
    }


def api_tool_message(tool_call_id: str, result: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": tool_result_message_content(result),
    }


def assistant_history_message(
    *,
    content: Any,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    reasoning_content: Optional[Any] = None,
    raw_message: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if isinstance(raw_message, dict):
        message = safe_jsonable(raw_message)
        if isinstance(message, dict):
            message["role"] = "assistant"
            if content is not None or "content" not in message:
                message["content"] = content
            if tool_calls and "tool_calls" not in message:
                message["tool_calls"] = tool_calls
            elif "tool_calls" in message and not message.get("tool_calls"):
                message.pop("tool_calls", None)
            if reasoning_content is not None and "reasoning_content" not in message:
                message["reasoning_content"] = reasoning_content
            elif "reasoning_content" in message and message.get("reasoning_content") is None:
                message.pop("reasoning_content", None)
            return message
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return message


def assistant_retry_history_message(
    *,
    content: Any,
    reasoning_content: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    if reasoning_content is None and not assistant_has_meaningful_text(content):
        return None
    # For retry/correction branches, preserve a replay-safe assistant history
    # message without tool calls so provider-specific reasoning state is not
    # lost while avoiding invalid unfinished tool-call history.
    return assistant_history_message(
        content=assistant_text_content(content),
        reasoning_content=reasoning_content,
    )


def parse_tool_arguments_list(tool_calls: list[dict[str, Any]]) -> list[Any]:
    def _maybe_parse_nested_json(raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if isinstance(parsed, str):
            nested_text = parsed.strip()
            if nested_text.startswith("{") or nested_text.startswith("["):
                try:
                    return json.loads(nested_text)
                except (TypeError, ValueError):
                    return parsed
        return parsed

    parsed_arguments: list[Any] = []
    for tool_call in tool_calls:
        function_block = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        tool_arguments_raw = function_block.get("arguments", {})
        parsed = _maybe_parse_nested_json(tool_arguments_raw)
        parsed_arguments.append(safe_jsonable(parsed))
    return parsed_arguments


def image_trace_paths(result: Any) -> list[str]:
    if not isinstance(result, dict) or result.get("kind") != "image_tool_result":
        return []
    path = str(result.get("path", "")).strip()
    return [path] if path else []


def image_context_trace_text(result: Any) -> str:
    if not isinstance(result, dict) or result.get("kind") != "image_tool_result":
        return ""
    metadata_text = str(result.get("text", "")).strip()
    text = (
        "Runtime image context from ReadImage.\n"
        "Use the attached image as evidence produced by that tool call when deciding the next step or final result.\n"
        "Do not assume that all required tool work is complete merely because an image is attached."
    )
    if metadata_text:
        text += "\n\nReadImage metadata:\n" + metadata_text
    return text


def default_llm_config(model_name: Optional[str] = None) -> dict:
    selected_model = str(model_name or "").strip() or default_model_name()
    return {
        "model": selected_model,
        "api_key": os.environ.get("API_KEY", "EMPTY"),
        "api_base": os.environ.get("API_BASE"),
        "timeout_seconds": float(os.environ.get("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS))),
        "generate_cfg": {
            "max_input_tokens": int(os.environ.get("MAX_INPUT_TOKENS", str(DEFAULT_MAX_INPUT_TOKENS))),
            "max_output_tokens": int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
            "max_retries": int(os.environ.get("LLM_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
            "temperature": float(os.environ.get("TEMPERATURE", str(DEFAULT_TEMPERATURE))),
            "top_p": float(os.environ.get("TOP_P", str(DEFAULT_TOP_P))),
            "presence_penalty": float(os.environ.get("PRESENCE_PENALTY", str(DEFAULT_PRESENCE_PENALTY))),
        },
    }


def execute_tool_by_name(tool_map: dict[str, Any], tool_name: str, tool_args: Any, **kwargs):
    if tool_name not in tool_map:
        return f"Error: Tool {tool_name} not found"
    tool = tool_map[tool_name]
    if tool_name == "ReadImage" and hasattr(tool, "call_for_llm"):
        return tool.call_for_llm(tool_args, **kwargs)
    return tool.call(tool_args, **kwargs)


def can_parallelize_tool_name(tool_name: str) -> bool:
    return tool_name in PARALLEL_READ_TOOL_NAMES


def tool_execution_batches(tool_names: Sequence[str]) -> list[list[int]]:
    batches: list[list[int]] = []
    read_batch: list[int] = []
    for index, tool_name in enumerate(tool_names):
        if can_parallelize_tool_name(tool_name):
            read_batch.append(index)
            continue
        if read_batch:
            batches.append(read_batch)
            read_batch = []
        batches.append([index])
    if read_batch:
        batches.append(read_batch)
    return batches


class MultiTurnReactAgent(BaseAgent):
    def __init__(
        self,
        function_list: Optional[List[str]] = None,
        llm: Optional[Dict] = None,
        trace_dir: Optional[str] = None,
        role_prompt: Optional[str] = None,
        max_llm_calls: Optional[int] = None,
        max_rounds: Optional[int] = None,
        max_runtime_seconds: Optional[int] = None,
    ):
        if not isinstance(llm, dict):
            raise ValueError("llm must be a dict configuration.")
        requested_tools = self.resolve_function_list(function_list)
        if requested_tools is None:
            requested_tools = list(AVAILABLE_TOOL_MAP.keys())
        unknown_tools = [tool for tool in requested_tools if tool not in ALL_TOOL_MAP]
        if unknown_tools:
            raise ValueError(f"Unknown tools requested: {unknown_tools}")
        if "model" not in llm or not str(llm["model"]).strip():
            raise ValueError('llm["model"] must be a non-empty string.')
        if "generate_cfg" not in llm or not isinstance(llm["generate_cfg"], dict):
            raise ValueError('llm["generate_cfg"] must be a dict.')

        self.tool_map = {tool_name: ALL_TOOL_MAP[tool_name] for tool_name in requested_tools}
        self.tool_names = list(self.tool_map.keys())
        self.model = str(llm["model"])
        self.llm_generate_cfg = llm["generate_cfg"]
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.trace_path: Optional[Path] = None
        self.session_state_path: Optional[Path] = None
        self.role_prompt = self.resolve_role_prompt(role_prompt)
        self.max_llm_calls = int(max_llm_calls) if max_llm_calls is not None else max_llm_calls_per_run()
        self.max_rounds = int(max_rounds) if max_rounds is not None else max_agent_rounds()
        self.max_runtime_seconds = (
            int(max_runtime_seconds) if max_runtime_seconds is not None else max_agent_runtime_seconds()
        )
        if self.max_rounds <= 0:
            raise ValueError("max_rounds must be > 0.")
        self._native_tools = [tool_schema(self.tool_map[tool_name]) for tool_name in self.tool_names]
        self._encoding = tiktoken.get_encoding("cl100k_base")
        self._native_tools_token_estimate = len(
            self._encoding.encode(json.dumps(self._native_tools, ensure_ascii=False))
        )
        self._llm_timeout_seconds = float(
            llm.get("timeout_seconds", os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS)))
        )
        self._llm_api_key = str(llm.get("api_key") or os.environ.get("API_KEY", "EMPTY"))
        api_base = str(llm.get("api_base") or os.environ.get("API_BASE", "")).strip()
        self._llm_api_base = api_base or None
        self._llm_client = (
            OpenAI(
                api_key=self._llm_api_key,
                base_url=self._llm_api_base,
                timeout=self._llm_timeout_seconds,
            )
            if self._llm_api_base
            else None
        )

    def _call_chat_completion(
        self,
        msgs,
        *,
        include_native_tools: bool,
        max_tries=10,
        runtime_deadline: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
    ) -> dict[str, Any]:
        max_tries = int(self.llm_generate_cfg.get("max_retries", max_tries))
        if self._llm_client is None or not self._llm_api_base:
            return {"status": "error", "error": "llm api error: API_BASE is not set."}

        base_sleep_time = 1
        last_error = "unknown llm error"
        for attempt in range(max_tries):
            remaining = remaining_runtime_seconds(runtime_deadline)
            if remaining is not None and remaining <= 0:
                last_error = "agent runtime limit reached before llm call could complete"
                break
            try:
                if debug_enabled():
                    print(f"--- Attempting to call the service, try {attempt + 1}/{max_tries} ---")
                request_timeout = (
                    min(self._llm_timeout_seconds, max(remaining, 0.001))
                    if remaining is not None
                    else self._llm_timeout_seconds
                )
                request_client = self._llm_client.with_options(timeout=request_timeout)
                request_kwargs = dict(
                    model=self.model,
                    messages=msgs,
                    max_tokens=int(
                        max_output_tokens
                        if max_output_tokens is not None
                        else self.llm_generate_cfg.get("max_output_tokens", llm_max_output_tokens())
                    ),
                )
                apply_sampling_params(
                    request_kwargs,
                    model_name=self.model,
                    temperature=(
                        temperature if temperature is not None else self.llm_generate_cfg.get("temperature", 0.6)
                    ),
                    top_p=top_p if top_p is not None else self.llm_generate_cfg.get("top_p", 0.95),
                    presence_penalty=(
                        presence_penalty
                        if presence_penalty is not None
                        else self.llm_generate_cfg.get("presence_penalty", 1.1)
                    ),
                )
                if include_native_tools and self._native_tools:
                    request_kwargs["tools"] = self._native_tools
                    request_kwargs["tool_choice"] = "auto"
                    request_kwargs["parallel_tool_calls"] = True
                with llm_hard_timeout(request_timeout):
                    chat_response = request_client.chat.completions.create(**request_kwargs)
                choice = chat_response.choices[0]
                message = choice.message
                content = message.content
                tool_calls = [normalized_tool_call(tool_call) for tool_call in (message.tool_calls or [])]
                reasoning_content = assistant_reasoning_content(message)
                raw_message = safe_jsonable(message.model_dump()) if hasattr(message, "model_dump") else None
                usage = safe_jsonable(chat_response.usage.model_dump()) if getattr(chat_response, "usage", None) else None

                if assistant_has_meaningful_text(content) or tool_calls:
                    if debug_enabled():
                        print("--- Service call successful, received a valid response ---")
                    return {
                        "status": "ok",
                        "finish_reason": choice.finish_reason,
                        "content": content,
                        "tool_calls": tool_calls,
                        "reasoning_content": reasoning_content,
                        "raw_message": raw_message,
                        "usage": usage,
                    }
                else:
                    last_error = "empty response from llm api"
                    if debug_enabled():
                        print(f"Warning: Attempt {attempt + 1} received an empty response.")

            except (APIError, APIConnectionError, APITimeoutError, LLMHardTimeoutError) as e:
                last_error = str(e)
                if debug_enabled():
                    print(f"Error: Attempt {attempt + 1} failed with an API or network error: {e}")

            if attempt < max_tries - 1:
                sleep_time = base_sleep_time * (2 ** attempt) + random.uniform(0, 1)
                sleep_time = min(sleep_time, 30)
                remaining = remaining_runtime_seconds(runtime_deadline)
                if remaining is not None:
                    if remaining <= 0:
                        last_error = "agent runtime limit reached before llm retry could complete"
                        break
                    sleep_time = min(sleep_time, remaining)
                if debug_enabled():
                    print(f"Retrying in {sleep_time:.2f} seconds...")
                if sleep_time > 0:
                    time.sleep(sleep_time)
            else:
                if debug_enabled():
                    print("Error: All retry attempts have been exhausted. The call has failed.")

        return {"status": "error", "error": f"llm api error: {last_error}"}

    def call_llm_api(self, msgs, max_tries=10, runtime_deadline: Optional[float] = None) -> dict[str, Any]:
        return self._call_chat_completion(
            msgs,
            include_native_tools=True,
            max_tries=max_tries,
            runtime_deadline=runtime_deadline,
        )

    def call_compaction_api(
        self,
        msgs,
        *,
        runtime_deadline: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        return self._call_chat_completion(
            msgs,
            include_native_tools=False,
            max_tries=3,
            runtime_deadline=runtime_deadline,
            max_output_tokens=max_output_tokens,
            temperature=0.0,
            top_p=1.0,
            presence_penalty=0.0,
        )

    def count_tokens(self, messages, *, include_tool_schema: bool = True):
        image_token_estimate = int(os.getenv("IMAGE_PART_TOKEN_ESTIMATE", str(DEFAULT_IMAGE_TOKEN_ESTIMATE)))
        token_count = self._native_tools_token_estimate if include_tool_schema else 0
        for message in messages:
            token_count += len(self._encoding.encode(message.get("role", "")))
            content = message.get("content", "")
            if isinstance(content, str):
                token_count += len(self._encoding.encode(content))
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        token_count += len(self._encoding.encode(str(part)))
                        continue
                    if part.get("type") == "text":
                        token_count += len(self._encoding.encode(str(part.get("text", ""))))
                    elif part.get("type") == "image_url":
                        token_count += image_token_estimate
                    else:
                        token_count += len(self._encoding.encode(str(part)))
            else:
                token_count += len(self._encoding.encode(str(content)))
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                token_count += len(self._encoding.encode(json.dumps(tool_calls, ensure_ascii=False)))
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                token_count += len(self._encoding.encode(reasoning_content))
            elif reasoning_content is not None:
                token_count += len(
                    self._encoding.encode(json.dumps(safe_jsonable(reasoning_content), ensure_ascii=False))
                )
        return token_count

    def run(self, prompt: str, workspace_root: Optional[str] = None) -> str:
        """Run the agent on one prompt and return only the final result text."""
        return self._run_session(prompt, workspace_root=workspace_root)["result_text"]

    def _run_session(
        self,
        prompt: str,
        workspace_root: Optional[str] = None,
        event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        initial_content_parts: Optional[Sequence[dict[str, Any]]] = None,
        prior_messages: Optional[Sequence[dict[str, Any]]] = None,
        interrupt_event: Optional[threading.Event] = None,
    ) -> dict:
        """Internal execution path with trace data for tests and debugging."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string.")

        prompt_text = prompt.strip()
        resolved_workspace_root = normalize_workspace_root(workspace_root)
        start_time = time.time()
        trace_dir = self.trace_dir
        cur_date = today_date()
        extra_blocks = [self.role_prompt] if self.role_prompt else None
        system_prompt = composed_system_prompt(current_date=str(cur_date), extra_blocks=extra_blocks)
        user_content = (
            f"Current workspace root: {resolved_workspace_root}\n"
            "Relative local file paths resolve from the workspace root.\n\n"
            f"Prompt:\n{prompt_text}"
        )
        if initial_content_parts is not None:
            if not isinstance(initial_content_parts, Sequence) or isinstance(initial_content_parts, (str, bytes)):
                raise ValueError("initial_content_parts must be a sequence of OpenAI-style content part dicts.")
            safe_initial_parts = safe_jsonable(list(initial_content_parts))
            if not isinstance(safe_initial_parts, list) or not all(isinstance(part, dict) for part in safe_initial_parts):
                raise ValueError("initial_content_parts must contain only dict content parts.")
            user_content: Any = [{"type": "text", "text": user_content}, *safe_initial_parts]
        continuing_conversation = prior_messages is not None
        if continuing_conversation:
            if not isinstance(prior_messages, Sequence) or isinstance(prior_messages, (str, bytes)):
                raise ValueError("prior_messages must be a sequence of message dicts.")
            safe_prior_messages = safe_jsonable(list(prior_messages))
            if not isinstance(safe_prior_messages, list) or not all(isinstance(message, dict) for message in safe_prior_messages):
                raise ValueError("prior_messages must contain only dict messages.")
            messages = list(safe_prior_messages)
            if not messages or messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_content})
        else:
            messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        max_llm_calls = self.max_llm_calls
        max_input_tokens = int(self.llm_generate_cfg.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS))
        max_output_tokens = int(self.llm_generate_cfg.get("max_output_tokens", llm_max_output_tokens()))
        compact_trigger_tokens = self.llm_generate_cfg.get("compact_trigger_tokens")
        if compact_trigger_tokens is None:
            compact_trigger_tokens = os.getenv("AUTO_COMPACT_TRIGGER_TOKENS", "128k")
        model_profile = resolve_model_profile(
            self.model,
            configured_max_input_tokens=max_input_tokens,
            configured_max_output_tokens=max_output_tokens,
            compact_trigger_tokens=compact_trigger_tokens,
        )
        agent_runtime_limit = self.max_runtime_seconds
        runtime_deadline = start_time + agent_runtime_limit
        num_llm_calls_available = max_llm_calls
        round_index = 0
        trace_writer = FlatTraceWriter(
            trace_dir=trace_dir,
            model_name=self.model,
            workspace_root=resolved_workspace_root,
            on_event=event_callback,
        )
        self.trace_path = trace_writer.path
        self.session_state_path = resolve_session_state_path(self.trace_path) if self.trace_path else None
        session_state = AgentSessionState(
            run_id=trace_writer.run_id,
            model_name=self.model,
            workspace_root=str(resolved_workspace_root),
            prompt=prompt_text,
            trace_path=str(self.trace_path) if self.trace_path else "",
            llm_calls_remaining=num_llm_calls_available,
            max_rounds=self.max_rounds,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            model_profile=model_profile,
        )

        def persist_state(*, termination: str = "", error: str = "") -> None:
            session_state.trace_path = str(self.trace_path) if self.trace_path else ""
            session_state.turn_index = round_index
            session_state.llm_calls_remaining = num_llm_calls_available
            session_state.current_token_estimate = self.count_tokens(messages)
            session_state.termination = termination
            session_state.error = error
            session_state.capture_messages(messages)
            if self.session_state_path:
                persist_session_state(self.session_state_path, session_state)

        def finalize(result_text: str, termination: str, *, role: str = "runtime", error: str = "") -> dict[str, Any]:
            trace_writer.append(
                role=role,
                text=result_text,
                turn_index=round_index,
                termination=termination,
                error=error,
            )
            persist_state(termination=termination, error=error)
            return {
                "prompt": prompt_text,
                "messages": messages,
                "result_text": result_text,
                "termination": termination,
                "trace_path": str(self.trace_path) if self.trace_path else "",
                "session_state_path": str(self.session_state_path) if self.session_state_path else "",
            }

        def interruption_requested() -> bool:
            return bool(interrupt_event is not None and interrupt_event.is_set())

        def finalize_interrupted() -> dict[str, Any]:
            return finalize(
                "Interrupted by user. Continue with a follow-up prompt to resume from the current context.",
                "interrupted",
                role="runtime",
                error="user interrupt",
            )

        if continuing_conversation:
            trace_writer.append(
                role="runtime",
                text="Continuing existing conversation with prior messages.",
                turn_index=0,
            )
        else:
            trace_writer.append(role="system", text=system_prompt, turn_index=0)
        trace_writer.append(role="user", text=message_trace_text(user_content), turn_index=0)
        persist_state()

        while num_llm_calls_available > 0 and round_index < self.max_rounds:
            if interruption_requested():
                return finalize_interrupted()
            if remaining_runtime_seconds(runtime_deadline) is not None and remaining_runtime_seconds(runtime_deadline) <= 0:
                result_text = "No result found before the maximum agent runtime limit."
                termination = f"agent runtime limit reached: {agent_runtime_limit}s"
                return finalize(result_text, termination, error=termination)
            current_token_estimate = self.count_tokens(messages)
            should_compact = False
            compact_reason = ""
            if len(messages) > 2:
                should_compact, compact_reason = should_compact_messages(
                    last_input_tokens=session_state.last_input_tokens,
                    current_token_estimate=current_token_estimate,
                    model_profile=model_profile,
                )
            if should_compact:
                trace_writer.append(
                    role="runtime",
                    text=(
                        "Runtime note: compacting earlier conversation history before the next model call "
                        f"because the {compact_reason} budget crossed the pre-limit threshold."
                    ),
                    turn_index=round_index,
                )
                compact_outcome = compact_messages(
                    messages=messages,
                    original_prompt_text=prompt_text,
                    model_name=self.model,
                    model_profile=model_profile,
                    llm_caller=self.call_compaction_api,
                    token_counter=self.count_tokens,
                    runtime_deadline=runtime_deadline,
                )
                if compact_outcome.status == "ok":
                    messages = compact_outcome.compacted_messages
                    session_state.last_input_tokens = None
                    session_state.compactions.append(
                        CompactionRecord(
                            turn_index=round_index,
                            status="ok",
                            trigger_reason=compact_reason,
                            prior_token_estimate=compact_outcome.prior_token_estimate,
                            prior_message_count=len(session_state.messages),
                            compacted_group_count=compact_outcome.compacted_group_count,
                            kept_group_count=compact_outcome.kept_group_count,
                            new_token_estimate=compact_outcome.new_token_estimate,
                            new_message_count=len(messages),
                            summary_text=compact_outcome.summary_text,
                        )
                    )
                    trace_writer.append(
                        role="runtime",
                        text=(
                            "Runtime note: context compaction completed. "
                            f"Token estimate {compact_outcome.prior_token_estimate} -> {compact_outcome.new_token_estimate}. "
                            f"Compacted {compact_outcome.compacted_group_count} older turn groups."
                        ),
                        turn_index=round_index,
                        capture_type="compaction",
                        payload=compaction_trace_payload(trigger_reason=compact_reason, outcome=compact_outcome),
                    )
                    persist_state()
                    current_token_estimate = compact_outcome.new_token_estimate
                else:
                    session_state.compactions.append(
                        CompactionRecord(
                            turn_index=round_index,
                            status="error",
                            trigger_reason=compact_reason,
                            prior_token_estimate=compact_outcome.prior_token_estimate,
                            prior_message_count=len(messages),
                            compacted_group_count=compact_outcome.compacted_group_count,
                            kept_group_count=compact_outcome.kept_group_count,
                            error=compact_outcome.error,
                        )
                    )
                    trace_writer.append(
                        role="runtime",
                        text="Runtime note: context compaction failed; the existing history was kept unchanged.",
                        turn_index=round_index,
                        error=compact_outcome.error,
                        capture_type="compaction",
                        payload=compaction_trace_payload(trigger_reason=compact_reason, outcome=compact_outcome),
                    )
                    persist_state(error=compact_outcome.error)
            if current_token_estimate > max_input_tokens:
                result_text = "No result found before the maximum input token limit."
                termination = f"input token limit reached: {current_token_estimate} > {max_input_tokens}"
                return finalize(result_text, termination, error=termination)
            if interruption_requested():
                return finalize_interrupted()
            round_index += 1
            num_llm_calls_available -= 1
            llm_request_messages, image_aging = prepare_messages_for_llm(messages)
            try:
                llm_reply = self.call_llm_api(llm_request_messages, runtime_deadline=runtime_deadline)
            except KeyboardInterrupt:
                return finalize_interrupted()
            if interruption_requested():
                return finalize_interrupted()
            trace_writer.append(
                role="runtime",
                text="",
                turn_index=round_index,
                capture_type="llm_call",
                payload=llm_call_trace_payload(
                    request_messages=llm_request_messages,
                    image_aging=image_aging,
                    response=llm_reply,
                    model_name=self.model,
                    native_tools=self._native_tools,
                ),
            )
            session_state.last_input_tokens = input_tokens_from_usage(
                llm_reply.get("usage") if isinstance(llm_reply, dict) else None
            )
            assistant_content = llm_reply.get("content") if isinstance(llm_reply, dict) else None
            assistant_tool_calls = llm_reply.get("tool_calls", []) if isinstance(llm_reply, dict) else []
            assistant_reasoning = llm_reply.get("reasoning_content") if isinstance(llm_reply, dict) else None
            assistant_raw_message = llm_reply.get("raw_message") if isinstance(llm_reply, dict) else None
            assistant_text = assistant_text_content(assistant_content)
            finish_reason = llm_reply.get("finish_reason") if isinstance(llm_reply, dict) else None
            assistant_tool_arguments = parse_tool_arguments_list(assistant_tool_calls)
            assistant_tool_call_ids = [str(tool_call.get("id", "")) for tool_call in assistant_tool_calls]
            assistant_tool_names = [
                str((tool_call.get("function", {}) if isinstance(tool_call, dict) else {}).get("name", ""))
                for tool_call in assistant_tool_calls
            ]
            if debug_enabled():
                if assistant_tool_calls:
                    print(f"Round {round_index}: tool_calls={json.dumps(assistant_tool_calls, ensure_ascii=False)}")
                    if assistant_text.strip():
                        print(f"Round {round_index} content: {assistant_text}")
                else:
                    print(f"Round {round_index}: {assistant_text}")
            if not isinstance(llm_reply, dict) or llm_reply.get("status") == "error":
                result_text = llm_reply.get("error", "llm api error: unknown error") if isinstance(llm_reply, dict) else str(llm_reply)
                if self.should_accept_terminal_error(
                    error_text=result_text,
                    workspace_root=resolved_workspace_root,
                    messages=messages,
                ):
                    recovered_result_text = self.accepted_terminal_error_result_text(
                        error_text=result_text,
                        workspace_root=resolved_workspace_root,
                        messages=messages,
                    ).strip()
                    if not recovered_result_text:
                        recovered_result_text = (
                            "Recovered completion after a terminal LLM/runtime error because the required "
                            "completion artifacts already exist in the workspace."
                        )
                    return finalize(recovered_result_text, "result", role="runtime", error=result_text)
                termination = "llm api error"
                return finalize(result_text, termination, error=result_text)

            deprecated_protocol = legacy_protocol_error(assistant_text)
            if deprecated_protocol is not None:
                trace_writer.append(
                    role="assistant",
                    text=assistant_text.strip(),
                    turn_index=round_index,
                    tool_call_ids=assistant_tool_call_ids,
                    tool_names=assistant_tool_names,
                    tool_arguments=assistant_tool_arguments,
                    finish_reason=finish_reason,
                    error=deprecated_protocol,
                )
                retry_assistant_message = assistant_retry_history_message(
                    content=assistant_content,
                    reasoning_content=assistant_reasoning,
                )
                if retry_assistant_message is not None:
                    messages.append(retry_assistant_message)
                correction_text = (
                    "Error: The previous assistant turn used the deprecated text-tag protocol. "
                    "Do not emit <tool_call>, <tool_response>, <think>, or <answer> in plain text. "
                    "Use only the native tool calling interface when tools are needed, or plain final result text when no more tools are needed."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": correction_text,
                    }
                )
                trace_writer.append(role="user", text=correction_text, turn_index=round_index)
                persist_state(error=deprecated_protocol)
                continue

            if finish_reason == "length" and assistant_tool_calls:
                protocol_error = "assistant tool call turn was truncated by output limit"
                trace_writer.append(
                    role="assistant",
                    text=assistant_text.strip(),
                    turn_index=round_index,
                    tool_call_ids=assistant_tool_call_ids,
                    tool_names=assistant_tool_names,
                    tool_arguments=assistant_tool_arguments,
                    finish_reason=finish_reason,
                    error=protocol_error,
                )
                retry_assistant_message = assistant_retry_history_message(
                    content=assistant_content,
                    reasoning_content=assistant_reasoning,
                )
                if retry_assistant_message is not None:
                    messages.append(retry_assistant_message)
                correction_text = (
                    "Error: The previous assistant turn hit the output limit while emitting native tool calls, "
                    "so none of those tool calls were executed. Re-emit the needed tool calls in a smaller form. "
                    "If a file is large, split it into multiple smaller Write calls or create it via shorter steps. "
                    "Do not resend the same oversized truncated tool call."
                )
                messages.append({"role": "user", "content": correction_text})
                trace_writer.append(role="user", text=correction_text, turn_index=round_index)
                persist_state(error=protocol_error)
                continue

            if assistant_tool_calls:
                trace_writer.append(
                    role="assistant",
                    text=assistant_text.strip(),
                    turn_index=round_index,
                    tool_call_ids=assistant_tool_call_ids,
                    tool_names=assistant_tool_names,
                    tool_arguments=assistant_tool_arguments,
                    finish_reason=finish_reason,
                )
                assistant_message = assistant_history_message(
                    content=assistant_content,
                    tool_calls=assistant_tool_calls,
                    reasoning_content=assistant_reasoning,
                    raw_message=assistant_raw_message,
                )
                tool_turn_message_start = len(messages)
                messages.append(assistant_message)
                deferred_image_contexts: list[tuple[str, str, Any, Any, dict[str, Any]]] = []
                tool_call_items: list[dict[str, Any]] = []
                for tool_call, tool_arguments in zip(assistant_tool_calls, assistant_tool_arguments):
                    function_block = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
                    tool_call_items.append(
                        {
                            "tool_call_id": str(tool_call.get("id", "")),
                            "tool_name": str(function_block.get("name", "")),
                            "tool_arguments": tool_arguments,
                        }
                    )

                def execute_tool_item(item: dict[str, Any]) -> tuple[dict[str, Any], Any]:
                    result = self.custom_call_tool(
                        str(item["tool_name"]),
                        item["tool_arguments"],
                        workspace_root=resolved_workspace_root,
                        runtime_deadline=runtime_deadline,
                        model_name=self.model,
                    )
                    return item, result

                for batch_indexes in tool_execution_batches([str(item["tool_name"]) for item in tool_call_items]):
                    if remaining_runtime_seconds(runtime_deadline) is not None and remaining_runtime_seconds(runtime_deadline) <= 0:
                        result_text = "No result found before the maximum agent runtime limit."
                        termination = f"agent runtime limit reached: {agent_runtime_limit}s"
                        return finalize(result_text, termination, error=termination)
                    batch_items = [tool_call_items[index] for index in batch_indexes]
                    try:
                        should_run_parallel = len(batch_items) > 1 and all(
                            can_parallelize_tool_name(str(item["tool_name"])) for item in batch_items
                        )
                        if should_run_parallel:
                            max_workers = min(MAX_PARALLEL_READ_TOOL_CALLS, len(batch_items))
                            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                                batch_results = list(executor.map(execute_tool_item, batch_items))
                        else:
                            batch_results = [execute_tool_item(item) for item in batch_items]
                    except KeyboardInterrupt:
                        messages = messages[:tool_turn_message_start]
                        return finalize_interrupted()

                    for item, result in batch_results:
                        tool_call_id = str(item["tool_call_id"])
                        tool_name = str(item["tool_name"])
                        tool_arguments = item["tool_arguments"]
                        tool_result_text = tool_result_message_content(result)
                        messages.append(api_tool_message(tool_call_id, result))
                        trace_writer.append(
                            role="tool",
                            text=tool_result_text,
                            turn_index=round_index,
                            tool_call_ids=[tool_call_id],
                            tool_names=[tool_name],
                            tool_arguments=[tool_arguments],
                        )
                        extra_image_context = image_context_message(result, self.model)
                        if extra_image_context is not None:
                            deferred_image_contexts.append((tool_call_id, tool_name, tool_arguments, result, extra_image_context))
                for tool_call_id, tool_name, tool_arguments, result, extra_image_context in deferred_image_contexts:
                    messages.append(extra_image_context)
                    trace_writer.append(
                        role="user",
                        text=image_context_trace_text(result),
                        turn_index=round_index,
                        tool_call_ids=[tool_call_id],
                        tool_names=[tool_name],
                        tool_arguments=[tool_arguments],
                        image_paths=image_trace_paths(result),
                    )
                    if remaining_runtime_seconds(runtime_deadline) is not None and remaining_runtime_seconds(runtime_deadline) <= 0:
                        result_text = "No result found before the maximum agent runtime limit."
                        termination = f"agent runtime limit reached: {agent_runtime_limit}s"
                        return finalize(result_text, termination, error=termination)
                persist_state()
                if interruption_requested():
                    return finalize_interrupted()
            elif assistant_has_meaningful_text(assistant_content):
                current_result_text = assistant_text.strip()
                messages.append(
                    assistant_history_message(
                        content=current_result_text,
                        reasoning_content=assistant_reasoning,
                        raw_message=assistant_raw_message,
                    )
                )
                should_accept_result = self.should_accept_plaintext_result(
                    result_text=current_result_text,
                    workspace_root=resolved_workspace_root,
                    messages=messages,
                )
                if should_accept_result:
                    return finalize(current_result_text, "result", role="assistant")
                protocol_error = "plain result rejected by additional stop condition"
                trace_writer.append(
                    role="assistant",
                    text=current_result_text,
                    turn_index=round_index,
                    finish_reason=finish_reason,
                    error=protocol_error,
                )
                correction_text = self.rejected_plaintext_result_message(
                    result_text=current_result_text,
                    workspace_root=resolved_workspace_root,
                    messages=messages,
                ).strip()
                if not correction_text:
                    correction_text = (
                        "The previous assistant turn was not accepted as the final result because the additional stop condition returned false. "
                        "Continue working. If the task is incomplete, use tool calls to produce the required artifacts before finishing."
                    )
                messages.append({"role": "user", "content": correction_text})
                trace_writer.append(role="user", text=correction_text, turn_index=round_index)
                persist_state(error=protocol_error)
                continue
            else:
                protocol_error = "assistant emitted empty response"
                trace_writer.append(
                    role="assistant",
                    text="",
                    turn_index=round_index,
                    finish_reason=finish_reason,
                    error=protocol_error,
                )
                retry_assistant_message = assistant_retry_history_message(
                    content=assistant_content,
                    reasoning_content=assistant_reasoning,
                )
                if retry_assistant_message is not None:
                    messages.append(retry_assistant_message)
                correction_text = (
                    "Error: The previous assistant turn was empty. "
                    "If tools are needed, use native tool calling. Otherwise return the final result text."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": correction_text,
                    }
                )
                trace_writer.append(role="user", text=correction_text, turn_index=round_index)
                persist_state(error=protocol_error)
                continue

            token_count = self.count_tokens(messages)
            if debug_enabled():
                print(f"round: {round_index}, token count: {token_count}")
            persist_state()

        result_text = 'No result found.'
        termination = 'result not found'
        if round_index >= self.max_rounds:
            termination = 'exceed available rounds'
        elif num_llm_calls_available == 0:
            termination = 'exceed available llm calls'
        return finalize(result_text, termination, error=termination)

    def custom_call_tool(self, tool_name: str, tool_args: Any, **kwargs):
        return execute_tool_by_name(self.tool_map, tool_name, tool_args, **kwargs)


def _path_has_suffix(path: Path, suffix_parts: Sequence[str]) -> bool:
    normalized_parts = tuple(part.casefold() for part in path.parts)
    normalized_suffix = tuple(part.casefold() for part in suffix_parts)
    if len(normalized_parts) < len(normalized_suffix):
        return False
    return normalized_parts[-len(normalized_suffix) :] == normalized_suffix


def resolve_agent_class_for_role_prompt_files(role_prompt_files: Sequence[str]) -> Type[MultiTurnReactAgent]:
    for raw_path in role_prompt_files:
        path_text = str(raw_path).strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser().resolve(strict=False)
        if _path_has_suffix(path, ("benchmarks", "ResearchClawBench", "role_prompt.md")):
            from benchmarks.ResearchClawBench.adapter import ResearchClawBenchAgent

            return ResearchClawBenchAgent
    return MultiTurnReactAgent


def _parse_cli_args(argv: list[str]) -> tuple[str, Optional[str], Optional[str], str, list[str], list[str], Optional[bool], list[str]]:
    parser = argparse.ArgumentParser(description="Run the local agent directly from agent_base.react_agent.")
    parser.add_argument("prompt", nargs="*", help="Prompt text.")
    parser.add_argument("--prompt-file", help="Optional UTF-8 text file containing the prompt.")
    parser.add_argument("--trace-dir", help="Optional directory where the run trace JSONL should be created.")
    parser.add_argument(
        "--workspace-root",
        help="Optional workspace root for local file tools, Bash, and TerminalStart.",
    )
    parser.add_argument(
        "--role-prompt-file",
        action="append",
        default=[],
        dest="role_prompt_files",
        metavar="PATH",
        help="Append one role-specific prompt file to the base system prompt. May be passed multiple times.",
    )
    parser.add_argument(
        "--images",
        action="append",
        nargs="+",
        default=[],
        dest="image_paths",
        metavar="PATH",
        help="Attach one or more local image paths to the initial user message.",
    )
    parser.add_argument(
        "--chat",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Continue asking for follow-up user messages after each final answer. Defaults to on only in an interactive terminal.",
    )
    parser.add_argument(
        "--extra-tool",
        action="append",
        default=[],
        dest="extra_tools",
        metavar="NAME",
        help="Enable one optional extra tool for this run. Currently supported: str_replace_editor. May be passed multiple times.",
    )
    args = parser.parse_args(argv)

    prompt_text = ""
    if args.prompt_file:
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    elif args.prompt:
        prompt_text = " ".join(args.prompt).strip()

    if not prompt_text:
        raise ValueError("A non-empty prompt is required via positional args or --prompt-file.")
    role_prompt = read_role_prompt_files(args.role_prompt_files)
    return (
        prompt_text,
        args.trace_dir,
        args.workspace_root,
        role_prompt,
        list(args.role_prompt_files),
        [path for group in args.image_paths for path in group],
        args.chat,
        resolve_extra_tool_names(args.extra_tools),
    )


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        require_required_env("ResearchHarness agent")
        prompt_text, trace_dir, workspace_root, role_prompt, role_prompt_files, image_paths, chat_arg, extra_tools = _parse_cli_args(argv or sys.argv[1:])
        agent_cls = resolve_agent_class_for_role_prompt_files(role_prompt_files)
        forbidden_tools = set(getattr(agent_cls, "forbidden_tool_names", set()))
        agent = agent_cls(
            function_list=(
                default_tool_names(include_ask_user="AskUser" not in forbidden_tools, extra_tools=extra_tools)
                if extra_tools
                else None
            ),
            llm=default_llm_config(),
            trace_dir=trace_dir,
            role_prompt=role_prompt or None,
        )
        resolved_workspace_root = normalize_workspace_root(workspace_root)
        initial_content_parts: list[dict[str, Any]] = []
        saved_image_paths: list[str] = []
        for image_index, image_path in enumerate(image_paths):
            saved_path, data_url = stage_image_file_for_input(
                image_path,
                workspace_root=resolved_workspace_root,
                image_index=image_index,
            )
            saved_image_paths.append(saved_path)
            initial_content_parts.extend(image_input_content_parts(data_url, saved_path))
        run_prompt = append_saved_image_paths_to_prompt(prompt_text, saved_image_paths)
        printer = ConsoleEventPrinter(
            model_name=agent.model,
            workspace_root=resolved_workspace_root,
            prompt=run_prompt,
        )
        printer.print_header()
        session = agent._run_session(
            run_prompt,
            workspace_root=str(resolved_workspace_root),
            event_callback=printer.handle_event,
            initial_content_parts=initial_content_parts or None,
        )
        chat_enabled = chat_arg if chat_arg is not None else (sys.stdin.isatty() and sys.stdout.isatty())
        messages = session.get("messages", [])
        while chat_enabled:
            try:
                followup = input("\n[ResearchHarness] Follow-up (Ctrl+C to exit): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n[ResearchHarness] Chat ended.")
                break
            if not followup:
                continue
            print(f"\n[ResearchHarness] Continuing conversation: {followup}")
            printer.reset_rounds()
            session = agent._run_session(
                followup,
                workspace_root=str(resolved_workspace_root),
                event_callback=printer.handle_event,
                prior_messages=messages,
            )
            messages = session.get("messages", messages)
        return 0
    except (MissingRequiredEnvError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
