import json
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shlex
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_support import EXAMPLE_TEXT_FILES_DIR, bootstrap


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


def preview(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


@dataclass
class EndToEndResult:
    status: str
    detail: str
    tools_called: list[str]
    output_preview: str


def extract_winning_score(text: str) -> tuple[str | None, float | None]:
    winner_match = re.search(r"WINNER=([A-Za-z0-9_-]+)", text)
    score_match = re.search(r"WINNING_SCORE=([0-9]+\.[0-9]+)", text)
    if not winner_match or not score_match:
        return None, None
    try:
        return winner_match.group(1), float(score_match.group(1))
    except ValueError:
        return None, None


def main() -> int:
    bootstrap()

    from agent_base.tools.tool_file import Read
    from agent_base.tools.tool_runtime import Bash
    from agent_base.tools.tool_web import ScholarSearch, WebFetch, WebSearch

    tools_called: list[str] = []
    artifacts: dict[str, str] = {}

    file_tool = Read()
    benchmark_path = EXAMPLE_TEXT_FILES_DIR / "benchmark_table.csv"
    file_output = file_tool.call({"path": str(benchmark_path)})
    tools_called.append("Read")
    artifacts["Read"] = file_output

    bash_tool = Bash()
    python_script = (
        "import csv\n"
        "from pathlib import Path\n"
        f"rows = list(csv.DictReader(Path({str(benchmark_path)!r}).open(encoding='utf-8')))\n"
        "best = None\n"
        "best_score = None\n"
        "for row in rows:\n"
        "    score = 0.45 * float(row['accuracy']) + 0.0008 * float(row['throughput']) - 0.015 * float(row['latency']) - 0.0012 * float(row['memory'])\n"
        "    print(f\"{row['model']}: {score:.5f}\")\n"
        "    if best_score is None or score > best_score:\n"
        "        best = row['model']\n"
        "        best_score = score\n"
        "print(f'WINNER={best}')\n"
        "print(f'WINNING_SCORE={best_score:.5f}')\n"
    )
    python_output = bash_tool.call(
        {
            "command": shlex.join([sys.executable, "-c", python_script]),
            "timeout": 60,
            "workdir": str(EXAMPLE_TEXT_FILES_DIR),
        }
    )
    tools_called.append("Bash")
    artifacts["Bash"] = python_output

    scholar_tool = ScholarSearch()
    scholar_output = scholar_tool.call({"query": ["Attention Is All You Need publication year"]})
    tools_called.append("ScholarSearch")
    artifacts["ScholarSearch"] = scholar_output

    search_tool = WebSearch()
    search_output = search_tool.call({"query": ["Attention Is All You Need authors list"]})
    tools_called.append("WebSearch")
    artifacts["WebSearch"] = search_output

    visit_tool = WebFetch()
    visit_output = visit_tool.call(
        {
            "url": [EXPECTED_VISITED_URL],
        }
    )
    tools_called.append("WebFetch")
    artifacts["WebFetch"] = visit_output

    combined_text = "\n\n".join(artifacts.values())
    normalized = normalize_text(combined_text)

    model, score = extract_winning_score(python_output)
    model_ok = model == EXPECTED_MODEL
    score_ok = score is not None and abs(score - EXPECTED_SCORE) < 1e-4
    year_ok = str(EXPECTED_YEAR) in scholar_output or str(EXPECTED_YEAR) in visit_output
    authors_ok = all(token in normalized for token in EXPECTED_AUTHOR_TOKENS)
    visit_ok = EXPECTED_VISITED_URL in visit_output or EXPECTED_VISITED_URL in combined_text

    checks = [model_ok, score_ok, year_ok, authors_ok, visit_ok]

    if all(checks):
        result = EndToEndResult(
            status="PASS",
            detail="All five tools executed successfully and produced the expected combined result for the complex case.",
            tools_called=tools_called,
            output_preview=preview(combined_text),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    detail_parts = []
    if not model_ok:
        detail_parts.append(f"unexpected winning model: {model}")
    if not score_ok:
        detail_parts.append(f"unexpected winning score: {score}")
    if not year_ok:
        detail_parts.append("publication year 2017 not found")
    if not authors_ok:
        detail_parts.append("expected author tokens not found")
    if not visit_ok:
        detail_parts.append(f"expected visit URL not confirmed: {EXPECTED_VISITED_URL}")

    result = EndToEndResult(
        status="FAIL",
        detail="; ".join(detail_parts),
        tools_called=tools_called,
        output_preview=preview(combined_text),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
