import json
import os
import shlex
import shutil
import sys
from pathlib import Path


def _resolve_root() -> Path:
    configured = os.getenv("RESEARCHHARNESS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


ROOT = _resolve_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_base.utils import MissingRequiredEnvError, load_dotenv, require_required_env


WORKSPACE_ROOT = ROOT / "workspace"
TEST_RUNS_DIR = WORKSPACE_ROOT / "test_runs"
API_RUNS_ROOT = ROOT / "api_runs"
API_TEST_RUNS_DIR = API_RUNS_ROOT / "test_runs"
EXAMPLE_FILES_DIR = ROOT / "tests" / "example_files"
EXAMPLE_TEXT_FILES_DIR = EXAMPLE_FILES_DIR / "files"
EXAMPLE_IMAGE_DIR = EXAMPLE_FILES_DIR / "images"
EXAMPLE_PDF_DIR = EXAMPLE_FILES_DIR / "pdfs"
REQUIRED_TEST_IMAGE = EXAMPLE_IMAGE_DIR / "complex_scene.jpg"
REQUIRED_TEST_PDF = EXAMPLE_PDF_DIR / "dummy_document.pdf"
SUBPROCESS_PYTHON_ENV = "RESEARCHHARNESS_TEST_PYTHON"
TRACE_REQUIRED_KEYS = {
    "run_id",
    "event_index",
    "turn_index",
    "timestamp",
    "model_name",
    "workspace_root",
    "role",
    "text",
    "tool_call_ids",
    "tool_names",
    "tool_arguments",
    "finish_reason",
    "termination",
    "error",
    "image_paths",
    "capture_type",
    "payload",
}


def bootstrap() -> None:
    load_dotenv(ROOT / ".env")
    require_test_env()
    os.environ["WORKSPACE_ROOT"] = str(ROOT)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def require_test_env() -> None:
    try:
        require_required_env("ResearchHarness tests")
    except MissingRequiredEnvError as exc:
        raise SystemExit(str(exc)) from None


def subprocess_python() -> list[str]:
    configured = os.getenv(SUBPROCESS_PYTHON_ENV, "").strip()
    if configured:
        return shlex.split(configured)
    return [sys.executable]


def preview(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def load_trace_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def single_trace_path(trace_dir: Path) -> Path | None:
    if not trace_dir.exists():
        return None
    matches = sorted(trace_dir.glob("*.jsonl"))
    if len(matches) != 1:
        return None
    return matches[0]


def collect_tool_names(rows: list[dict]) -> list[str]:
    tool_names_seen: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "tool":
            continue
        tool_names = row.get("tool_names")
        if isinstance(tool_names, list):
            tool_names_seen.extend(str(name) for name in tool_names if str(name).strip())
    return tool_names_seen


def trace_rows_have_uniform_keys(rows: list[dict]) -> bool:
    if not rows:
        return False
    for row in rows:
        if not isinstance(row, dict) or set(row.keys()) != TRACE_REQUIRED_KEYS:
            return False
    return True


def training_trace_ok(rows: list[dict]) -> bool:
    if not trace_rows_have_uniform_keys(rows) or len(rows) < 3:
        return False
    run_ids = {str(row.get("run_id", "")) for row in rows}
    if len(run_ids) != 1 or "" in run_ids:
        return False
    if [row.get("event_index") for row in rows] != list(range(1, len(rows) + 1)):
        return False
    if rows[0].get("role") != "system" or rows[1].get("role") != "user":
        return False
    has_tool_turn = any(row.get("role") == "assistant" and row.get("tool_names") for row in rows)
    has_tool_result = any(row.get("role") == "tool" and row.get("tool_names") for row in rows)
    has_result_turn = any(
        row.get("role") == "assistant"
        and isinstance(row.get("text"), str)
        and row.get("text", "").strip()
        and row.get("termination") == "result"
        for row in rows
    )
    for row in rows:
        if not isinstance(row.get("tool_names"), list):
            return False
        if not isinstance(row.get("tool_arguments"), list):
            return False
        if not isinstance(row.get("tool_call_ids"), list):
            return False
        if not isinstance(row.get("capture_type"), str):
            return False
        if not isinstance(row.get("payload"), dict):
            return False
    return has_tool_turn and has_tool_result and has_result_turn


def final_result_text(rows: list[dict]) -> str | None:
    for row in reversed(rows):
        if row.get("termination") == "result" and row.get("role") == "assistant":
            text = row.get("text")
            return text if isinstance(text, str) else None
    return None


def collect_trace_errors(rows: list[dict]) -> list[str]:
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        error = row.get("error")
        if isinstance(error, str) and error:
            issues.append(error)
    return issues


def has_structai() -> bool:
    try:
        import structai  # noqa: F401
    except ImportError:
        return False
    return True


def required_test_image() -> Path:
    if not REQUIRED_TEST_IMAGE.exists():
        raise FileNotFoundError(f"Required test image is missing: {REQUIRED_TEST_IMAGE}")
    return REQUIRED_TEST_IMAGE


def required_test_pdf() -> Path:
    if not REQUIRED_TEST_PDF.exists():
        raise FileNotFoundError(f"Required test PDF is missing: {REQUIRED_TEST_PDF}")
    return REQUIRED_TEST_PDF


def clear_pdf_parse_cache(pdf_path: Path) -> Path:
    cache_dir = pdf_path.with_suffix("")
    if cache_dir.exists():
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
        else:
            cache_dir.unlink()
    if cache_dir.exists():
        raise RuntimeError(f"Failed to clear ReadPDF cache directory: {cache_dir}")
    return cache_dir


def main() -> int:
    bootstrap()
    print(f"ROOT={ROOT}")
    print(f"WORKSPACE_ROOT={WORKSPACE_ROOT}")
    print(f"TEST_RUNS_DIR={TEST_RUNS_DIR}")
    print(f"EXAMPLE_TEXT_FILES_DIR={EXAMPLE_TEXT_FILES_DIR}")
    print(f"structai_available={has_structai()}")
    print(f"required_test_image={required_test_image()}")
    print(f"required_test_pdf={required_test_pdf()}")
    print(f"subprocess_python={subprocess_python()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
