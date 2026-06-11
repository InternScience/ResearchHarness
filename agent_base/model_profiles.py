from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModelProfile:
    family: str
    context_window: int
    output_reserve_tokens: int
    compact_buffer_tokens: int
    recent_history_budget_tokens: int
    compact_summary_max_tokens: int
    compact_trigger_tokens_override: Optional[int] = None

    @property
    def compact_trigger_tokens(self) -> int:
        if self.compact_trigger_tokens_override is not None:
            if self.compact_trigger_tokens_override <= 0:
                raise ValueError("compact_trigger_tokens must be positive.")
            return self.compact_trigger_tokens_override
        trigger_tokens = self.context_window - self.output_reserve_tokens - self.compact_buffer_tokens
        if trigger_tokens <= 0:
            raise ValueError(
                "max_input_tokens must be larger than max_output_tokens + compact_summary_max_tokens."
            )
        return trigger_tokens


def _model_family(model_name: str) -> str:
    normalized = str(model_name or "").strip().casefold()
    if "gemini" in normalized:
        return "gemini"
    if "claude" in normalized:
        return "claude"
    if "deepseek" in normalized:
        return "deepseek"
    if "qwen" in normalized:
        return "qwen"
    if "glm" in normalized:
        return "glm"
    if "gpt" in normalized or "o1" in normalized or "o3" in normalized or "o4" in normalized:
        return "gpt"
    return "generic"


def resolve_model_profile(
    model_name: str,
    *,
    configured_max_input_tokens: int,
    configured_max_output_tokens: int,
    configured_recent_history_budget_tokens: int,
    configured_compact_summary_max_tokens: int,
    compact_trigger_tokens: Any = None,
) -> ModelProfile:
    context_window = int(configured_max_input_tokens)
    if context_window <= 0:
        raise ValueError("max_input_tokens must be positive.")
    output_reserve_tokens = int(configured_max_output_tokens)
    if output_reserve_tokens <= 0:
        raise ValueError("max_output_tokens must be positive.")
    if output_reserve_tokens >= context_window:
        raise ValueError("max_output_tokens must be smaller than max_input_tokens.")
    recent_history_budget_tokens = int(configured_recent_history_budget_tokens)
    if recent_history_budget_tokens <= 0:
        raise ValueError("recent_history_budget_tokens must be positive.")
    compact_summary_max_tokens = int(configured_compact_summary_max_tokens)
    if compact_summary_max_tokens <= 0:
        raise ValueError("compact_summary_max_tokens must be positive.")
    if output_reserve_tokens + compact_summary_max_tokens >= context_window:
        raise ValueError(
            "max_output_tokens + compact_summary_max_tokens must be smaller than max_input_tokens."
        )
    compact_buffer_tokens = compact_summary_max_tokens
    compact_trigger_override = parse_compact_trigger_tokens(compact_trigger_tokens, context_window=context_window)
    if compact_trigger_override is not None:
        if compact_trigger_override <= 0:
            raise ValueError("compact_trigger_tokens must be positive.")
        if compact_trigger_override >= context_window - output_reserve_tokens:
            raise ValueError("compact_trigger_tokens must be smaller than max_input_tokens - max_output_tokens.")

    family = _model_family(model_name)

    return ModelProfile(
        family=family,
        context_window=context_window,
        output_reserve_tokens=output_reserve_tokens,
        compact_buffer_tokens=compact_buffer_tokens,
        recent_history_budget_tokens=recent_history_budget_tokens,
        compact_summary_max_tokens=compact_summary_max_tokens,
        compact_trigger_tokens_override=compact_trigger_override,
    )


def parse_compact_trigger_tokens(value: Any, *, context_window: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("compact trigger tokens must not be a boolean.")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip().casefold()
        if not text:
            return None
        multiplier = 1
        if text.endswith("k"):
            multiplier = 1024
            text = text[:-1].strip()
        elif text.endswith("m"):
            multiplier = 1024 * 1024
            text = text[:-1].strip()
        text = text.replace("_", "").replace(",", "")
        parsed = int(text) * multiplier
    return parsed
