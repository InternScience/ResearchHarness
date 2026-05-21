"""Public import surface for ResearchHarness."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("researchharness")
except PackageNotFoundError:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    __version__ = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0.0.0"

from researchharness.runtime import create_agent, run_agent

__all__ = ["__version__", "create_agent", "run_agent"]

