import argparse
import datetime
from pathlib import Path
import re
from typing import Any, Callable, Optional
from uuid import uuid4

from agent_base.utils import append_jsonl, safe_jsonable


TRACE_FIELD_NAMES = [
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
]


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class FlatTraceWriter:
    def __init__(
        self,
        *,
        trace_dir: Optional[str | Path],
        model_name: str,
        workspace_root: str | Path,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        run_id: Optional[str] = None,
    ):
        self.model_name = model_name
        self.workspace_root = str(workspace_root)
        self.on_event = on_event
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError("run_id must be a string")
        resolved_run_id = run_id if run_id is not None else uuid4().hex
        if _RUN_ID_RE.fullmatch(resolved_run_id) is None:
            raise ValueError(
                "run_id must be 1-128 characters using only letters, digits, '.', '_', or '-'"
            )
        self.run_id = resolved_run_id
        self.path = resolve_trace_path(trace_dir, run_id=self.run_id) if trace_dir else None
        self.event_index = 0

    def append(
        self,
        *,
        role: str,
        text: str = "",
        turn_index: int = 0,
        tool_call_ids: Optional[list[str]] = None,
        tool_names: Optional[list[str]] = None,
        tool_arguments: Optional[list[Any]] = None,
        finish_reason: Optional[str] = None,
        termination: Optional[str] = None,
        error: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        capture_type: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.event_index += 1
        row = {
            "run_id": self.run_id,
            "event_index": self.event_index,
            "turn_index": turn_index,
            "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "model_name": self.model_name,
            "workspace_root": self.workspace_root,
            "role": role,
            "text": text,
            "tool_call_ids": tool_call_ids or [],
            "tool_names": tool_names or [],
            "tool_arguments": safe_jsonable(tool_arguments or []),
            "finish_reason": finish_reason or "",
            "termination": termination or "",
            "error": error or "",
            "image_paths": image_paths or [],
            "capture_type": capture_type or "",
            "payload": safe_jsonable(payload or {}),
        }
        if self.path is not None:
            append_jsonl(self.path, row)
        if self.on_event is not None:
            self.on_event(row)
        return row


def resolve_trace_path(
    trace_dir: str | Path,
    *,
    run_id: str,
    prefix: str = "trace",
    suffix: str = ".jsonl",
) -> Path:
    directory = Path(trace_dir)
    timestamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    short_run_id = run_id[:12]
    filename = f"{prefix}_{timestamp}_{short_run_id}{suffix}"
    return directory / filename


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the flat trace field order used by the agent.")
    parser.parse_args(argv)
    print("\n".join(TRACE_FIELD_NAMES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
