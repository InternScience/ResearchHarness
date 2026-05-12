#!/usr/bin/env python3

import json
import io
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_support import EXAMPLE_TEXT_FILES_DIR, TEST_RUNS_DIR, bootstrap, has_structai, required_test_image, required_test_pdf


TMP_DIR = TEST_RUNS_DIR / "local_tools_validation"


@dataclass
class LocalToolsResult:
    status: str
    detail: str
    tools_called: list[str]
    output_preview: str


def preview(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def main() -> int:
    bootstrap()

    from agent_base.tools.tool_file import Edit, Glob, Grep, Read, ReadImage, ReadPDF, Write
    from agent_base.tools.tool_runtime import Bash, TerminalInterrupt, TerminalKill, TerminalRead, TerminalStart, TerminalWrite
    from agent_base.tools.tool_user import AskUser

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    file_path = TMP_DIR / "demo.txt"
    file_corpus_dir = EXAMPLE_TEXT_FILES_DIR
    pdf_path = required_test_pdf()
    image_path = required_test_image()
    outputs: list[str] = []
    tools_called: list[str] = []

    create_tool = Write()
    create_output = create_tool.call(
        {
            "path": str(file_path),
            "content": "hello world\nsecond line\n",
            "overwrite": True,
        }
    )
    tools_called.append("Write")
    outputs.append(create_output)

    edit_tool = Edit()
    edit_output = edit_tool.call(
        {
            "path": str(file_path),
            "patch": "@@ -1,2 +1,2 @@\n-hello world\n+hello agent\n second line",
        }
    )
    tools_called.append("Edit")
    outputs.append(edit_output)

    read_tool = Read()
    read_output = read_tool.call(
        {
            "path": str(file_path),
            "start_line": 1,
            "end_line": 1,
            "max_chars": 100,
        }
    )
    tools_called.append("Read")
    outputs.append(read_output)

    glob_tool = Glob()
    glob_output = glob_tool.call(
        {
            "pattern": "**/*.txt",
            "path": str(file_corpus_dir),
            "max_results": 20,
        }
    )
    tools_called.append("Glob")
    outputs.append(glob_output)

    grep_tool = Grep()
    grep_output = grep_tool.call(
        {
            "pattern": "Hello\\.",
            "path": str(file_corpus_dir),
            "glob": "**/*.txt",
            "max_results": 20,
        }
    )
    tools_called.append("Grep")
    outputs.append(grep_output)

    if not has_structai():
        result = LocalToolsResult(
            status="FAIL",
            detail="Missing required dependency: structai.",
            tools_called=tools_called,
            output_preview=preview("\n\n".join(outputs)),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 1
    read_pdf_tool = ReadPDF()
    read_pdf_output = read_pdf_tool.call(
        {
            "path": str(pdf_path),
            "max_chars": 1500,
        }
    )
    tools_called.append("ReadPDF")
    outputs.append(read_pdf_output)

    read_image_tool = ReadImage()
    read_image_output = read_image_tool.call(
        {
            "path": str(image_path),
        }
    )
    tools_called.append("ReadImage")
    outputs.append(read_image_output)

    bash_tool = Bash()
    bash_output = bash_tool.call(
        {
            "command": "pwd && cat demo.txt",
            "timeout": 10,
            "workdir": str(TMP_DIR),
        }
    )
    tools_called.append("Bash")
    outputs.append(bash_output)

    ask_user_tool = AskUser()
    ask_user_prompt_output = io.StringIO()
    ask_user_output = ask_user_tool.call(
        {
            "question": "Which validation option should be used?",
            "context": "Testing controlled AskUser input.",
        },
        input_stream=io.StringIO("Use the deterministic option.\n"),
        output_stream=ask_user_prompt_output,
    )
    tools_called.append("AskUser")
    outputs.append(ask_user_output)

    terminal_start = TerminalStart()
    terminal_start_output = terminal_start.call({"cwd": str(TMP_DIR)})
    tools_called.append("TerminalStart")
    outputs.append(terminal_start_output)
    session_match = re.search(r"session_id: (term_\d+)", terminal_start_output)
    session_id = session_match.group(1) if session_match else None

    if not session_id:
        result = LocalToolsResult(
            status="FAIL",
            detail="terminal_start did not return a valid session_id.",
            tools_called=tools_called,
            output_preview=preview("\n\n".join(outputs)),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 1

    terminal_write = TerminalWrite()
    terminal_write_output = terminal_write.call(
        {
            "session_id": session_id,
            "input": "printf 'alpha\\n'; printf 'beta\\n' > session_file.txt; pwd",
            "yield_time_ms": 300,
        }
    )
    tools_called.append("TerminalWrite")
    outputs.append(terminal_write_output)

    terminal_read = TerminalRead()
    terminal_read_output = terminal_read.call(
        {
            "session_id": session_id,
            "yield_time_ms": 200,
            "max_output_chars": 4000,
        }
    )
    tools_called.append("TerminalRead")
    outputs.append(terminal_read_output)

    terminal_interrupt = TerminalInterrupt()
    interrupt_write_output = terminal_write.call(
        {
            "session_id": session_id,
            "input": "sleep 10",
            "yield_time_ms": 150,
        }
    )
    tools_called.append("TerminalWrite")
    outputs.append(interrupt_write_output)

    terminal_interrupt_output = terminal_interrupt.call(
        {
            "session_id": session_id,
            "max_output_chars": 4000,
        }
    )
    tools_called.append("TerminalInterrupt")
    outputs.append(terminal_interrupt_output)

    terminal_kill = TerminalKill()
    terminal_kill_output = terminal_kill.call({"session_id": session_id})
    tools_called.append("TerminalKill")
    outputs.append(terminal_kill_output)

    combined = "\n\n".join(outputs)
    ok = (
        "Wrote file" in create_output
        and "Updated file" in edit_output
        and "hello agent" in read_output
        and "source_type: text" in read_output
        and "start_line: 1" in read_output
        and "end_line: 1" in read_output
        and "hello.txt" in glob_output
        and "match_count:" in glob_output
        and "hello.txt:1: Hello." in grep_output
        and "match_count:" in grep_output
        and "source_type: pdf" in read_pdf_output
        and "Dummy PDF file" in read_pdf_output
        and "format: JPEG" in read_image_output
        and "source_type: image" in read_image_output
        and "mime_type: image/jpeg" in read_image_output
        and "width: 1280" in read_image_output
        and "height: 720" in read_image_output
        and "llm_image_attached: true" in read_image_output
        and "exit_code: 0" in bash_output
        and "hello agent" in bash_output
        and "User answer" in ask_user_output
        and "deterministic option" in ask_user_output
        and "Which validation option should be used?" in ask_user_prompt_output.getvalue()
        and "Started terminal session" in terminal_start_output
        and "Session updated" in terminal_write_output
        and "alpha" in (terminal_write_output + terminal_read_output)
        and str(TMP_DIR) in terminal_write_output
        and "Session updated" in interrupt_write_output
        and "Sent Ctrl-C" in terminal_interrupt_output
        and "alive: true" in terminal_interrupt_output
        and "Terminal session terminated" in terminal_kill_output
    )

    if ok:
        result = LocalToolsResult(
            status="PASS",
            detail="Local glob/grep/read/create/edit/bash/ask-user/terminal/pdf/image tools all executed successfully.",
            tools_called=tools_called,
            output_preview=preview(combined),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    result = LocalToolsResult(
        status="FAIL",
        detail="One or more local tools returned unexpected output.",
        tools_called=tools_called,
        output_preview=preview(combined),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
