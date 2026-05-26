from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import os
import re
import threading
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_base.react_agent import (
    MultiTurnReactAgent,
    default_llm_config,
    default_tool_names,
    resolve_extra_tool_names,
)
from agent_base.utils import (
    MissingRequiredEnvError,
    PROJECT_ROOT,
    append_saved_image_paths_to_prompt,
    image_input_content_parts,
    load_default_dotenvs,
    require_required_env,
    safe_jsonable,
    stage_image_bytes_for_input,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_IMAGES = 12
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 800
FRONTEND_ROLE_PROMPT = ""
FRONTEND_TRACE_DIR: str | None = None
FRONTEND_EXTRA_TOOLS: tuple[str, ...] = ()
_FILE_WORKSPACES: dict[str, str] = {}
_FILE_WORKSPACES_LOCK = threading.Lock()

app = FastAPI(title="ResearchHarness Local UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="frontend-static")


def configure_frontend(
    *,
    role_prompt: str = "",
    trace_dir: str | None = None,
    extra_tools: list[str] | None = None,
) -> None:
    global FRONTEND_ROLE_PROMPT, FRONTEND_TRACE_DIR, FRONTEND_EXTRA_TOOLS
    FRONTEND_ROLE_PROMPT = str(role_prompt or "").strip()
    FRONTEND_EXTRA_TOOLS = tuple(resolve_extra_tool_names(extra_tools or []))
    if trace_dir:
        path = Path(trace_dir).expanduser()
        if path.exists() and not path.is_dir():
            raise ValueError(f"trace-dir is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        FRONTEND_TRACE_DIR = str(path)
    else:
        FRONTEND_TRACE_DIR = None


class FrontendRunBridge:
    def __init__(self, *, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.cancelled = threading.Event()
        self.conversation_messages: list[dict[str, Any]] | None = None
        self.conversation_workspace_root: str = ""
        self.file_token: str = ""
        self._pending_answers: dict[str, str] = {}
        self._pending_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.outbound.put_nowait, safe_jsonable(payload))

    def trace_event(self, row: dict[str, Any]) -> None:
        self.send({"type": "trace", "row": row})

    def submit_answer(self, request_id: str, answer: str) -> bool:
        with self._lock:
            event = self._pending_events.get(request_id)
            if event is None:
                return False
            self._pending_answers[request_id] = str(answer)
            event.set()
            return True

    def ask_user(self, *, question: str, context: str = "") -> str:
        request_id = uuid4().hex
        event = threading.Event()
        with self._lock:
            self._pending_events[request_id] = event
        self.send(
            {
                "type": "ask_user",
                "request_id": request_id,
                "question": question,
                "context": context,
            }
        )
        while not event.wait(0.2):
            if self.cancelled.is_set():
                return "[AskUser] Cancelled before user answer was received."
        with self._lock:
            answer = self._pending_answers.pop(request_id, "")
            self._pending_events.pop(request_id, None)
        answer = str(answer).strip()
        if not answer:
            return "[AskUser] User answer was empty."
        return f"[AskUser] User answer:\n{answer}"


def _register_file_workspace(workspace_root: Path, token: str = "") -> str:
    token = token or uuid4().hex
    with _FILE_WORKSPACES_LOCK:
        _FILE_WORKSPACES[token] = str(workspace_root.resolve())
    return token


def _unregister_file_workspace(token: str) -> None:
    if not token:
        return
    with _FILE_WORKSPACES_LOCK:
        _FILE_WORKSPACES.pop(token, None)


def _workspace_for_file_token(token: str) -> Path:
    with _FILE_WORKSPACES_LOCK:
        workspace_text = _FILE_WORKSPACES.get(str(token or ""))
    if not workspace_text:
        raise HTTPException(status_code=404, detail="No workspace file token is available for this chat.")
    workspace_root = Path(workspace_text).resolve()
    if not workspace_root.is_dir():
        raise HTTPException(status_code=404, detail="The workspace is no longer available.")
    return workspace_root


def _resolve_workspace_file_path(workspace_root: Path, raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if text.startswith("file://"):
        text = text[7:]
    text = unquote(text)
    if not text:
        raise HTTPException(status_code=400, detail="workspace file path is required")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace_root / text
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="workspace file path is outside the workspace") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="workspace file does not exist")
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        raise HTTPException(status_code=415, detail="only workspace image files can be displayed inline")
    return resolved


class FrontendInteractiveAgent(MultiTurnReactAgent):
    def __init__(self, *, bridge: FrontendRunBridge, **kwargs: Any):
        super().__init__(**kwargs)
        self.bridge = bridge

    def custom_call_tool(self, tool_name: str, tool_args: Any, **kwargs: Any):
        if tool_name != "AskUser":
            return super().custom_call_tool(tool_name, tool_args, **kwargs)
        tool = self.tool_map.get("AskUser")
        if tool is None:
            return "[AskUser] Tool is not available in this run."
        try:
            parsed = tool.parse_json_args(tool_args)
        except ValueError as exc:
            return f"[AskUser] {exc}"
        question = str(parsed.get("question", "")).strip()
        context = str(parsed.get("context", "") or "").strip()
        if not question:
            return "[AskUser] question must be a non-empty string."
        return self.bridge.ask_user(question=question, context=context)


def _safe_image_suffix(mime: str, filename: str = "") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return suffix
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    return mapping.get(mime.lower(), ".png")


def decode_image_data_url(data_url: str, *, filename: str = "") -> tuple[str, bytes]:
    match = re.fullmatch(r"data:(image/[A-Za-z0-9.+-]+);base64,(.*)", str(data_url), flags=re.DOTALL)
    if not match:
        raise ValueError("image must be a data:image/...;base64,... URL")
    mime = match.group(1)
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise ValueError(f"invalid base64 image data: {exc}") from exc
    if not raw:
        raise ValueError("image upload is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image upload exceeds {MAX_IMAGE_BYTES} bytes")
    return _safe_image_suffix(mime, filename), raw


def save_uploaded_images(workspace_root: Path, images: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    if len(images) > MAX_UPLOAD_IMAGES:
        raise ValueError(f"at most {MAX_UPLOAD_IMAGES} images are supported per run")
    if not images:
        return [], []
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    content_parts: list[dict[str, Any]] = []
    saved_paths: list[str] = []
    for idx, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            raise ValueError("each image item must be an object")
        data_url = str(item.get("data_url", "")).strip()
        filename = str(item.get("name", "") or f"image_{idx}")
        suffix, raw = decode_image_data_url(data_url, filename=filename)
        saved_path = stage_image_bytes_for_input(
            raw,
            workspace_root=workspace_root,
            filename=f"{timestamp}_{filename}",
            image_index=idx - 1,
            suffix=suffix,
        )
        saved_paths.append(saved_path)
        content_parts.extend(image_input_content_parts(data_url, saved_path))
    return content_parts, saved_paths


def _prompt_with_uploaded_image_paths(prompt: str, saved_paths: list[str]) -> str:
    return append_saved_image_paths_to_prompt(prompt, saved_paths)


def _run_agent_thread(
    *,
    bridge: FrontendRunBridge,
    prompt: str,
    workspace_root: Path,
    initial_content_parts: list[dict[str, Any]],
    prior_messages: list[dict[str, Any]] | None = None,
    model_name: str = "",
) -> None:
    try:
        load_default_dotenvs()
        require_required_env("ResearchHarness frontend")
        agent = FrontendInteractiveAgent(
            bridge=bridge,
            function_list=default_tool_names(extra_tools=FRONTEND_EXTRA_TOOLS) if FRONTEND_EXTRA_TOOLS else None,
            llm=default_llm_config(model_name=model_name or None),
            trace_dir=FRONTEND_TRACE_DIR,
            role_prompt=FRONTEND_ROLE_PROMPT or None,
        )
        bridge.send(
            {
                "type": "run_started",
                "model": agent.model,
                "workspace_root": str(workspace_root),
                "trace_dir": FRONTEND_TRACE_DIR or "",
                "file_token": bridge.file_token,
            }
        )
        result = agent._run_session(
            prompt,
            workspace_root=str(workspace_root),
            event_callback=bridge.trace_event,
            initial_content_parts=initial_content_parts or None,
            prior_messages=prior_messages,
            interrupt_event=bridge.cancelled,
        )
        bridge.conversation_messages = result.get("messages", [])
        bridge.conversation_workspace_root = str(workspace_root)
        bridge.send(
            {
                "type": "run_finished",
                "result_text": result.get("result_text", ""),
                "termination": result.get("termination", ""),
            }
        )
    except (MissingRequiredEnvError, ValueError) as exc:
        bridge.send({"type": "run_error", "error": str(exc)})
    except Exception as exc:
        bridge.send({"type": "run_error", "error": str(exc), "traceback": traceback.format_exc()})


def _resolve_existing_workspace(raw_path: str) -> Path:
    if not str(raw_path or "").strip():
        raise ValueError("workspace path is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"workspace must be an existing directory: {path}")
    return path


def _resolve_directory_browser_path(raw_path: str = "") -> Path:
    text = str(raw_path or "").strip()
    if text:
        path = Path(text).expanduser()
    else:
        path = Path.home() if Path.home().exists() else PROJECT_ROOT
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"directory does not exist: {path}")
    return path


def _directory_root_choices() -> list[dict[str, str]]:
    candidates = [Path.home(), PROJECT_ROOT, PROJECT_ROOT / "workspace", Path.cwd(), Path("/mnt"), Path("/")]
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            candidates.append(Path(f"{letter}:\\"))

    seen: set[str] = set()
    roots: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        label = "Home" if resolved == Path.home().resolve() else (resolved.name or key)
        roots.append({"label": label, "path": key})
    return roots


def _workspace_directory_payload(raw_path: str = "") -> dict[str, Any]:
    directory = _resolve_directory_browser_path(raw_path)
    entries: list[dict[str, str]] = []
    truncated = False
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except PermissionError as exc:
        raise ValueError(f"permission denied: {directory}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read directory {directory}: {exc}") from exc

    for child in children:
        if len(entries) >= MAX_DIRECTORY_ENTRIES:
            truncated = True
            break
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({"name": child.name or str(child), "path": str(child)})

    parent = directory.parent if directory.parent != directory else None
    return {
        "path": str(directory),
        "parent": str(parent) if parent else "",
        "entries": entries,
        "truncated": truncated,
        "roots": _directory_root_choices(),
    }


@app.get("/api/workspace-directories")
def workspace_directories(path: str = "") -> JSONResponse:
    try:
        return JSONResponse(_workspace_directory_payload(path))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/workspace-file")
def workspace_file(token: str, path: str) -> FileResponse:
    workspace_root = _workspace_for_file_token(token)
    return FileResponse(_resolve_workspace_file_path(workspace_root, path))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    bridge = FrontendRunBridge(loop=asyncio.get_running_loop())
    run_thread: threading.Thread | None = None

    async def sender() -> None:
        while True:
            payload = await bridge.outbound.get()
            await websocket.send_json(payload)

    sender_task = asyncio.create_task(sender())
    try:
        await websocket.send_json({"type": "ready"})
        while True:
            message = await websocket.receive_json()
            message_type = str(message.get("type", "")).strip()
            if message_type == "start":
                if run_thread is not None and run_thread.is_alive():
                    bridge.send({"type": "run_error", "error": "A run is already active. Wait for it to finish before starting a new conversation."})
                    continue
                prompt = str(message.get("prompt", "")).strip()
                if not prompt:
                    bridge.send({"type": "run_error", "error": "Prompt is required."})
                    continue
                try:
                    workspace_root = _resolve_existing_workspace(str(message.get("workspace_root", "")))
                    image_parts, saved_paths = save_uploaded_images(
                        workspace_root,
                        message.get("images", []) if isinstance(message.get("images", []), list) else [],
                    )
                    run_prompt = _prompt_with_uploaded_image_paths(prompt, saved_paths)
                    continue_conversation = bool(message.get("continue_conversation"))
                    model_name = str(message.get("model_name", "") or "").strip()
                    prior_messages = None
                    if continue_conversation:
                        if not bridge.conversation_messages:
                            bridge.send({"type": "run_error", "error": "No active conversation is available on the server. Click New chat and start again."})
                            continue
                        elif bridge.conversation_workspace_root and bridge.conversation_workspace_root != str(workspace_root):
                            bridge.send({"type": "run_error", "error": "Workspace changed. Start a new chat before using a different workspace."})
                            continue
                        else:
                            prior_messages = bridge.conversation_messages
                except ValueError as exc:
                    bridge.send({"type": "run_error", "error": str(exc)})
                    continue
                bridge.file_token = _register_file_workspace(workspace_root, bridge.file_token)
                bridge.cancelled.clear()
                if not continue_conversation:
                    bridge.conversation_messages = None
                    bridge.conversation_workspace_root = str(workspace_root)
                    bridge.send({"type": "conversation_reset"})
                if saved_paths:
                    bridge.send({"type": "uploaded_images", "paths": saved_paths})
                run_thread = threading.Thread(
                    target=_run_agent_thread,
                    kwargs={
                        "bridge": bridge,
                        "prompt": run_prompt,
                        "workspace_root": workspace_root,
                        "initial_content_parts": image_parts,
                        "prior_messages": prior_messages,
                        "model_name": model_name,
                    },
                    daemon=True,
                )
                run_thread.start()
            elif message_type == "ask_user_answer":
                ok = bridge.submit_answer(str(message.get("request_id", "")), str(message.get("answer", "")))
                if not ok:
                    bridge.send({"type": "run_error", "error": "No pending AskUser request matched that answer."})
            elif message_type == "interrupt":
                if run_thread is not None and run_thread.is_alive():
                    bridge.cancelled.set()
                    bridge.send({"type": "interrupt_requested"})
                else:
                    bridge.send({"type": "run_error", "error": "No active run is available to interrupt."})
            elif message_type == "new":
                if run_thread is not None and run_thread.is_alive():
                    bridge.send({"type": "run_error", "error": "The current run is still active. Start a new conversation after it finishes."})
                else:
                    _unregister_file_workspace(bridge.file_token)
                    bridge.file_token = ""
                    bridge.conversation_messages = None
                    bridge.conversation_workspace_root = ""
                    bridge.send({"type": "conversation_reset"})
            else:
                bridge.send({"type": "run_error", "error": f"Unknown websocket message type: {message_type}"})
    except WebSocketDisconnect:
        bridge.cancelled.set()
    finally:
        bridge.cancelled.set()
        _unregister_file_workspace(bridge.file_token)
        sender_task.cancel()
