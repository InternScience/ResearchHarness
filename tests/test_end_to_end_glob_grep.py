#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_base.utils import load_dotenv
from test_support import (
    TEST_RUNS_DIR,
    collect_tool_names,
    collect_trace_errors,
    final_result_text,
    load_trace_records,
    preview,
    require_test_env,
    single_trace_path,
    subprocess_python,
    training_trace_ok,
)


QUESTION_FILE = ROOT / "tests" / "cases" / "end_to_end_glob_grep.txt"
TRACE_DIR = TEST_RUNS_DIR / "end_to_end_glob_grep" / "traces"
RUN_TIMEOUT_SECONDS = 240
REQUIRED_TOOL_NAMES = ["Glob", "Grep", "Read", "Bash"]
FORBIDDEN_TOOL_NAMES = [
    "WebSearch",
    "ScholarSearch",
    "WebFetch",
    "TerminalStart",
    "TerminalWrite",
    "TerminalRead",
    "TerminalInterrupt",
    "TerminalKill",
]
EXPECTED_OUTPUT = {
    "txt_file_count": 1,
    "matched_file_name": "hello.txt",
    "matched_line_text": "Hello.",
    "uppercase_text": "HELLO.",
}

@dataclass
class AgentRunResult:
    status: str
    detail: str
    tool_calls_seen: int
    distinct_tools_seen: list[str]
    output_preview: str


def load_trace(trace_dir: Path) -> tuple[list[dict], list[str]]:
    trace_path = single_trace_path(trace_dir)
    records = load_trace_records(trace_path) if trace_path else []
    return records, collect_tool_names(records)


def collect_trace_issues(rows: list[dict]) -> list[str]:
    return collect_trace_errors(rows)


def main() -> int:
    load_dotenv(ROOT / ".env")
    require_test_env()
    prompt = QUESTION_FILE.read_text(encoding="utf-8").strip()
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    for existing_trace in TRACE_DIR.glob("*.jsonl"):
        existing_trace.unlink()

    env = os.environ.copy()
    env["DEBUG_AGENT"] = "1"
    env["TEMPERATURE"] = "0"
    env["TOP_P"] = "1.0"
    env["PRESENCE_PENALTY"] = "0.0"
    env["MAX_LLM_CALL_PER_RUN"] = "8"
    env["MAX_AGENT_RUNTIME_SECONDS"] = "180"
    env["LLM_MAX_RETRIES"] = "2"
    env["LLM_TIMEOUT_SECONDS"] = "120"
    env["WORKSPACE_ROOT"] = str(ROOT)

    try:
        proc = subprocess.run(
            subprocess_python() + ["-m", "agent_base.react_agent", prompt, "--trace-dir", str(TRACE_DIR)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        combined_output = (exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else "")
        trace_rows, tool_names_seen = load_trace(TRACE_DIR)
        result = AgentRunResult(
            status="FAIL",
            detail=f"agent_base.react_agent timed out after {RUN_TIMEOUT_SECONDS} seconds",
            tool_calls_seen=len(tool_names_seen),
            distinct_tools_seen=sorted(set(tool_names_seen)),
            output_preview=preview(combined_output),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 1

    combined_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    trace_rows, tool_names_seen = load_trace(TRACE_DIR)
    distinct_tools_seen = sorted(set(tool_names_seen))
    tool_calls_seen = len(tool_names_seen)
    trace_issues = collect_trace_issues(trace_rows)
    training_trace_valid = training_trace_ok(trace_rows)
    result_text = final_result_text(trace_rows)

    result_json = None
    result_json_error = None
    try:
        if isinstance(result_text, str):
            result_json = json.loads(result_text)
    except json.JSONDecodeError as exc:
        result_json_error = str(exc)

    required_tools_ok = all(tool in distinct_tools_seen for tool in REQUIRED_TOOL_NAMES)
    forbidden_tools_seen = sorted(tool for tool in distinct_tools_seen if tool in FORBIDDEN_TOOL_NAMES)
    output_ok = isinstance(result_json, dict) and all(result_json.get(key) == value for key, value in EXPECTED_OUTPUT.items())

    ok = (
        proc.returncode == 0
        and bool(trace_rows)
        and required_tools_ok
        and not forbidden_tools_seen
        and output_ok
        and training_trace_valid
        and not trace_issues
    )

    if ok:
        result = AgentRunResult(
            status="PASS",
            detail="Agent completed a real local run using Glob, Grep, Read, and Bash with no assistant protocol errors.",
            tool_calls_seen=tool_calls_seen,
            distinct_tools_seen=distinct_tools_seen,
            output_preview=preview(combined_output),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    detail_parts: list[str] = []
    if proc.returncode != 0:
        detail_parts.append(f"agent_base.react_agent exited with code {proc.returncode}")
    if not trace_rows:
        detail_parts.append(f"trace not found in directory: {TRACE_DIR}")
    if not required_tools_ok:
        detail_parts.append(f"required tools missing: expected {REQUIRED_TOOL_NAMES}, got {distinct_tools_seen}")
    if forbidden_tools_seen:
        detail_parts.append(f"forbidden tools used: {forbidden_tools_seen}")
    if not output_ok:
        if result_json_error:
            detail_parts.append(f"final result_text is not valid required JSON ({result_json_error}): {result_text!r}")
        else:
            detail_parts.append(f"final result JSON mismatch: expected {EXPECTED_OUTPUT}, got {result_json!r}")
    if not training_trace_valid:
        detail_parts.append("trace rows are not uniform flat training events")
    if trace_issues:
        detail_parts.append(f"trace contains invalid turn markers: {sorted(set(trace_issues))}")

    result = AgentRunResult(
        status="FAIL",
        detail="; ".join(detail_parts),
        tool_calls_seen=tool_calls_seen,
        distinct_tools_seen=distinct_tools_seen,
        output_preview=preview(combined_output),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
