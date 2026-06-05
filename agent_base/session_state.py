from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_base.model_profiles import ModelProfile
from agent_base.utils import safe_jsonable


SESSION_STATE_PREFIX = "session_state"


@dataclass
class CompactionRecord:
    turn_index: int
    status: str
    trigger_reason: str
    prior_token_estimate: int
    prior_message_count: int
    compacted_group_count: int = 0
    kept_group_count: int = 0
    new_token_estimate: Optional[int] = None
    new_message_count: Optional[int] = None
    summary_text: str = ""
    error: str = ""


@dataclass
class AgentSessionState:
    run_id: str
    model_name: str
    workspace_root: str
    prompt: str
    trace_path: str = ""
    turn_index: int = 0
    max_rounds: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    last_input_tokens: Optional[int] = None
    current_token_estimate: int = 0
    termination: str = ""
    error: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[CompactionRecord] = field(default_factory=list)
    model_profile: Optional[ModelProfile] = None

    def capture_messages(self, messages: Sequence[dict[str, Any]]) -> None:
        self.messages = safe_jsonable(list(messages))

    def payload(self) -> dict[str, Any]:
        profile = self.model_profile
        return {
            "version": 1,
            "run_id": self.run_id,
            "model_name": self.model_name,
            "workspace_root": self.workspace_root,
            "prompt": self.prompt,
            "trace_path": self.trace_path,
            "turn_index": self.turn_index,
            "max_rounds": self.max_rounds,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "last_input_tokens": self.last_input_tokens,
            "current_token_estimate": self.current_token_estimate,
            "termination": self.termination,
            "error": self.error,
            "messages": self.messages,
            "compactions": [safe_jsonable(asdict(record)) for record in self.compactions],
            "model_profile": safe_jsonable(asdict(profile)) if profile is not None else None,
        }


def resolve_session_state_path(trace_path: str | Path) -> Path:
    trace = Path(trace_path)
    stem = trace.stem
    suffix = stem[len("trace_") :] if stem.startswith("trace_") else stem
    return trace.with_name(f"{SESSION_STATE_PREFIX}_{suffix}.json")


def persist_session_state(path: str | Path, state: AgentSessionState) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(state.payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
