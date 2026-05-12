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
    final_result_text,
    load_trace_records,
    preview,
    require_test_env,
    single_trace_path,
    subprocess_python,
    training_trace_ok,
)


QUESTION_FILE = ROOT / "tests" / "cases" / "end_to_end_multitool.txt"
TRACE_DIR = TEST_RUNS_DIR / "end_to_end_multitool" / "traces"
RUN_TIMEOUT_SECONDS = 420
REQUIRED_TOOL_NAMES = ["Read", "Bash", "ScholarSearch", "WebSearch", "WebFetch"]
REQUIRED_OUTPUT_KEYS = [
    "winning_model",
    "winning_score",
    "transformer_paper_year",
    "transformer_authors",
    "visited_url",
    "evidence_used",
]
EXPECTED_VISITED_URL = "https://arxiv.org/abs/1706.03762"
EXPECTED_MODEL = "HelixAttn"
EXPECTED_SCORE = 39.66064
EXPECTED_YEAR = 2017
EXPECTED_AUTHOR_TOKENS = [
    "vaswani",
    "shazeer",
    "parmar",
    "uszkoreit",
    "jones",
    "gomez",
    "kaiser",
    "polosukhin",
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
def load_trace(trace_dir: Path) -> tuple[list[dict], list[str]]:
    trace_path = single_trace_path(trace_dir)
    records = load_trace_records(trace_path) if trace_path else []
    return records, collect_tool_names(records)


def collect_trace_issues(rows: list[dict]) -> tuple[list[str], bool]:
    issues: list[str] = []
    other_protocol_error_seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        protocol_error = row.get("error")
        if isinstance(protocol_error, str) and protocol_error:
            other_protocol_error_seen = True
            issues.append("assistant_protocol_error")
    return issues, other_protocol_error_seen


def main() -> int:
    load_dotenv(ROOT / ".env")
    require_test_env()
    prompt = QUESTION_FILE.read_text(encoding="utf-8").strip()
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    for existing_trace in TRACE_DIR.glob("*.jsonl"):
        existing_trace.unlink()

    env = os.environ.copy()
    env["DEBUG_AGENT"] = "1"
    env["DEBUG_VISIT"] = "1"
    env["TEMPERATURE"] = "0"
    env["TOP_P"] = "1.0"
    env["PRESENCE_PENALTY"] = "0.0"
    env["MAX_LLM_CALL_PER_RUN"] = "12"
    env["MAX_AGENT_RUNTIME_SECONDS"] = "390"
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
    result_text = final_result_text(trace_rows)
    trace_issues, other_protocol_error_seen = collect_trace_issues(trace_rows)
    training_trace_valid = training_trace_ok(trace_rows)

    result_json = None
    result_json_error = None
    try:
        if isinstance(result_text, str):
            result_json = json.loads(result_text)
    except json.JSONDecodeError as exc:
        result_json_error = str(exc)
        result_json = None

    required_tools_ok = all(tool in distinct_tools_seen for tool in REQUIRED_TOOL_NAMES)
    required_keys_ok = isinstance(result_json, dict) and all(key in result_json for key in REQUIRED_OUTPUT_KEYS)
    visited_url_ok = isinstance(result_json, dict) and result_json.get("visited_url") == EXPECTED_VISITED_URL
    winning_model_ok = isinstance(result_json, dict) and result_json.get("winning_model") == EXPECTED_MODEL
    winning_score_ok = False
    transformer_year_ok = False
    transformer_authors_ok = False
    evidence_used_ok = False
    if isinstance(result_json, dict):
        try:
            winning_score_ok = abs(float(result_json.get("winning_score")) - EXPECTED_SCORE) < 1e-5
        except (TypeError, ValueError):
            winning_score_ok = False
        try:
            transformer_year_ok = int(result_json.get("transformer_paper_year")) == EXPECTED_YEAR
        except (TypeError, ValueError):
            transformer_year_ok = False
        authors_text = normalize_text(json.dumps(result_json.get("transformer_authors", ""), ensure_ascii=False))
        transformer_authors_ok = all(token in authors_text for token in EXPECTED_AUTHOR_TOKENS)
        evidence_used = result_json.get("evidence_used")
        if isinstance(evidence_used, str):
            evidence_used_ok = bool(evidence_used.strip())
        elif isinstance(evidence_used, list):
            evidence_used_ok = any(str(item).strip() for item in evidence_used)
        elif isinstance(evidence_used, dict):
            evidence_used_ok = any(str(value).strip() for value in evidence_used.values())

    ok = (
        proc.returncode == 0
        and bool(trace_rows)
        and tool_calls_seen >= len(REQUIRED_TOOL_NAMES)
        and required_tools_ok
        and required_keys_ok
        and visited_url_ok
        and winning_model_ok
        and winning_score_ok
        and transformer_year_ok
        and transformer_authors_ok
        and evidence_used_ok
        and training_trace_valid
        and not trace_issues
        and not other_protocol_error_seen
    )

    if ok:
        result = AgentRunResult(
            status="PASS",
            detail="Agent completed a real multi-round run with multiple tool calls and no assistant protocol errors.",
            tool_calls_seen=tool_calls_seen,
            distinct_tools_seen=distinct_tools_seen,
            output_preview=preview(combined_output),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    detail_parts = []
    if proc.returncode != 0:
        detail_parts.append(f"agent_base.react_agent exited with code {proc.returncode}")
    if not trace_rows:
        detail_parts.append(f"trace not found in directory: {TRACE_DIR}")
    if tool_calls_seen < len(REQUIRED_TOOL_NAMES):
        detail_parts.append(f"tool call count too low: {tool_calls_seen} < {len(REQUIRED_TOOL_NAMES)}")
    if not required_tools_ok:
        detail_parts.append(f"required tools missing: expected {REQUIRED_TOOL_NAMES}, got {distinct_tools_seen}")
    if not required_keys_ok:
        if result_json_error:
            detail_parts.append(f"final result_text is not valid required JSON ({result_json_error}): {result_text!r}")
        else:
            detail_parts.append(f"final result_text is not valid required JSON: {result_text!r}")
    if not winning_model_ok:
        detail_parts.append(f"winning_model mismatch: expected {EXPECTED_MODEL}")
    if not winning_score_ok:
        detail_parts.append(f"winning_score mismatch: expected {EXPECTED_SCORE}")
    if not transformer_year_ok:
        detail_parts.append(f"transformer_paper_year mismatch: expected {EXPECTED_YEAR}")
    if not transformer_authors_ok:
        detail_parts.append("transformer_authors missing expected author tokens")
    if not visited_url_ok:
        detail_parts.append(f"visited_url mismatch: expected {EXPECTED_VISITED_URL}")
    if not evidence_used_ok:
        detail_parts.append("evidence_used is empty")
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
