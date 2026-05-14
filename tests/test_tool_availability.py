import argparse
import io
import json
import re
import shutil
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_support import (
    EXAMPLE_TEXT_FILES_DIR,
    TEST_RUNS_DIR,
    bootstrap,
    clear_pdf_parse_cache,
    has_structai,
    required_test_image,
    required_test_pdf,
)


DEFAULT_VISIT_URL = "https://en.wikipedia.org/wiki/Attention_Is_All_You_Need"
NETWORK_TOOL_NAMES = {"WebSearch", "ScholarSearch", "WebFetch", "ReadPDF"}


def preview(value: Any, limit: int = 500) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def call_with_capture(func: Callable[..., Any], *args, **kwargs) -> tuple[Any, str, str]:
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        result = func(*args, **kwargs)
    return result, stdout_buffer.getvalue(), stderr_buffer.getvalue()


@dataclass
class ToolTestResult:
    name: str
    status: str
    duration_seconds: float
    detail: str
    output_preview: str = ""
    stdout_preview: str = ""
    stderr_preview: str = ""


def make_result(
    name: str,
    status: str,
    started_at: float,
    detail: str,
    output: Any = "",
    stdout: str = "",
    stderr: str = "",
) -> ToolTestResult:
    return ToolTestResult(
        name=name,
        status=status,
        duration_seconds=round(time.time() - started_at, 3),
        detail=detail,
        output_preview=preview(output),
        stdout_preview=preview(stdout),
        stderr_preview=preview(stderr),
    )


def test_search() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_web import WebSearch

    tool = WebSearch()
    result, stdout, stderr = call_with_capture(tool.call, {"query": ["OpenAI"]})
    text = str(result)
    if "## Web Results" not in text or "Timeout" in text or "Invalid request format" in text:
        return make_result("WebSearch", "FAIL", started_at, "Unexpected search response.", text, stdout, stderr)
    return make_result("WebSearch", "PASS", started_at, "WebSearch returned web results.", text, stdout, stderr)


def test_google_scholar() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_web import ScholarSearch

    tool = ScholarSearch()
    result, stdout, stderr = call_with_capture(tool.call, {"query": ["Attention Is All You Need"]})
    text = str(result)
    if "## Scholar Results" not in text or "Timeout" in text or "Invalid request format" in text:
        return make_result("ScholarSearch", "FAIL", started_at, "Unexpected ScholarSearch response.", text, stdout, stderr)
    return make_result("ScholarSearch", "PASS", started_at, "ScholarSearch returned academic results.", text, stdout, stderr)


def test_visit() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_web import WebFetch

    tool = WebFetch()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {
            "url": [DEFAULT_VISIT_URL],
        },
    )
    text = str(result)
    bad_markers = [
        "[WebFetch] Failed to read page.",
        "The provided webpage content could not be accessed",
    ]
    if "source_type: web" not in text or "content:" not in text or any(marker in text for marker in bad_markers):
        return make_result("WebFetch", "FAIL", started_at, "WebFetch did not return usable webpage content.", text, stdout, stderr)
    return make_result("WebFetch", "PASS", started_at, "WebFetch returned webpage content.", text, stdout, stderr)


def test_read() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import Read

    target_file = EXAMPLE_TEXT_FILES_DIR / "hello.txt"
    if not target_file.exists():
        return make_result("Read", "FAIL", started_at, f"Test file does not exist: {target_file}")

    tool = Read()
    result, stdout, stderr = call_with_capture(tool.call, {"path": str(target_file)})
    text = str(result)
    if "source_type: text" not in text or "Hello." not in text:
        return make_result("Read", "FAIL", started_at, "Read did not return expected local text content.", text, stdout, stderr)
    return make_result("Read", "PASS", started_at, "Read returned expected local text content.", text, stdout, stderr)


def test_glob() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import Glob

    tool = Glob()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"pattern": "**/*.txt", "path": str(EXAMPLE_TEXT_FILES_DIR), "max_results": 20},
    )
    text = str(result)
    if "match_count:" not in text or "hello.txt" not in text:
        return make_result("Glob", "FAIL", started_at, "Glob did not return expected file matches.", text, stdout, stderr)
    return make_result("Glob", "PASS", started_at, "Glob returned expected file matches.", text, stdout, stderr)


def test_grep() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import Grep

    tool = Grep()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"pattern": "Hello\\.", "path": str(EXAMPLE_TEXT_FILES_DIR), "glob": "**/*.txt", "max_results": 20},
    )
    text = str(result)
    if "match_count:" not in text or "hello.txt:1: Hello." not in text:
        return make_result("Grep", "FAIL", started_at, "Grep did not return expected text matches.", text, stdout, stderr)
    return make_result("Grep", "PASS", started_at, "Grep returned expected text matches.", text, stdout, stderr)


def test_read_pdf() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import ReadPDF

    pdf_path = required_test_pdf()
    cache_dir = clear_pdf_parse_cache(pdf_path)
    if not has_structai():
        return make_result("ReadPDF", "FAIL", started_at, "Missing required dependency: structai")

    tool = ReadPDF()
    result, stdout, stderr = call_with_capture(tool.call, {"path": str(pdf_path), "max_chars": 2000})
    text = str(result)
    if "source_type: pdf" not in text or "Dummy PDF file" not in text:
        return make_result("ReadPDF", "FAIL", started_at, "ReadPDF did not return expected PDF content.", text, stdout, stderr)
    return make_result(
        "ReadPDF",
        "PASS",
        started_at,
        f"ReadPDF returned expected PDF content after clearing cache: {cache_dir}",
        text,
        stdout,
        stderr,
    )


def test_read_image() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import ReadImage

    image_path = required_test_image()

    tool = ReadImage()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"path": str(image_path)},
    )
    text = str(result)
    if (
        "source_type: image" not in text
        or "format: JPEG" not in text
        or "mime_type: image/jpeg" not in text
        or "width: 1280" not in text
        or "height: 720" not in text
        or "llm_image_attached: true" not in text
    ):
        return make_result("ReadImage", "FAIL", started_at, "ReadImage did not return expected image metadata.", text, stdout, stderr)
    return make_result("ReadImage", "PASS", started_at, "ReadImage returned expected image metadata.", text, stdout, stderr)


def test_write() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import Write

    target = TEST_RUNS_DIR / "tool_availability_check" / "write_demo.txt"
    tool = Write()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"path": str(target), "content": "alpha\nbeta\n", "overwrite": True},
    )
    text = str(result)
    if "Wrote file" not in text or not target.exists():
        return make_result("Write", "FAIL", started_at, "Write did not create the expected file.", text, stdout, stderr)
    return make_result("Write", "PASS", started_at, "Write created the expected file.", text, stdout, stderr)


def test_edit() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_file import Edit

    target = TEST_RUNS_DIR / "tool_availability_check" / "edit_demo.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("line one\nline two\n", encoding="utf-8")
    tool = Edit()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"path": str(target), "patch": "@@ -1,2 +1,2 @@\n-line one\n+line alpha\n line two"},
    )
    text = str(result)
    file_text = target.read_text(encoding="utf-8")
    if "Updated file" not in text or "line alpha" not in file_text:
        return make_result("Edit", "FAIL", started_at, "Edit did not update the file as expected.", text, stdout, stderr)
    return make_result("Edit", "PASS", started_at, "Edit updated the file as expected.", text, stdout, stderr)


def test_bash() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_runtime import Bash

    workdir = EXAMPLE_TEXT_FILES_DIR
    tool = Bash()
    result, stdout, stderr = call_with_capture(
        tool.call,
        {"command": "pwd && sed -n '1,2p' hello.txt", "timeout": 20, "workdir": str(workdir)},
    )
    text = str(result)
    if "exit_code: 0" not in text or "Hello." not in text:
        return make_result("Bash", "FAIL", started_at, "Bash did not execute the expected command.", text, stdout, stderr)
    return make_result("Bash", "PASS", started_at, "Bash executed the expected command.", text, stdout, stderr)


def test_ask_user() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_user import AskUser

    prompt_output = io.StringIO()
    result, stdout, stderr = call_with_capture(
        AskUser().call,
        {
            "question": "Which deterministic answer should be used?",
            "context": "Testing AskUser availability.",
        },
        input_stream=io.StringIO("availability-ok\n"),
        output_stream=prompt_output,
    )
    text = str(result)
    combined_stdout = stdout + prompt_output.getvalue()
    if "User answer" not in text or "availability-ok" not in text or "Which deterministic answer" not in combined_stdout:
        return make_result("AskUser", "FAIL", started_at, "AskUser did not return the expected deterministic answer.", text, combined_stdout, stderr)
    return make_result("AskUser", "PASS", started_at, "AskUser returned the expected deterministic answer.", text, combined_stdout, stderr)


def test_terminal_toolchain() -> ToolTestResult:
    started_at = time.time()
    from agent_base.tools.tool_runtime import TerminalInterrupt, TerminalKill, TerminalRead, TerminalStart, TerminalWrite

    workdir = TEST_RUNS_DIR / "tool_availability_check" / "terminal"
    workdir.mkdir(parents=True, exist_ok=True)
    start_tool = TerminalStart()
    start_result, stdout_start, stderr_start = call_with_capture(start_tool.call, {"cwd": str(workdir)})
    start_text = str(start_result)
    session_match = re.search(r"session_id: (term_\d+)", start_text)
    if not session_match:
        return make_result("TerminalToolchain", "FAIL", started_at, "TerminalStart did not return a valid session id.", start_text, stdout_start, stderr_start)
    session_id = session_match.group(1)

    write_tool = TerminalWrite()
    write_result, stdout_write, stderr_write = call_with_capture(
        write_tool.call,
        {"session_id": session_id, "input": "printf 'alpha\\n'; pwd", "yield_time_ms": 300},
    )
    read_tool = TerminalRead()
    read_result, stdout_read, stderr_read = call_with_capture(
        read_tool.call,
        {"session_id": session_id, "yield_time_ms": 200, "max_output_chars": 4000},
    )
    interrupt_prep_result, stdout_interrupt_prep, stderr_interrupt_prep = call_with_capture(
        write_tool.call,
        {"session_id": session_id, "input": "sleep 10", "yield_time_ms": 150},
    )
    interrupt_tool = TerminalInterrupt()
    interrupt_result, stdout_interrupt, stderr_interrupt = call_with_capture(
        interrupt_tool.call,
        {"session_id": session_id, "max_output_chars": 4000},
    )
    kill_tool = TerminalKill()
    kill_result, stdout_kill, stderr_kill = call_with_capture(kill_tool.call, {"session_id": session_id})

    combined = "\n\n".join(
        [
            str(start_result),
            str(write_result),
            str(read_result),
            str(interrupt_prep_result),
            str(interrupt_result),
            str(kill_result),
        ]
    )
    stdout = "\n".join(
        filter(
            None,
            [
                stdout_start,
                stdout_write,
                stdout_read,
                stdout_interrupt_prep,
                stdout_interrupt,
                stdout_kill,
            ],
        )
    )
    stderr = "\n".join(
        filter(
            None,
            [
                stderr_start,
                stderr_write,
                stderr_read,
                stderr_interrupt_prep,
                stderr_interrupt,
                stderr_kill,
            ],
        )
    )

    ok = (
        "Started terminal session" in str(start_result)
        and "Session updated" in str(write_result)
        and "alpha" in (str(write_result) + str(read_result))
        and "Sent Ctrl-C" in str(interrupt_result)
        and "alive: true" in str(interrupt_result)
        and "Terminal session terminated" in str(kill_result)
    )
    if not ok:
        return make_result("TerminalToolchain", "FAIL", started_at, "Terminal toolchain returned unexpected output.", combined, stdout, stderr)
    return make_result("TerminalToolchain", "PASS", started_at, "Terminal toolchain executed successfully.", combined, stdout, stderr)


def test_main_agent_api() -> ToolTestResult:
    started_at = time.time()
    from agent_base.react_agent import MultiTurnReactAgent, default_llm_config

    workdir = TEST_RUNS_DIR / "tool_availability_check" / "main_agent_api"
    trace_dir = workdir / "trace"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    llm_config = default_llm_config()
    generate_cfg = dict(llm_config.get("generate_cfg") or {})
    generate_cfg["max_output_tokens"] = 32
    generate_cfg["max_retries"] = 2
    llm_config["generate_cfg"] = generate_cfg

    agent = MultiTurnReactAgent(
        function_list=[],
        llm=llm_config,
        trace_dir=str(trace_dir),
        max_rounds=2,
        max_runtime_seconds=180,
    )
    result, stdout, stderr = call_with_capture(
        agent.run,
        "Reply with exactly the lowercase token main-agent-ok and no other text.",
        workspace_root=str(workdir),
    )
    text = str(result).strip()
    if "main-agent-ok" not in text.lower():
        return make_result(
            "MainAgentAPI",
            "FAIL",
            started_at,
            "Main agent real API call did not return the expected marker.",
            text,
            stdout,
            stderr,
        )
    return make_result("MainAgentAPI", "PASS", started_at, "Main agent real API call completed successfully.", text, stdout, stderr)


TESTS: dict[str, Callable[[], ToolTestResult]] = {
    "MainAgentAPI": test_main_agent_api,
    "WebSearch": test_search,
    "ScholarSearch": test_google_scholar,
    "WebFetch": test_visit,
    "Glob": test_glob,
    "Grep": test_grep,
    "Read": test_read,
    "ReadPDF": test_read_pdf,
    "ReadImage": test_read_image,
    "Write": test_write,
    "Edit": test_edit,
    "Bash": test_bash,
    "AskUser": test_ask_user,
    "TerminalToolchain": test_terminal_toolchain,
}
TOOL_COVERAGE_NAMES = {
    name for name in TESTS if name != "TerminalToolchain"
} | {
    "TerminalStart",
    "TerminalWrite",
    "TerminalRead",
    "TerminalInterrupt",
    "TerminalKill",
}


def assert_all_runtime_tools_are_covered() -> None:
    from agent_base.react_agent import AVAILABLE_TOOL_MAP

    missing = sorted(set(AVAILABLE_TOOL_MAP) - TOOL_COVERAGE_NAMES)
    if missing:
        raise RuntimeError(f"Missing tool availability checks for runtime tools: {', '.join(missing)}")


def run_selected_tests(selected: list[str]) -> list[ToolTestResult]:
    results: list[ToolTestResult] = []
    for name in selected:
        started_at = time.time()
        try:
            results.append(TESTS[name]())
        except Exception as exc:
            results.append(
                make_result(
                    name,
                    "FAIL",
                    started_at,
                    f"Unhandled exception during test execution: {type(exc).__name__}: {exc}",
                    traceback.format_exc(),
                )
            )
    return results


def print_human_readable(results: list[ToolTestResult]) -> None:
    for result in results:
        print(f"[{result.status}] {result.name} ({result.duration_seconds:.3f}s) {result.detail}")
        if result.output_preview:
            print(f"  output: {result.output_preview}")
        if result.stdout_preview:
            print(f"  stdout: {result.stdout_preview}")
        if result.stderr_preview:
            print(f"  stderr: {result.stderr_preview}")

    total = len(results)
    passed = sum(result.status == "PASS" for result in results)
    failed = sum(result.status == "FAIL" for result in results)
    print(f"\nSummary: total={total}, passed={passed}, failed={failed}")
    if any(result.status != "PASS" and result.name in NETWORK_TOOL_NAMES for result in results):
        print(
            "\nHint: If WebSearch, ScholarSearch, WebFetch, or ReadPDF fails with network, TLS, "
            "upload, download, or parsing errors, retry with VPN/proxy disabled."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Availability check for the core local agent tools.")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=list(TESTS.keys()),
        default=list(TESTS.keys()),
        help="Run only the specified tests.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print results as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    bootstrap()
    assert_all_runtime_tools_are_covered()
    args = parse_args()
    results = run_selected_tests(args.only)

    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_human_readable(results)

    has_failures = any(result.status != "PASS" for result in results)
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
