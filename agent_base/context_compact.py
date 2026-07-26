from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from agent_base.model_profiles import ModelProfile
from agent_base.utils import safe_jsonable


COMPACT_MEMORY_PREFIX = (
    "Runtime memory summary from earlier turns.\n"
    "This is compressed context, not ground truth.\n"
    "The workspace files remain authoritative; re-read any file if exact details matter.\n\n"
)


@dataclass
class CompactionOutcome:
    status: str
    compacted_messages: list[dict[str, Any]]
    summary_text: str = ""
    error: str = ""
    trigger_reason: str = ""
    prior_token_estimate: int = 0
    new_token_estimate: int = 0
    compacted_group_count: int = 0
    kept_group_count: int = 0
    existing_memory_text: str = ""
    summary_request: list[dict[str, Any]] | None = None
    summary_response: dict[str, Any] | None = None
    pre_messages: list[dict[str, Any]] | None = None
    post_messages: list[dict[str, Any]] | None = None


def should_compact_messages(
    *,
    last_input_tokens: Optional[int],
    current_token_estimate: int,
    model_profile: ModelProfile,
) -> tuple[bool, str]:
    usage_hit = last_input_tokens is not None and int(last_input_tokens) >= model_profile.compact_trigger_tokens
    estimate_hit = current_token_estimate >= model_profile.compact_trigger_tokens
    if usage_hit and estimate_hit:
        return True, "usage+estimate"
    if usage_hit:
        return True, "usage"
    if estimate_hit:
        return True, "estimate"
    return False, ""


def compact_messages(
    *,
    messages: Sequence[dict[str, Any]],
    original_prompt_text: str,
    model_name: str,
    model_profile: ModelProfile,
    llm_caller: Callable[..., dict[str, Any]],
    token_counter: Callable[[Sequence[dict[str, Any]]], int],
    runtime_deadline: Optional[float] = None,
) -> CompactionOutcome:
    safe_messages = [dict(message) for message in messages]
    if len(safe_messages) <= 2:
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            pre_messages=safe_messages,
            post_messages=safe_messages,
            error="context compaction requires at least one conversational turn beyond the initial prompt",
        )

    prior_token_estimate = token_counter(safe_messages)
    existing_memory_text, eligible_messages = _split_existing_memory_messages(safe_messages[2:])
    turn_groups = _turn_groups(eligible_messages)
    if not turn_groups:
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            pre_messages=safe_messages,
            post_messages=safe_messages,
            error="context compaction found no eligible conversational turns",
        )

    compacted_groups, recent_groups = _split_turn_groups(turn_groups, model_profile)
    if not compacted_groups:
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            pre_messages=safe_messages,
            post_messages=safe_messages,
            error="context compaction did not find any older turns to summarize",
        )

    history_text = _render_history_text(compacted_groups, model_profile)
    prior_memory_block = ""
    if existing_memory_text:
        prior_memory_block = (
            "Previously compressed memory to preserve and refine:\n"
            f"{_truncate_summary_text(existing_memory_text, max_chars=max(1200, model_profile.context_window // 3))}\n\n"
        )
    summary_request = [
        {
            "role": "system",
            "content": (
                "You compress older tool-using agent history into short working memory for continued execution. "
                "Return plain text only. Do not call tools. Do not invent facts."
            ),
        },
        {
            "role": "user",
            "content": (
                "Summarize the earlier conversation history for a tool-using agent.\n\n"
                f"Original task:\n{original_prompt_text}\n\n"
                "Write a concise working memory with these sections:\n"
                "- Goal\n"
                "- Constraints\n"
                "- Files and artifacts\n"
                "- Evidence and results\n"
                "- Open issues\n"
                "- Next useful actions\n\n"
                "Rules:\n"
                "- Prefer concrete file paths, numeric results, and grounded facts.\n"
                "- Mention uncertainty when details may need to be re-read from files.\n"
                "- Merge any prior compressed memory with the newer history below into one refreshed memory.\n"
                "- Deduplicate repeated sections and do not repeat earlier summaries verbatim.\n"
                "- The workspace remains authoritative.\n\n"
                f"{prior_memory_block}"
                f"Older history to compress:\n{history_text}"
            ),
        },
    ]
    try:
        summary_reply = llm_caller(
            summary_request,
            runtime_deadline=runtime_deadline,
            max_output_tokens=model_profile.compact_summary_max_tokens,
        )
    except KeyboardInterrupt:
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            summary_request=summary_request,
            summary_response={
                "status": "error",
                "error": "context compaction provider call interrupted",
                "interrupted": True,
                "tool_calls": [],
            },
            pre_messages=safe_messages,
            post_messages=safe_messages,
            error="context compaction provider call interrupted",
            compacted_group_count=len(compacted_groups),
            kept_group_count=len(recent_groups),
        )
    if not isinstance(summary_reply, dict) or summary_reply.get("status") != "ok":
        error = summary_reply.get("error", "context compaction summary call failed") if isinstance(summary_reply, dict) else str(summary_reply)
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            summary_request=summary_request,
            summary_response=safe_jsonable(summary_reply) if isinstance(summary_reply, dict) else {"status": "error", "error": error},
            pre_messages=safe_messages,
            post_messages=safe_messages,
            error=error,
            compacted_group_count=len(compacted_groups),
            kept_group_count=len(recent_groups),
        )

    if summary_reply.get("tool_calls"):
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            summary_request=summary_request,
            summary_response=safe_jsonable(summary_reply),
            pre_messages=safe_messages,
            post_messages=safe_messages,
            compacted_group_count=len(compacted_groups),
            kept_group_count=len(recent_groups),
            error="context compaction summary call returned tool calls",
        )

    summary_text = str(summary_reply.get("content", "") or "").strip()
    if not summary_text:
        return CompactionOutcome(
            status="error",
            compacted_messages=safe_messages,
            prior_token_estimate=prior_token_estimate,
            existing_memory_text=existing_memory_text,
            summary_request=summary_request,
            summary_response=safe_jsonable(summary_reply),
            pre_messages=safe_messages,
            post_messages=safe_messages,
            compacted_group_count=len(compacted_groups),
            kept_group_count=len(recent_groups),
            error="context compaction summary call returned empty text",
        )

    summary_message = {"role": "user", "content": COMPACT_MEMORY_PREFIX + summary_text}
    compacted_messages = safe_messages[:2] + [summary_message]
    for group in recent_groups:
        compacted_messages.extend(group)
    new_token_estimate = token_counter(compacted_messages)
    return CompactionOutcome(
        status="ok",
        compacted_messages=compacted_messages,
        summary_text=summary_text,
        prior_token_estimate=prior_token_estimate,
        new_token_estimate=new_token_estimate,
        compacted_group_count=len(compacted_groups),
        kept_group_count=len(recent_groups),
        existing_memory_text=existing_memory_text,
        summary_request=summary_request,
        summary_response=safe_jsonable(summary_reply),
        pre_messages=safe_messages,
        post_messages=compacted_messages,
    )


def _turn_groups(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", ""))
        if role == "assistant" and current_group:
            groups.append(current_group)
            current_group = [message]
            continue
        current_group.append(message)
    if current_group:
        groups.append(current_group)
    return groups


def _split_existing_memory_messages(messages: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    existing_summaries: list[str] = []
    remaining_messages: list[dict[str, Any]] = []
    preserving_summary_prefix = True
    for message in messages:
        content = message.get("content", "")
        if (
            preserving_summary_prefix
            and str(message.get("role", "")) == "user"
            and isinstance(content, str)
            and content.startswith(COMPACT_MEMORY_PREFIX)
        ):
            existing_summaries.append(content[len(COMPACT_MEMORY_PREFIX) :].strip())
            continue
        preserving_summary_prefix = False
        remaining_messages.append(dict(message))
    merged_summary = "\n\n".join(summary for summary in existing_summaries if summary).strip()
    return merged_summary, remaining_messages


def _split_turn_groups(turn_groups: Sequence[Sequence[dict[str, Any]]], model_profile: ModelProfile) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    recent_char_budget = max(400, model_profile.recent_history_budget_tokens * 4)
    recent_groups: list[list[dict[str, Any]]] = []
    recent_chars = 0

    for group in reversed(turn_groups):
        rendered = _render_group(group, max_chars_per_message=240)
        if recent_groups and recent_chars >= recent_char_budget:
            break
        recent_groups.insert(0, [dict(message) for message in group])
        recent_chars += len(rendered)
        if len(recent_groups) >= 4:
            break

    if len(recent_groups) >= len(turn_groups):
        recent_groups = recent_groups[1:]
    compacted_count = max(0, len(turn_groups) - len(recent_groups))
    compacted_groups = [[dict(message) for message in group] for group in turn_groups[:compacted_count]]
    return compacted_groups, recent_groups


def _render_history_text(turn_groups: Sequence[Sequence[dict[str, Any]]], model_profile: ModelProfile) -> str:
    max_history_chars = max(600, min(64000, model_profile.context_window * 2))
    max_chars_per_message = max(200, min(4000, max_history_chars // 10))
    parts: list[str] = []
    used = 0
    for index, group in enumerate(turn_groups, start=1):
        rendered = f"[Turn group {index}]\n{_render_group(group, max_chars_per_message=max_chars_per_message)}"
        if parts and used + len(rendered) > max_history_chars:
            remaining = max_history_chars - used
            if remaining > 80:
                parts.append(rendered[: remaining - 40].rstrip() + "\n...[history truncated]")
            break
        parts.append(rendered)
        used += len(rendered)
    return "\n\n".join(parts).strip()


def _render_group(group: Sequence[dict[str, Any]], *, max_chars_per_message: int) -> str:
    lines: list[str] = []
    for message in group:
        role = str(message.get("role", ""))
        content = _message_excerpt(message, max_chars=max_chars_per_message)
        lines.append(f"{role}: {content}")
    return "\n".join(lines).strip()


def _message_excerpt(message: dict[str, Any], *, max_chars: int) -> str:
    content = message.get("content", "")
    text: str
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, dict) and part.get("type") == "image_url":
                parts.append("[image_url]")
            else:
                parts.append(str(part))
        text = " ".join(part for part in parts if part)
    else:
        text = str(content)
    tool_calls = message.get("tool_calls")
    if tool_calls:
        tool_names = []
        for tool_call in tool_calls:
            function_block = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
            tool_names.append(str(function_block.get("name", "")))
        if tool_names:
            text = (text + "\nTool calls: " + ", ".join(name for name in tool_names if name)).strip()
    compacted = " ".join(text.split())
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 16].rstrip() + "...[truncated]"


def _truncate_summary_text(text: str, *, max_chars: int) -> str:
    compacted = " ".join(str(text).split())
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 16].rstrip() + "...[truncated]"
