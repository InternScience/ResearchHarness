import base64
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend.local_server import (
    app,
    configure_frontend,
    decode_image_data_url,
    save_uploaded_images,
    _register_file_workspace,
    _resolve_existing_workspace,
    _resolve_workspace_file_path,
    _unregister_file_workspace,
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


def test_frontend_workspace_file_paths_are_scoped_to_workspace(tmp_path: Path) -> None:
    image_path = tmp_path / "outputs" / "plot.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"png")
    spaced_path = tmp_path / "outputs" / "demo image.png"
    spaced_path.write_bytes(b"png")

    assert _resolve_workspace_file_path(tmp_path, "outputs/plot.png") == image_path.resolve()
    assert _resolve_workspace_file_path(tmp_path, str(image_path)) == image_path.resolve()
    assert _resolve_workspace_file_path(tmp_path, "outputs/demo%20image.png") == spaced_path.resolve()

    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"png")
    try:
        _resolve_workspace_file_path(tmp_path, str(outside))
    except Exception as exc:
        assert "outside the workspace" in str(exc)
    else:
        raise AssertionError("expected outside workspace file to be rejected")

    text_file = tmp_path / "outputs" / "note.txt"
    text_file.write_text("not an image", encoding="utf-8")
    try:
        _resolve_workspace_file_path(tmp_path, "outputs/note.txt")
    except Exception as exc:
        assert "image files" in str(exc)
    else:
        raise AssertionError("expected non-image file to be rejected")


def test_frontend_workspace_file_endpoint_serves_only_scoped_images(tmp_path: Path) -> None:
    image_path = tmp_path / "outputs" / "demo image.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"png-bytes")
    svg_path = tmp_path / "assets" / "workflow.svg"
    svg_path.parent.mkdir()
    svg_path.write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>", encoding="utf-8")
    outside = tmp_path.parent / f"outside-{tmp_path.name}.png"
    outside.write_bytes(b"outside")
    token = _register_file_workspace(tmp_path)
    client = TestClient(app)
    try:
        for path in ("outputs/demo image.png", "outputs/demo%20image.png", str(image_path)):
            response = client.get("/api/workspace-file", params={"token": token, "path": path})
            assert response.status_code == 200
            assert response.content == b"png-bytes"
        svg_response = client.get("/api/workspace-file", params={"token": token, "path": "assets/workflow.svg"})
        assert svg_response.status_code == 200
        assert b"<svg" in svg_response.content
        outside_response = client.get("/api/workspace-file", params={"token": token, "path": str(outside)})
        assert outside_response.status_code == 403
        missing_token_response = client.get("/api/workspace-file", params={"token": "missing", "path": "outputs/demo image.png"})
        assert missing_token_response.status_code == 404
    finally:
        _unregister_file_workspace(token)
        outside.unlink(missing_ok=True)


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
    assert "Ctrl+Enter or Shift+Enter inserts a newline" in html
    assert "Click + to add one or more images" in html
    assert '/static/favicon.svg?v=rocket-1' in html
    assert 'placeholder="Message ResearchHarness"' in html
    assert 'id="modelSelect"' in html
    assert 'value="gpt-5.5"' in html
    assert 'id="modelOptions"' in html
    assert 'data-model-value="claude-opus-4-7"' in html
    assert "Message ResearchHarness... Enter sends" not in html
    assert 'id="workspaceStrip"' in html
    assert 'id="workspaceInput" type="hidden"' in html
    assert "Workspace not selected." in html
    assert "Workspace folder path" not in html
    assert "--role-prompt-file" in launcher
    assert "--trace-dir" in launcher
    assert "configure_frontend" in launcher
    assert "role_prompt=FRONTEND_ROLE_PROMPT or None" in server
    assert "trace_dir=FRONTEND_TRACE_DIR" in server
    assert "default_llm_config(model_name=model_name or None)" in server
    assert "prior_messages=prior_messages" in server
    assert "conversation_messages" in server
    assert "No active conversation is available on the server" in server
    assert 'message_type == "interrupt"' in server
    assert "interrupt_requested" in server
    assert "interrupt_event=bridge.cancelled" in server
    assert "/api/workspace-directories" in js
    assert "autoFollowTimeline" in js
    assert "syncTimelineFollowMode" in js
    assert "setWorkspaceSelected" in js
    assert "Workspace selected: " in js
    assert "Connection closed. Refresh to reconnect." not in js
    assert "Starting agent run" not in js
    assert "Finished: " not in js
    assert "conversationStarted" in js
    assert "continue_conversation" in js
    assert "modelSelect" in js
    assert "setupModelDropdown" in js
    assert "positionModelOptions" in js
    assert "document.body.appendChild(modelOptions)" in js
    assert "model_name: modelSelect ? modelSelect.value : \"\"" in js
    assert "setEventExpanded" in js
    assert "refreshEventCollapseCapability" in js
    assert "can-collapse" in js
    assert "COLLAPSED_STEP_HEIGHT + 8" in js
    assert "body.scrollHeight" in js
    assert "keepSubmittedMessageOnReset" in js
    assert "setEventExpanded(eventNode, false, true)" in js
    assert "sendAskUserAnswer" in js
    assert "Agent question" in js
    assert 'runBtn.textContent = "Reply"' in js
    assert '"Stop"' in js
    assert "sendInterrupt" in js
    assert "Interrupting" in js
    assert 'addMessage("user", answer, [])' in js
    assert "function renderMarkdown(text)" in js
    assert "rewriteWorkspaceImageSources" in js
    assert "normalizeMarkdownImageDestinations" in js
    assert "unwrapFullMarkdownFence" in js
    assert "markdown|md|gfm" in js
    assert "renderMermaidInMarkdown" in js
    assert "language-mermaid" in js
    assert "language-mmd" in js
    assert "lang-mmd" in js
    assert "securityLevel: \"strict\"" in js
    assert "renderMermaidInMarkdown(node)" in js
    assert "/api/workspace-file" in js
    assert '"file_token"' in server
    assert "window.marked.parse" in js
    assert "window.DOMPurify.sanitize" in js
    assert '!tools.length && row.termination === "result"' in js
    assert '"Message ResearchHarness"' in js
    assert "Answer the agent question here" not in js
    assert "askForm" not in js
    assert "askAnswer" not in js
    assert "askPanel" not in html
    assert "https://github.com/InternScience/ResearchHarness" in html
    assert "https://black-yt.github.io/" in html
    assert 'class="space-links"' in html
    assert "marked@15.0.12/marked.min.js" in html
    assert "dompurify@3.2.6/dist/purify.min.js" in html
    assert "mermaid@11.12.0/dist/mermaid.min.js" in html
    assert 'eventNode.classList.add("collapsed")' not in js
    assert 'node.classList.contains("latest")' in js
    assert "event.isComposing" in js
    assert ".rh_frontend_inputs" not in server
    assert "send-button.is-running" in css
    assert "0 14px 38px rgba(var(--glow-rgb), 0.15)" in css
    assert "position: sticky" in css
    assert "top: 66px" in css
    assert "z-index: 4" in css
    assert "z-index: 80" in css
    assert ".model-options.open" in css
    assert ".topbar:has(.model-dropdown.open)" not in css
    assert "height: 100dvh" in css
    assert ".chat-shell > *" in css
    assert "overflow-wrap: anywhere" in css
    assert "word-break: break-word" in css
    assert "flex: 0 0 auto" in css
    assert "overflow-y: scroll" in css
    assert "max-height: 100%" in css
    assert "transition: max-height" in css
    assert "event-body-inner" in css
    assert ".event.can-collapse.collapsed .event-body-inner" in css
    assert ".event.can-collapse.collapsed .event-body-inner::after" in css
    assert ".event.collapsed .event-body-inner::after" not in css
    assert ".markdown-body" in css
    assert "object-fit: contain" in css
    assert ".markdown-body table" in css
    assert ".mermaid-chart" in css
    assert ".event.can-collapse" in css
    assert ".event:not(.can-collapse) .event-toggle" in css
    assert ".space-links" in css
    assert ".sr-only" in css
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
            test_frontend_workspace_file_paths_are_scoped_to_workspace(Path(tmp))
        outputs.append("workspace image path scoping: ok")
        with tempfile.TemporaryDirectory() as tmp:
            test_frontend_workspace_file_endpoint_serves_only_scoped_images(Path(tmp))
        outputs.append("workspace image endpoint: ok")
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
