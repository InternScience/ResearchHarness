from __future__ import annotations

import base64
import binascii
import datetime
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from agent_base.react_agent import (
    AVAILABLE_TOOL_MAP,
    MultiTurnReactAgent,
    assistant_text_content,
    default_llm_config,
    model_supports_runtime_image_parts,
)
from agent_base.tools.tooling import normalize_workspace_root
from agent_base.utils import append_jsonl, safe_jsonable


DATA_IMAGE_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
DEFAULT_MAX_IMAGE_BYTES = 25 * 1024 * 1024

INPUT_WRAPPER_SYSTEM_PROMPT = """You are the ResearchHarness input wrapper.

Convert the user's OpenAI-compatible chat request into a stable task for a
tool-using ResearchHarness agent.

Return only a JSON object with these string fields:
- agent_instruction: the task the agent should solve, including all substantive question details.
- output_contract: the final output format or schema requested by the user. If no strict format is requested, say "plain text".
- wrapper_notes: brief notes about images, constraints, or benchmark-specific requirements.

Rules:
- Do not answer the task.
- Do not remove substantive constraints.
- Keep strict final formatting requirements out of agent_instruction when possible.
- If images are listed, mention their saved paths in agent_instruction.
"""

OUTPUT_WRAPPER_SYSTEM_PROMPT = """You are the ResearchHarness output wrapper.

Format the ResearchHarness agent result so it satisfies the user's requested
final output contract.

Rules:
- Return only the final answer requested by the user.
- Do not add markdown fences unless the user explicitly required them.
- Do not solve the task again.
- Do not introduce facts not present in the agent result.
- If the requested format is JSON, return valid JSON only.
- If the agent result does not contain enough information, produce the best
  contract-compliant failure answer instead of inventing evidence.
"""


class OpenAICompatError(Exception):
    def __init__(self, status_code: int, message: str, error_type: str = "invalid_request_error"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type


@dataclass
class ServerConfig:
    api_runs_dir: Path
    role_prompt: str = ""
    host: str = "127.0.0.1"
    port: int = 8686
    input_wrapper: bool = True
    output_wrapper: bool = True


@dataclass
class PreparedInput:
    wrapper_messages: list[dict[str, str]]
    initial_content_parts: list[dict[str, Any]]
    image_paths: list[str]


def openai_error_response(exc: OpenAICompatError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": exc.error_type}},
    )


def read_role_prompt_files(paths: list[str]) -> str:
    blocks: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        blocks.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(block for block in blocks if block)


def make_chat_completion_response(*, request_id: str, model: str, content: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def validate_chat_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OpenAICompatError(400, "Request body must be a JSON object.")
    if payload.get("stream") is True:
        raise OpenAICompatError(400, "Streaming is not supported by this synchronous endpoint.")
    try:
        n_value = int(payload.get("n", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise OpenAICompatError(400, "n must be an integer.") from exc
    if n_value != 1:
        raise OpenAICompatError(400, "Only n=1 is supported.")
    model = str(payload.get("model", "")).strip()
    if not model:
        raise OpenAICompatError(400, "model is required.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAICompatError(400, "messages must be a non-empty list.")
    return payload


def prepare_openai_input(messages: list[Any], workspace_root: Path) -> PreparedInput:
    wrapper_messages: list[dict[str, str]] = []
    initial_content_parts: list[dict[str, Any]] = []
    image_paths: list[str] = []
    image_dir = workspace_root / "inputs" / "images"
    image_index = 0

    for message in messages:
        if not isinstance(message, dict):
            raise OpenAICompatError(400, "Each message must be an object.")
        role = str(message.get("role", "")).strip()
        if role not in {"system", "user", "assistant"}:
            raise OpenAICompatError(400, f"Unsupported message role: {role!r}.")
        content = message.get("content", "")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    raise OpenAICompatError(400, "Multimodal content parts must be objects.")
                part_type = str(part.get("type", "")).strip()
                if part_type == "text":
                    text_parts.append(str(part.get("text", "")))
                elif part_type == "image_url":
                    image_url = part.get("image_url")
                    if not isinstance(image_url, dict):
                        raise OpenAICompatError(400, "image_url content must contain an image_url object.")
                    url = str(image_url.get("url", "")).strip()
                    detail = str(image_url.get("detail", "auto") or "auto")
                    rel_path = save_data_image(
                        url,
                        workspace_root=workspace_root,
                        image_dir=image_dir,
                        image_index=image_index,
                    )
                    image_index += 1
                    image_paths.append(rel_path)
                    text_parts.append(f"[image saved at {rel_path}]")
                    initial_content_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": url, "detail": detail},
                        }
                    )
                else:
                    raise OpenAICompatError(400, f"Unsupported content part type: {part_type!r}.")
        else:
            raise OpenAICompatError(400, "message content must be a string or a list of content parts.")
        wrapper_messages.append({"role": role, "content": "\n".join(part for part in text_parts if part)})

    return PreparedInput(
        wrapper_messages=wrapper_messages,
        initial_content_parts=initial_content_parts,
        image_paths=image_paths,
    )


def save_data_image(url: str, *, workspace_root: Path, image_dir: Path, image_index: int) -> str:
    match = DATA_IMAGE_RE.match(url)
    if not match:
        raise OpenAICompatError(
            400,
            "Only data:image/...;base64,... image_url inputs are supported in the first API version.",
        )
    mime_type = match.group(1).lower()
    extension = IMAGE_EXTENSIONS.get(mime_type)
    if extension is None:
        raise OpenAICompatError(400, f"Unsupported image MIME type: {mime_type}.")
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OpenAICompatError(400, "Invalid base64 image data.") from exc
    if len(image_bytes) > DEFAULT_MAX_IMAGE_BYTES:
        raise OpenAICompatError(400, f"Image exceeds the {DEFAULT_MAX_IMAGE_BYTES} byte limit.")
    image_dir.mkdir(parents=True, exist_ok=True)
    filename = f"image_{image_index:03d}{extension}"
    path = image_dir / filename
    path.write_bytes(image_bytes)
    return str(path.relative_to(workspace_root))


def wrapper_request_payload(*, prepared: PreparedInput, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": prepared.wrapper_messages,
        "saved_image_paths": prepared.image_paths,
        "response_format": safe_jsonable(payload.get("response_format")),
        "requested_model_label": str(payload.get("model", "")),
    }


def build_input_wrapper_messages(*, prepared: PreparedInput, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": INPUT_WRAPPER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(wrapper_request_payload(prepared=prepared, payload=payload), ensure_ascii=False, indent=2),
        },
    ]


def build_passthrough_input_plan(*, prepared: PreparedInput, payload: dict[str, Any]) -> dict[str, str]:
    conversation = "\n\n".join(
        f"{message['role'].upper()}:\n{message['content']}" for message in prepared.wrapper_messages
    ).strip()
    response_format = payload.get("response_format")
    output_contract = "Follow the final answer requirements in the original request."
    if response_format is not None:
        output_contract += "\nOpenAI response_format request:\n" + json.dumps(
            safe_jsonable(response_format),
            ensure_ascii=False,
            indent=2,
        )
    return {
        "agent_instruction": conversation or "Answer the user's request.",
        "output_contract": output_contract,
        "wrapper_notes": "Input wrapper disabled; the original normalized conversation was passed through directly.",
    }


def build_agent_prompt(input_plan: dict[str, Any], prepared: PreparedInput) -> str:
    image_block = "\n".join(f"- {path}" for path in prepared.image_paths) if prepared.image_paths else "- none"
    return (
        "You are solving a QA benchmark request through ResearchHarness.\n\n"
        "Task for the agent:\n"
        f"{str(input_plan.get('agent_instruction', '')).strip()}\n\n"
        "User-provided images saved in this workspace:\n"
        f"{image_block}\n\n"
        "The original image content is attached to the initial user message when the backend model supports image parts. "
        "The same images are also saved at the paths above so you may call ReadImage when visual inspection is needed.\n\n"
        "Do not optimize your tool-use loop for the final output schema. Solve the task completely, then finish with a concise "
        "summary of what you did, the evidence used, and the final answer.\n\n"
        "Final output contract that will be enforced by a formatter after your run:\n"
        f"{str(input_plan.get('output_contract', 'plain text')).strip()}\n\n"
        "Wrapper notes:\n"
        f"{str(input_plan.get('wrapper_notes', '')).strip()}"
    )


def build_output_wrapper_messages(
    *,
    prepared: PreparedInput,
    payload: dict[str, Any],
    input_plan: dict[str, Any],
    agent_result_text: str,
) -> list[dict[str, str]]:
    output_payload = {
        "original_messages": prepared.wrapper_messages,
        "saved_image_paths": prepared.image_paths,
        "output_contract": str(input_plan.get("output_contract", "plain text")),
        "response_format": safe_jsonable(payload.get("response_format")),
        "agent_result_text": agent_result_text,
    }
    return [
        {"role": "system", "content": OUTPUT_WRAPPER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(output_payload, ensure_ascii=False, indent=2)},
    ]


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise OpenAICompatError(500, "Input wrapper did not return a JSON object.", "server_error") from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OpenAICompatError(500, f"Input wrapper returned invalid JSON: {exc}", "server_error") from exc
    if not isinstance(parsed, dict):
        raise OpenAICompatError(500, "Input wrapper JSON must be an object.", "server_error")
    if not str(parsed.get("agent_instruction", "")).strip():
        raise OpenAICompatError(500, "Input wrapper JSON missing agent_instruction.", "server_error")
    if not str(parsed.get("output_contract", "")).strip():
        parsed["output_contract"] = "plain text"
    parsed.setdefault("wrapper_notes", "")
    return parsed


def call_wrapper_text(
    agent: MultiTurnReactAgent,
    messages: list[dict[str, str]],
    *,
    max_output_tokens: Optional[int] = None,
) -> str:
    response = agent.call_compaction_api(messages, max_output_tokens=max_output_tokens)
    if not isinstance(response, dict) or response.get("status") == "error":
        error_text = response.get("error", "unknown wrapper error") if isinstance(response, dict) else str(response)
        raise OpenAICompatError(500, error_text, "server_error")
    text = assistant_text_content(response.get("content")).strip()
    if not text:
        raise OpenAICompatError(500, "Wrapper returned empty content.", "server_error")
    return text


def final_max_tokens(payload: dict[str, Any]) -> Optional[int]:
    raw_value = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise OpenAICompatError(400, "max_tokens must be an integer.") from exc
    if value <= 0:
        raise OpenAICompatError(400, "max_tokens must be positive.")
    return value


def append_api_event(workspace_root: Path, event: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        workspace_root / "api_trace.jsonl",
        {
            "timestamp": int(time.time()),
            "event": event,
            "payload": safe_jsonable(payload),
        },
    )


def run_chat_completion(payload: dict[str, Any], config: ServerConfig) -> dict[str, Any]:
    payload = validate_chat_payload(payload)
    request_id = "chatcmpl_" + uuid4().hex
    run_id = "run_" + datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    run_root = config.api_runs_dir / run_id
    agent_workspace = run_root / "agent_workspace"
    trace_dir = run_root / "agent_traces"
    agent_workspace.mkdir(parents=True, exist_ok=False)
    trace_dir.mkdir(parents=True, exist_ok=False)
    prepared = prepare_openai_input(payload["messages"], agent_workspace)
    llm_config = default_llm_config()
    backend_model = str(llm_config.get("model", ""))
    if prepared.initial_content_parts and not model_supports_runtime_image_parts(backend_model):
        raise OpenAICompatError(
            400,
            f"Backend model {backend_model!r} does not support image content parts.",
        )

    tool_names = [name for name in AVAILABLE_TOOL_MAP if name != "AskUser"]
    agent = MultiTurnReactAgent(
        function_list=tool_names,
        llm=llm_config,
        trace_dir=str(trace_dir),
        role_prompt=config.role_prompt or None,
    )

    if config.input_wrapper:
        input_wrapper_messages = build_input_wrapper_messages(prepared=prepared, payload=payload)
        input_wrapper_text = call_wrapper_text(agent, input_wrapper_messages, max_output_tokens=1200)
        input_plan = extract_json_object(input_wrapper_text)
        append_api_event(
            trace_dir,
            "input_wrapper",
            {
                "enabled": True,
                "request": input_wrapper_messages,
                "response_text": input_wrapper_text,
                "input_plan": input_plan,
            },
        )
    else:
        input_plan = build_passthrough_input_plan(prepared=prepared, payload=payload)
        append_api_event(
            trace_dir,
            "input_wrapper",
            {
                "enabled": False,
                "input_plan": input_plan,
            },
        )

    agent_prompt = build_agent_prompt(input_plan, prepared)
    session = agent._run_session(
        agent_prompt,
        workspace_root=str(agent_workspace),
        initial_content_parts=prepared.initial_content_parts or None,
    )
    agent_result_text = str(session.get("result_text", "")).strip()
    append_api_event(
        trace_dir,
        "agent_result",
        {
            "termination": session.get("termination", ""),
            "result_text": agent_result_text,
            "trace_path": session.get("trace_path", ""),
        },
    )

    if config.output_wrapper:
        output_wrapper_messages = build_output_wrapper_messages(
            prepared=prepared,
            payload=payload,
            input_plan=input_plan,
            agent_result_text=agent_result_text,
        )
        final_text = call_wrapper_text(agent, output_wrapper_messages, max_output_tokens=final_max_tokens(payload))
        append_api_event(
            trace_dir,
            "output_wrapper",
            {
                "enabled": True,
                "request": output_wrapper_messages,
                "response_text": final_text,
            },
        )
    else:
        final_text = agent_result_text
        append_api_event(
            trace_dir,
            "output_wrapper",
            {
                "enabled": False,
                "response_text": final_text,
            },
        )
    return make_chat_completion_response(
        request_id=request_id,
        model=str(payload.get("model", "researchharness")),
        content=final_text,
    )


def create_app(config: ServerConfig) -> FastAPI:
    app = FastAPI(title="ResearchHarness OpenAI-Compatible API", version="1.0")

    @app.exception_handler(OpenAICompatError)
    async def _handle_openai_compat_error(request: Request, exc: OpenAICompatError) -> JSONResponse:
        return openai_error_response(exc)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "api_runs_dir": str(config.api_runs_dir),
            "input_wrapper": config.input_wrapper,
            "output_wrapper": config.output_wrapper,
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return run_chat_completion(payload, config)
        except OpenAICompatError:
            raise
        except Exception as exc:
            raise OpenAICompatError(500, f"ResearchHarness API error: {exc}", "server_error") from exc

    return app


def serve(
    *,
    api_runs_dir: str,
    host: str = "127.0.0.1",
    port: int = 8686,
    role_prompt_files: Optional[list[str]] = None,
    input_wrapper: bool = True,
    output_wrapper: bool = True,
) -> None:
    root = normalize_workspace_root(api_runs_dir)
    role_prompt = read_role_prompt_files(role_prompt_files or [])
    config = ServerConfig(
        api_runs_dir=root,
        role_prompt=role_prompt,
        host=host,
        port=port,
        input_wrapper=input_wrapper,
        output_wrapper=output_wrapper,
    )
    app = create_app(config)
    uvicorn.run(app, host=host, port=port)
