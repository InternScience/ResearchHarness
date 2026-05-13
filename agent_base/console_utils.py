import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import unicodedata
from typing import Any, Optional


ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "header": "\033[36m",
    "assistant": "\033[32m",
    "tool": "\033[33m",
    "runtime": "\033[34m",
    "user": "\033[35m",
    "error": "\033[31m",
}


def _char_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in {"Cc", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def _display_width(text: str) -> int:
    return sum(_char_display_width(char) for char in str(text))


def _truncate_display(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    suffix = "..."
    target = max(0, width - _display_width(suffix))
    out = []
    used = 0
    for char in text:
        char_width = _char_display_width(char)
        if used + char_width > target:
            break
        out.append(char)
        used += char_width
    return "".join(out) + suffix


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _last_soft_break(chars: list[str]) -> int:
    for index in range(len(chars) - 1, 0, -1):
        if chars[index].isspace() and "".join(chars[:index]).strip():
            return index
    return -1


class ConsoleEventPrinter:
    def __init__(self, *, model_name: str, workspace_root: Path, prompt: str):
        self.model_name = model_name
        self.workspace_root = workspace_root
        self.prompt = prompt.strip()
        self._printed_any = False
        self._use_color = (
            "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")))
        )

    def print_header(self) -> None:
        self._print_box(
            "ResearchHarness CLI",
            f"Model: {self.model_name}\nWorkspace Root: {self.workspace_root}\n\nPrompt:\n{self.prompt}",
            "header",
        )

    def reset_rounds(self) -> None:
        self._printed_any = False

    def _paint(self, text: str, color_key: str) -> str:
        if not self._use_color:
            return text
        return f"{ANSI_COLORS.get(color_key, '')}{text}{ANSI_RESET}"

    def _terminal_width(self) -> int:
        return max(60, min(110, shutil.get_terminal_size((100, 20)).columns))

    def _wrap_line(self, line: str, width: int) -> list[str]:
        expanded = line.expandtabs(2)
        if expanded == "":
            return [""]
        chunks: list[str] = []
        current: list[str] = []
        current_width = 0
        for char in expanded:
            char_width = _char_display_width(char)
            if current and current_width + char_width > width:
                break_at = _last_soft_break(current)
                if break_at > 0:
                    chunks.append("".join(current[:break_at]).rstrip())
                    current = list("".join(current[break_at + 1 :]).lstrip())
                    current_width = _display_width("".join(current))
                else:
                    chunks.append("".join(current))
                    current = []
                    current_width = 0
            current.append(char)
            current_width += char_width
        if current:
            chunks.append("".join(current))
        return chunks or [""]

    def _print_box(self, title: str, body: str, color_key: str = "runtime") -> None:
        width = self._terminal_width()
        inner_width = width - 4
        title_text = f" {_truncate_display(title.strip(), width - 6)} "
        top = "+" + title_text + "-" * max(0, width - 2 - _display_width(title_text)) + "+"
        bottom = "+" + "-" * (width - 2) + "+"
        if self._printed_any:
            print()
        print(self._paint(top, color_key))
        for raw_line in str(body or "").splitlines() or [""]:
            for line in self._wrap_line(raw_line, inner_width):
                padded = _pad_display(line, inner_width)
                print(f"{self._paint('|', color_key)} {padded} {self._paint('|', color_key)}")
        print(self._paint(bottom, color_key))
        self._printed_any = True

    def _title(self, label: str, turn_index: int) -> str:
        return f"{label} | round {turn_index}" if turn_index > 0 else label

    def _format_tool_call(self, tool_name: str, tool_args: Any) -> str:
        try:
            tool_args_text = json.dumps(tool_args, ensure_ascii=False, indent=2)
        except TypeError:
            tool_args_text = str(tool_args)
        return f"- {tool_name}\n{tool_args_text}"

    def handle_event(self, row: dict[str, Any]) -> None:
        role = str(row.get("role", ""))
        turn_index = int(row.get("turn_index", 0) or 0)
        text = str(row.get("text", ""))
        capture_type = str(row.get("capture_type", ""))
        tool_names = row.get("tool_names") if isinstance(row.get("tool_names"), list) else []
        tool_arguments = row.get("tool_arguments") if isinstance(row.get("tool_arguments"), list) else []
        finish_reason = str(row.get("finish_reason", ""))
        error = str(row.get("error", ""))

        if capture_type and not text.strip():
            return

        if role == "system":
            return

        if role == "user":
            if turn_index == 0:
                return
            self._print_box(self._title("Runtime Message", turn_index), text, "user")
            return

        if role == "assistant":
            lines: list[str] = []
            if tool_names:
                if text.strip():
                    lines.append(text)
                else:
                    suffix = f" finish_reason={finish_reason}" if finish_reason else ""
                    lines.append(f"(no text; native tool-calls only.{suffix})")
                lines.append("")
                lines.append("Assistant Tool Calls:")
                for idx, tool_name in enumerate(tool_names):
                    tool_args = tool_arguments[idx] if idx < len(tool_arguments) else {}
                    lines.append(self._format_tool_call(str(tool_name), tool_args))
            elif text.strip():
                lines.append(text)
            else:
                suffix = f" finish_reason={finish_reason}" if finish_reason else ""
                lines.append(f"(empty assistant output.{suffix})")
            if error:
                lines.append("")
                lines.append(f"Assistant Error: {error}")
            self._print_box(self._title("Assistant", turn_index), "\n".join(lines), "error" if error else "assistant")
            return

        if role == "tool":
            tool_name = str(tool_names[0]) if tool_names else "Tool"
            lines = [text]
            if error:
                lines.extend(["", f"{tool_name} Error: {error}"])
            self._print_box(self._title(f"{tool_name} Result", turn_index), "\n".join(lines), "error" if error else "tool")
            return

        if role == "runtime":
            lines = [text]
            if error:
                lines.extend(["", f"Runtime Error: {error}"])
            self._print_box(self._title("Runtime", turn_index), "\n".join(lines), "error" if error else "runtime")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Show a minimal example of the CLI console event formatter.")
    parser.parse_args(argv)
    printer = ConsoleEventPrinter(model_name="demo-model", workspace_root=Path("."), prompt="demo question")
    printer.print_header()
    printer.handle_event(
        {
            "role": "assistant",
            "turn_index": 1,
            "text": "",
            "tool_names": ["Read"],
            "tool_arguments": [{"path": "demo.txt"}],
            "termination": "",
            "error": "",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
