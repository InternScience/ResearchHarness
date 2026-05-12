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


QUESTION_FILE = ROOT / "tests" / "cases" / "end_to_end_write_edit.txt"
RUN_DIR = TEST_RUNS_DIR / "end_to_end_write_edit"
RUN_WORKSPACE_ROOT = RUN_DIR / "workspace"
TRACE_DIR = RUN_DIR / "traces"
RUN_TIMEOUT_SECONDS = 300
REQUIRED_TOOL_NAMES = ["Write", "Edit", "Read", "Bash"]
FORBIDDEN_TOOL_NAMES = [
    "Glob",
    "Grep",
    "ReadPDF",
    "ReadImage",
    "WebSearch",
    "ScholarSearch",
    "WebFetch",
    "TerminalStart",
    "TerminalWrite",
    "TerminalRead",
    "TerminalInterrupt",
    "TerminalKill",
]
EXPECTED_FILE_NAME = "write_edit_demo.txt"
EXPECTED_SHA256_PREFIX = "a8bc8eab76a2"
EXPECTED_LINE_COUNT = 4
EXPECTED_STATUS = "final"
EXPECTED_GAMMA = 3


@dataclass
class AgentRunResult:
    status: str
    detail: str
    tool_calls_seen: int
    distinct_tools_seen: list[str]
    output_preview: str


def main() -> int:
    load_dotenv(ROOT / ".env")
    require_test_env()
    prompt = QUESTION_FILE.read_text(encoding="utf-8").strip()
    RUN_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
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
    env["WORKSPACE_ROOT"] = str(RUN_WORKSPACE_ROOT)

    try:
        proc = subprocess.run(
            subprocess_python()
            + [
                "-m",
                "agent_base.react_agent",
                prompt,
                "--workspace-root",
                str(RUN_WORKSPACE_ROOT),
                "--trace-dir",
                str(TRACE_DIR),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        combined_output = (exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else "")
        trace_path = single_trace_path(TRACE_DIR)
        trace_rows = load_trace_records(trace_path) if trace_path else []
        tool_names_seen = collect_tool_names(trace_rows)
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
    trace_path = single_trace_path(TRACE_DIR)
    trace_rows = load_trace_records(trace_path) if trace_path else []
    tool_names_seen = collect_tool_names(trace_rows)
    distinct_tools_seen = sorted(set(tool_names_seen))
    tool_calls_seen = len(tool_names_seen)
    trace_issues = collect_trace_errors(trace_rows)
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
    tool_order_ok = False
    if required_tools_ok:
        tool_order_ok = (
            tool_names_seen.index("Write")
            < tool_names_seen.index("Edit")
            < tool_names_seen.index("Read")
            < tool_names_seen.index("Bash")
        )

    output_ok = False
    if isinstance(result_json, dict):
        file_path_value = result_json.get("file_path")
        status_value = result_json.get("status")
        gamma_value = result_json.get("gamma")
        line_count_value = result_json.get("line_count")
        sha256_prefix_value = result_json.get("sha256_prefix")
        if isinstance(file_path_value, str) and file_path_value.strip():
            raw_file_path = Path(file_path_value)
            file_path = raw_file_path if raw_file_path.is_absolute() else (RUN_WORKSPACE_ROOT / raw_file_path).resolve()
            file_exists_ok = (
                file_path.exists()
                and file_path.is_file()
                and file_path.name == EXPECTED_FILE_NAME
                and RUN_WORKSPACE_ROOT in file_path.parents
            )
            file_content_ok = file_path.read_text(encoding="utf-8") == "alpha=1\nbeta=2\ngamma=3\nstatus=final\n"
        else:
            file_exists_ok = False
            file_content_ok = False

        try:
            gamma_ok = int(gamma_value) == EXPECTED_GAMMA
        except (TypeError, ValueError):
            gamma_ok = False
        try:
            line_count_ok = int(line_count_value) == EXPECTED_LINE_COUNT
        except (TypeError, ValueError):
            line_count_ok = False

        output_ok = (
            file_exists_ok
            and file_content_ok
            and status_value == EXPECTED_STATUS
            and gamma_ok
            and line_count_ok
            and sha256_prefix_value == EXPECTED_SHA256_PREFIX
        )

    ok = (
        proc.returncode == 0
        and bool(trace_rows)
        and required_tools_ok
        and not forbidden_tools_seen
        and tool_order_ok
        and output_ok
        and training_trace_valid
        and not trace_issues
    )

    if ok:
        result = AgentRunResult(
            status="PASS",
            detail="Agent completed a real write-edit-read-bash run with no assistant protocol errors.",
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
    if required_tools_ok and not tool_order_ok:
        detail_parts.append(f"tool order mismatch: observed {tool_names_seen}")
    if not output_ok:
        if result_json_error:
            detail_parts.append(f"final result_text is not valid required JSON ({result_json_error}): {result_text!r}")
        else:
            detail_parts.append(f"final result JSON mismatch: {result_json!r}")
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
