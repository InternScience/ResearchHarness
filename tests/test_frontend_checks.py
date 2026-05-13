import base64
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend.local_server import (
    configure_frontend,
    decode_image_data_url,
    save_uploaded_images,
    _resolve_existing_workspace,
    _workspace_directory_payload,
)


@dataclass
class FrontendCheckResult:
    status: str
    detail: str
    output_preview: str


def test_frontend_image_data_url_save(tmp_path: Path) -> None:
    raw = b"not really an image, but valid bytes for data-url plumbing"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    second_raw = b"second image bytes"
    second_data_url = "data:image/jpeg;base64," + base64.b64encode(second_raw).decode("ascii")

    parts, saved_paths = save_uploaded_images(
        tmp_path,
        [
            {"name": "demo image.png", "data_url": data_url},
            {"name": "second image.jpg", "data_url": second_data_url},
        ],
    )

    assert len(parts) == 4
    assert parts[0]["type"] == "text"
    assert parts[0]["text"].startswith("[User-provided image saved at inputs/images/")
    assert parts[1] == {"type": "image_url", "image_url": {"url": data_url, "detail": "auto"}}
    assert parts[2]["type"] == "text"
    assert parts[2]["text"].startswith("[User-provided image saved at inputs/images/")
    assert parts[3] == {"type": "image_url", "image_url": {"url": second_data_url, "detail": "auto"}}
    assert len(saved_paths) == 2
    assert saved_paths[0].startswith("inputs/images/")
    assert saved_paths[1].startswith("inputs/images/")
    saved_path = tmp_path / saved_paths[0]
    second_saved_path = tmp_path / saved_paths[1]
    assert saved_path.is_file()
    assert second_saved_path.is_file()
    assert saved_path.read_bytes() == raw
    assert second_saved_path.read_bytes() == second_raw
    assert saved_path.parent == tmp_path / "inputs" / "images"
    assert second_saved_path.parent == tmp_path / "inputs" / "images"


def test_frontend_rejects_non_image_data_url() -> None:
    data_url = "data:text/plain;base64," + base64.b64encode(b"hello").decode("ascii")
    try:
        decode_image_data_url(data_url)
    except ValueError as exc:
        assert "data:image" in str(exc)
    else:
        raise AssertionError("expected non-image data URL to be rejected")


def test_frontend_workspace_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing-workspace"
    try:
        _resolve_existing_workspace(str(missing))
    except ValueError as exc:
        assert "existing directory" in str(exc)
    else:
        raise AssertionError("expected missing workspace to be rejected")
    assert not missing.exists()


def test_frontend_workspace_picker_supports_unicode_paths(tmp_path: Path) -> None:
    child = tmp_path / "中文 workspace"
    child.mkdir()

    payload = _workspace_directory_payload(str(tmp_path))

    names = [entry["name"] for entry in payload["entries"]]
    paths = [entry["path"] for entry in payload["entries"]]
    assert child.name in names
    assert str(child) in paths


def test_frontend_configures_trace_dir(tmp_path: Path) -> None:
    trace_dir = tmp_path / "frontend-traces"
    configure_frontend(role_prompt="Extra role guidance.", trace_dir=str(trace_dir))

    import frontend.local_server as local_server

    assert trace_dir.is_dir()
    assert local_server.FRONTEND_ROLE_PROMPT == "Extra role guidance."
    assert local_server.FRONTEND_TRACE_DIR == str(trace_dir)
    configure_frontend()


def test_frontend_static_interaction_contract() -> None:
    html = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    launcher = (ROOT / "run_frontend.py").read_text(encoding="utf-8")
    server = (ROOT / "frontend" / "local_server.py").read_text(encoding="utf-8")

    assert "Ctrl+Enter" in html
    assert "Ctrl+Enter inserts a newline" in html
    assert "Click + to add one or more images" in html
    assert "--role-prompt-file" in launcher
    assert "--trace-dir" in launcher
    assert "configure_frontend" in launcher
    assert "role_prompt=FRONTEND_ROLE_PROMPT or None" in server
    assert "trace_dir=FRONTEND_TRACE_DIR" in server
    assert "prior_messages=prior_messages" in server
    assert "conversation_messages" in server
    assert "No active conversation is available on the server" in server
    assert "/api/workspace-directories" in js
    assert "autoFollowTimeline" in js
    assert "syncTimelineFollowMode" in js
    assert "conversationStarted" in js
    assert "continue_conversation" in js
    assert "Type a follow-up or click New chat" in js
    assert "setEventExpanded" in js
    assert "body.scrollHeight" in js
    assert "keepSubmittedMessageOnReset" in js
    assert "setEventExpanded(eventNode, false, true)" in js
    assert "sendAskUserAnswer" in js
    assert "Agent question" in js
    assert 'runBtn.textContent = "Reply"' in js
    assert 'addMessage("user", answer, [])' in js
    assert "askForm" not in js
    assert "askAnswer" not in js
    assert "askPanel" not in html
    assert 'eventNode.classList.add("collapsed")' not in js
    assert 'node.classList.contains("latest")' in js
    assert "event.isComposing" in js
    assert ".rh_frontend_inputs" not in server
    assert "send-button.is-running" in css
    assert "height: 100dvh" in css
    assert ".chat-shell > *" in css
    assert "flex: 0 0 auto" in css
    assert "overflow-y: scroll" in css
    assert "max-height: 100%" in css
    assert "transition: max-height" in css
    assert "event-body-inner" in css
    assert "ask-card" not in css
    assert ".event-body {\n  display: grid" not in css
    assert ".event.collapsed .event-body {\n  grid-template-rows" not in css
    assert ".event:not(.collapsed) .event-body" not in css
    assert ".event-body::-webkit-scrollbar" not in css
    assert "tkinter" not in (ROOT / "frontend" / "local_server.py").read_text(encoding="utf-8")


def main() -> int:
    outputs: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            test_frontend_image_data_url_save(Path(tmp))
        outputs.append("image data-url save: ok")
        test_frontend_rejects_non_image_data_url()
        outputs.append("non-image rejection: ok")
        with tempfile.TemporaryDirectory() as tmp:
            test_frontend_workspace_must_exist(Path(tmp))
        outputs.append("missing workspace rejection: ok")
        with tempfile.TemporaryDirectory() as tmp:
            test_frontend_workspace_picker_supports_unicode_paths(Path(tmp))
        outputs.append("unicode workspace picker: ok")
        with tempfile.TemporaryDirectory() as tmp:
            test_frontend_configures_trace_dir(Path(tmp))
        outputs.append("frontend trace-dir config: ok")
        test_frontend_static_interaction_contract()
        outputs.append("static interaction contract: ok")
        result = FrontendCheckResult(
            status="PASS",
            detail="Local frontend helpers are working.",
            output_preview="\n".join(outputs),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        result = FrontendCheckResult(
            status="FAIL",
            detail=str(exc),
            output_preview="\n".join(outputs),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
