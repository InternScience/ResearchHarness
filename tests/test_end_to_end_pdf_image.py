#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import unicodedata
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


QUESTION_FILE = ROOT / "tests" / "cases" / "end_to_end_pdf_image.txt"
RUN_DIR = TEST_RUNS_DIR / "end_to_end_pdf_image"
RUN_WORKSPACE_ROOT = RUN_DIR / "workspace"
TRACE_DIR = RUN_DIR / "traces"
RUN_TIMEOUT_SECONDS = 420
REQUIRED_TOOL_NAMES = ["Bash", "ReadPDF", "ReadImage"]
REQUIRED_OUTPUT_KEYS = [
    "pdf_url",
    "local_pdf_path",
    "first_image_path",
    "figure_text",
]
EXPECTED_PDF_URL = (
    "https://proceedings.neurips.cc/paper_files/paper/2024/file/"
    "298c3e32d7d402189444be2ff5d19979-Paper-Conference.pdf"
)
EXPECTED_FIGURE_TEXT_TOKENS = [
    "physics",
    "ai",
    "learnable router weight",
    "forecast lead time",
    "pde error accumulation",
]


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


@dataclass
class AgentRunResult:
    status: str
    detail: str
    tool_calls_seen: int
    distinct_tools_seen: list[str]
    output_preview: str


def collect_trace_issues(rows: list[dict]) -> list[str]:
    return collect_trace_errors(rows)


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
    env["MAX_LLM_CALL_PER_RUN"] = "10"
    env["MAX_AGENT_RUNTIME_SECONDS"] = "360"
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
    tool_order_ok = False
    if required_tools_ok:
        tool_order_ok = (
            tool_names_seen.index("Bash")
            < tool_names_seen.index("ReadPDF")
            < tool_names_seen.index("ReadImage")
        )

    required_keys_ok = isinstance(result_json, dict) and all(key in result_json for key in REQUIRED_OUTPUT_KEYS)
    pdf_url_ok = isinstance(result_json, dict) and result_json.get("pdf_url") == EXPECTED_PDF_URL
    local_pdf_path_ok = False
    first_image_path_ok = False
    figure_text_ok = False
    if isinstance(result_json, dict):
        local_pdf_path_value = result_json.get("local_pdf_path")
        first_image_path_value = result_json.get("first_image_path")
        figure_text_value = result_json.get("figure_text")
        if isinstance(local_pdf_path_value, str) and local_pdf_path_value.strip():
            raw_local_pdf_path = Path(local_pdf_path_value)
            local_pdf_path = (
                raw_local_pdf_path
                if raw_local_pdf_path.is_absolute()
                else (RUN_WORKSPACE_ROOT / raw_local_pdf_path).resolve()
            )
            local_pdf_path_ok = (
                local_pdf_path.exists()
                and local_pdf_path.is_file()
                and local_pdf_path.suffix.lower() == ".pdf"
                and RUN_WORKSPACE_ROOT in local_pdf_path.parents
            )
        if isinstance(first_image_path_value, str) and first_image_path_value.strip():
            raw_first_image_path = Path(first_image_path_value)
            first_image_path = (
                raw_first_image_path
                if raw_first_image_path.is_absolute()
                else (RUN_WORKSPACE_ROOT / raw_first_image_path).resolve()
            )
            first_image_path_ok = (
                first_image_path.exists()
                and first_image_path.is_file()
                and RUN_WORKSPACE_ROOT in first_image_path.parents
            )
        if isinstance(figure_text_value, str):
            normalized_figure_text = normalize_text(figure_text_value)
            figure_text_ok = (
                sum(token in normalized_figure_text for token in EXPECTED_FIGURE_TEXT_TOKENS) >= 3
            )

    ok = (
        proc.returncode == 0
        and bool(trace_rows)
        and required_tools_ok
        and tool_order_ok
        and required_keys_ok
        and pdf_url_ok
        and local_pdf_path_ok
        and first_image_path_ok
        and figure_text_ok
        and training_trace_valid
        and not trace_issues
    )

    if ok:
        result = AgentRunResult(
            status="PASS",
            detail="Agent downloaded the online PDF, used ReadPDF to locate the first extracted image, and used ReadImage to read visible figure text with no assistant protocol errors.",
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
    if not tool_order_ok:
        detail_parts.append(f"tool order mismatch: got {tool_names_seen}")
    if not required_keys_ok:
        detail_parts.append(f"final result missing required keys: expected {REQUIRED_OUTPUT_KEYS}, got {result_json!r}")
    if not pdf_url_ok:
        detail_parts.append(f"unexpected pdf_url: {None if not isinstance(result_json, dict) else result_json.get('pdf_url')!r}")
    if not local_pdf_path_ok:
        detail_parts.append(f"invalid local_pdf_path: {None if not isinstance(result_json, dict) else result_json.get('local_pdf_path')!r}")
    if not first_image_path_ok:
        detail_parts.append(f"invalid first_image_path: {None if not isinstance(result_json, dict) else result_json.get('first_image_path')!r}")
    if not figure_text_ok:
        detail_parts.append(f"figure_text missing expected tokens: {None if not isinstance(result_json, dict) else result_json.get('figure_text')!r}")
    if not training_trace_valid:
        detail_parts.append("trace rows are not uniform flat training events")
    if trace_issues:
        detail_parts.append(f"trace contains invalid turn markers: {sorted(set(trace_issues))}")
    if result_json_error:
        detail_parts.append(f"final result is not valid JSON ({result_json_error})")

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
