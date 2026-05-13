#!/usr/bin/env python3

import contextlib
import io
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_base.console_utils import ConsoleEventPrinter
from agent_base.console_utils import _display_width


@dataclass
class ConsoleUtilsResult:
    status: str
    detail: str
    output_preview: str


def main() -> int:
    old_force_color = os.environ.get("FORCE_COLOR")
    old_no_color = os.environ.get("NO_COLOR")
    old_columns = os.environ.get("COLUMNS")
    os.environ["FORCE_COLOR"] = "1"
    os.environ.pop("NO_COLOR", None)
    os.environ["COLUMNS"] = "110"
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            printer = ConsoleEventPrinter(
                model_name="demo-model",
                workspace_root=Path("/tmp/demo-workspace"),
                prompt="Summarize **this** task.",
            )
            printer.print_header()
            printer.handle_event(
                {
                    "role": "assistant",
                    "turn_index": 1,
                    "text": "",
                    "tool_names": ["Read"],
                    "tool_arguments": [{"path": "demo.txt"}],
                    "finish_reason": "tool_calls",
                }
            )
            printer.handle_event(
                {
                    "role": "tool",
                    "turn_index": 1,
                    "text": "demo output",
                    "tool_names": ["Read"],
                }
            )
            printer.handle_event(
                {
                    "role": "runtime",
                    "turn_index": 2,
                    "text": "done",
                    "error": "sample error",
                }
            )
        output = buffer.getvalue()

        cjk_buffer = io.StringIO()
        os.environ.pop("FORCE_COLOR", None)
        os.environ["NO_COLOR"] = "1"
        with contextlib.redirect_stdout(cjk_buffer):
            cjk_printer = ConsoleEventPrinter(
                model_name="gpt-5.4",
                workspace_root=Path("/mnt/d/xwh/ailab记录/工作/26年04月/ResearchHarness/workspace"),
                prompt="Who proposed the transformer architecture, and in what year was the paper published?",
            )
            cjk_printer.print_header()
            cjk_printer.handle_event(
                {
                    "role": "assistant",
                    "turn_index": 1,
                    "text": (
                        "Transformer 架构是由 Ashish Vaswani、Noam Shazeer、Niki Parmar、Jakob Uszkoreit、"
                        "Llion Jones、Aidan N. Gomez、Lukasz Kaiser 和 Illia Polosukhin 在论文"
                        "《Attention Is All You Need》中提出的，发表于 2017 年。"
                    ),
                }
            )
        cjk_output = cjk_buffer.getvalue()
    finally:
        if old_force_color is None:
            os.environ.pop("FORCE_COLOR", None)
        else:
            os.environ["FORCE_COLOR"] = old_force_color
        if old_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = old_no_color
        if old_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old_columns

    boxed_cjk_lines = [line for line in cjk_output.splitlines() if line.startswith(("+", "|"))]
    cjk_line_widths = {_display_width(line) for line in boxed_cjk_lines}

    checks = [
        "+ ResearchHarness CLI" in output,
        "+ Assistant | round 1" in output,
        "+ Read Result | round 1" in output,
        "+ Runtime | round 2" in output,
        "Assistant Tool Calls:" in output,
        "demo output" in output,
        "Runtime Error: sample error" in output,
        "== Round" not in output,
        "[Assistant]" not in output,
        "\033[" in output,
        "Transformer 架构是由" in cjk_output,
        len(cjk_line_widths) == 1,
    ]
    result = ConsoleUtilsResult(
        status="PASS" if all(checks) else "FAIL",
        detail="CLI console formatter uses boxed colored steps.",
        output_preview=(output + "\n" + cjk_output)[:1600],
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
