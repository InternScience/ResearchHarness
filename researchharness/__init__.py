"""Public import surface for ResearchHarness."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("researchharness")
except PackageNotFoundError:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    __version__ = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0.0.0"

from agent_base.tools.tool_extra import StrReplaceEditor
from agent_base.tools.tool_file import Edit, Glob, Grep, Read, ReadImage, ReadPDF, Write
from agent_base.tools.tool_runtime import Bash, TerminalInterrupt, TerminalKill, TerminalRead, TerminalStart, TerminalWrite
from agent_base.tools.tooling import ToolBase
from agent_base.tools.tool_user import AskUser
from agent_base.tools.tool_web import ScholarSearch, WebFetch, WebSearch
from agent_base.react_agent import available_tool_schemas
from researchharness.runtime import create_agent, run_agent, tool

__all__ = [
    "__version__",
    "available_tool_schemas",
    "create_agent",
    "run_agent",
    "tool",
    "AskUser",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "Read",
    "ReadImage",
    "ReadPDF",
    "ScholarSearch",
    "StrReplaceEditor",
    "ToolBase",
    "TerminalInterrupt",
    "TerminalKill",
    "TerminalRead",
    "TerminalStart",
    "TerminalWrite",
    "WebFetch",
    "WebSearch",
    "Write",
]
