"""Small public Python API for embedding ResearchHarness."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

from agent_base.react_agent import ALL_TOOL_MAP, MultiTurnReactAgent, default_llm_config, default_tool_names
from agent_base.provider_compat import normalize_omit_generate_params
from agent_base.training_export import TrainingExportConfig
from agent_base.tools.custom import build_custom_tool_map, tool
from agent_base.tools.tooling import ToolBase
from agent_base.utils import load_default_dotenvs, read_role_prompt_files, require_required_env


def _apply_llm_overrides(
    llm: dict[str, Any],
    *,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    max_input_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    recent_history_budget_tokens: Optional[int] = None,
    compact_summary_max_tokens: Optional[int] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    omit_generate_params: Optional[str | Sequence[str]] = None,
    compact_trigger_tokens: Optional[int | str] = None,
    extra_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if api_key is not None:
        llm["api_key"] = str(api_key)
    if api_base is not None:
        llm["api_base"] = str(api_base)
    if timeout_seconds is not None:
        llm["timeout_seconds"] = float(timeout_seconds)
    generate_cfg = dict(llm.get("generate_cfg", {}))
    for key, value in (
        ("max_input_tokens", max_input_tokens),
        ("max_output_tokens", max_output_tokens),
        ("recent_history_budget_tokens", recent_history_budget_tokens),
        ("compact_summary_max_tokens", compact_summary_max_tokens),
        ("max_retries", max_retries),
        ("temperature", temperature),
        ("top_p", top_p),
        ("presence_penalty", presence_penalty),
        ("compact_trigger_tokens", compact_trigger_tokens),
    ):
        if value is not None:
            generate_cfg[key] = value
    if omit_generate_params is not None:
        generate_cfg["omit_generate_params"] = normalize_omit_generate_params(omit_generate_params)
    llm["generate_cfg"] = generate_cfg
    if extra_body is not None:
        if not isinstance(extra_body, dict):
            raise ValueError("extra_body must be a dict.")
        llm["extra_body"] = dict(extra_body)
    return llm


def _resolve_tools(
    tools: Optional[Sequence[Any]],
    extra_tools: Optional[Sequence[str]],
) -> tuple[Optional[list[str]], list[Any]]:
    if tools is None:
        return (default_tool_names(extra_tools=extra_tools) if extra_tools else None), []
    if extra_tools:
        raise ValueError("Use tools=[...] instead of extra_tools when passing an explicit tool set.")
    tool_names: list[str] = []
    custom_tools: list[Any] = []
    for item in tools:
        if isinstance(item, str):
            name = item.strip()
            if not name:
                raise ValueError("Tool names passed to tools must be non-empty strings.")
            tool_names.append(name)
        elif isinstance(item, type) and issubclass(item, ToolBase):
            try:
                tool_obj = item()
            except TypeError as exc:
                raise ValueError(
                    f"Tool class {item.__name__} could not be instantiated without arguments; "
                    "pass a configured instance instead."
                ) from exc
            builtin_tool = ALL_TOOL_MAP.get(tool_obj.name)
            if builtin_tool is not None and tool_obj.__class__ is builtin_tool.__class__:
                tool_names.append(tool_obj.name)
                continue
            custom_tool_map = build_custom_tool_map([tool_obj])
            custom_tool = next(iter(custom_tool_map.values()))
            tool_names.append(custom_tool.name)
            custom_tools.append(custom_tool)
        elif isinstance(item, ToolBase):
            builtin_tool = ALL_TOOL_MAP.get(item.name)
            if builtin_tool is not None and item.__class__ is builtin_tool.__class__:
                tool_names.append(item.name)
                continue
            custom_tool_map = build_custom_tool_map([item])
            custom_tool = next(iter(custom_tool_map.values()))
            tool_names.append(custom_tool.name)
            custom_tools.append(custom_tool)
        elif callable(item):
            custom_tool_map = build_custom_tool_map([item])
            custom_tool = next(iter(custom_tool_map.values()))
            tool_names.append(custom_tool.name)
            custom_tools.append(custom_tool)
        else:
            raise ValueError(
                "tools entries must be built-in tool classes, built-in tool names, "
                "decorated @researchharness.tool functions, or ToolBase instances."
            )
    return tool_names, custom_tools


def _read_role_prompt_blocks(role_prompt_files: Optional[str | Path | Sequence[str | Path]]) -> str:
    if role_prompt_files is None:
        return ""
    if isinstance(role_prompt_files, (str, Path)):
        paths = [str(role_prompt_files)]
    else:
        paths = [str(path) for path in role_prompt_files]
    return read_role_prompt_files(paths)


def _resolve_training_export(
    *,
    training_export_dir: Optional[str | Path],
    task_id: Optional[str],
    task_revision_id: Optional[str],
    rollout_id: Optional[str],
    rollout_index: Optional[int],
    run_id: Optional[str],
    harness_revision: Optional[str],
    scaffold_version: str,
    tool_protocol_version: str,
    tokenizer_revision: Optional[str],
    final_patch_path: Optional[str | Path],
    trace_dir: Optional[str],
) -> tuple[Optional[TrainingExportConfig], Optional[str], Optional[str]]:
    requested = training_export_dir is not None or any(
        value is not None
        for value in (
            task_id,
            task_revision_id,
            rollout_id,
            rollout_index,
            harness_revision,
            final_patch_path,
        )
    )
    if not requested:
        return None, trace_dir, run_id
    missing = [
        name
        for name, value in (
            ("training_export_dir", training_export_dir),
            ("task_id", task_id),
            ("task_revision_id", task_revision_id),
            ("rollout_id", rollout_id),
            ("rollout_index", rollout_index),
            ("harness_revision", harness_revision),
        )
        if value is None
    ]
    if missing:
        raise ValueError("turn-level training export requires: " + ", ".join(missing))
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    resolved_run_id = run_id if run_id is not None else uuid4().hex
    output_dir = Path(training_export_dir)  # type: ignore[arg-type]
    resolved_trace_dir = trace_dir or str(output_dir.parent / "trace")
    config = TrainingExportConfig(
        output_dir=output_dir,
        task_id=task_id,  # type: ignore[arg-type]
        task_revision_id=task_revision_id,  # type: ignore[arg-type]
        rollout_id=rollout_id,  # type: ignore[arg-type]
        rollout_index=rollout_index,  # type: ignore[arg-type]
        run_id=resolved_run_id,
        harness_revision=harness_revision,  # type: ignore[arg-type]
        scaffold_version=scaffold_version,
        tool_protocol_version=tool_protocol_version,
        tokenizer_revision=tokenizer_revision,
        final_patch_path=Path(final_patch_path) if final_patch_path is not None else None,
    )
    return config, resolved_trace_dir, resolved_run_id


def create_agent(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    max_input_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    recent_history_budget_tokens: Optional[int] = None,
    compact_summary_max_tokens: Optional[int] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    omit_generate_params: Optional[str | Sequence[str]] = None,
    compact_trigger_tokens: Optional[int | str] = None,
    extra_body: Optional[dict[str, Any]] = None,
    max_rounds: Optional[int] = None,
    max_runtime_seconds: Optional[int] = None,
    workspace_root: Optional[str] = None,
    trace_dir: Optional[str] = None,
    role_prompt: Optional[str] = None,
    role_prompt_files: Optional[str | Path | Sequence[str | Path]] = None,
    tools: Optional[Sequence[Any]] = None,
    extra_tools: Optional[Sequence[str]] = None,
    training_export_dir: Optional[str | Path] = None,
    task_id: Optional[str] = None,
    task_revision_id: Optional[str] = None,
    rollout_id: Optional[str] = None,
    rollout_index: Optional[int] = None,
    run_id: Optional[str] = None,
    harness_revision: Optional[str] = None,
    scaffold_version: str = "",
    tool_protocol_version: str = "openai-chat-completions-native-tools-v1",
    tokenizer_revision: Optional[str] = None,
    final_patch_path: Optional[str | Path] = None,
    require_env: bool = True,
) -> MultiTurnReactAgent:
    """Create a configured single-agent ResearchHarness runtime."""
    load_default_dotenvs()
    if require_env:
        require_required_env("ResearchHarness")
    role_blocks = []
    if role_prompt:
        role_blocks.append(str(role_prompt).strip())
    if role_prompt_files:
        role_blocks.append(_read_role_prompt_blocks(role_prompt_files))
    resolved_role_prompt = "\n\n".join(block for block in role_blocks if block.strip())
    function_list, custom_tools = _resolve_tools(tools, extra_tools)
    training_export, trace_dir, resolved_run_id = _resolve_training_export(
        training_export_dir=training_export_dir,
        task_id=task_id,
        task_revision_id=task_revision_id,
        rollout_id=rollout_id,
        rollout_index=rollout_index,
        run_id=run_id,
        harness_revision=harness_revision,
        scaffold_version=scaffold_version,
        tool_protocol_version=tool_protocol_version,
        tokenizer_revision=tokenizer_revision,
        final_patch_path=final_patch_path,
        trace_dir=trace_dir,
    )
    llm = _apply_llm_overrides(
        default_llm_config(model_name=model_name),
        api_key=api_key,
        api_base=api_base,
        timeout_seconds=timeout_seconds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        recent_history_budget_tokens=recent_history_budget_tokens,
        compact_summary_max_tokens=compact_summary_max_tokens,
        max_retries=max_retries,
        temperature=temperature,
        top_p=top_p,
        presence_penalty=presence_penalty,
        omit_generate_params=omit_generate_params,
        compact_trigger_tokens=compact_trigger_tokens,
        extra_body=extra_body,
    )
    return MultiTurnReactAgent(
        function_list=function_list,
        llm=llm,
        trace_dir=trace_dir,
        role_prompt=resolved_role_prompt or None,
        workspace_root=workspace_root,
        custom_tools=custom_tools,
        max_rounds=max_rounds,
        max_runtime_seconds=max_runtime_seconds,
        training_export=training_export,
        run_id=resolved_run_id,
    )


def run_agent(
    prompt: str,
    *,
    workspace_root: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    max_input_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    recent_history_budget_tokens: Optional[int] = None,
    compact_summary_max_tokens: Optional[int] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    omit_generate_params: Optional[str | Sequence[str]] = None,
    compact_trigger_tokens: Optional[int | str] = None,
    extra_body: Optional[dict[str, Any]] = None,
    max_rounds: Optional[int] = None,
    max_runtime_seconds: Optional[int] = None,
    trace_dir: Optional[str] = None,
    role_prompt: Optional[str] = None,
    role_prompt_files: Optional[str | Path | Sequence[str | Path]] = None,
    images: Optional[str | Path | Sequence[str | Path]] = None,
    tools: Optional[Sequence[Any]] = None,
    extra_tools: Optional[Sequence[str]] = None,
    training_export_dir: Optional[str | Path] = None,
    task_id: Optional[str] = None,
    task_revision_id: Optional[str] = None,
    rollout_id: Optional[str] = None,
    rollout_index: Optional[int] = None,
    run_id: Optional[str] = None,
    harness_revision: Optional[str] = None,
    scaffold_version: str = "",
    tool_protocol_version: str = "openai-chat-completions-native-tools-v1",
    tokenizer_revision: Optional[str] = None,
    final_patch_path: Optional[str | Path] = None,
    return_session: bool = False,
    require_env: bool = True,
) -> str | dict[str, Any]:
    """Run once; optionally return trace/export metadata with the final text."""
    if not isinstance(return_session, bool):
        raise ValueError("return_session must be a boolean")
    agent = create_agent(
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        timeout_seconds=timeout_seconds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        recent_history_budget_tokens=recent_history_budget_tokens,
        compact_summary_max_tokens=compact_summary_max_tokens,
        max_retries=max_retries,
        temperature=temperature,
        top_p=top_p,
        presence_penalty=presence_penalty,
        omit_generate_params=omit_generate_params,
        compact_trigger_tokens=compact_trigger_tokens,
        extra_body=extra_body,
        max_rounds=max_rounds,
        max_runtime_seconds=max_runtime_seconds,
        workspace_root=workspace_root,
        trace_dir=trace_dir,
        role_prompt=role_prompt,
        role_prompt_files=role_prompt_files,
        tools=tools,
        extra_tools=extra_tools,
        training_export_dir=training_export_dir,
        task_id=task_id,
        task_revision_id=task_revision_id,
        rollout_id=rollout_id,
        rollout_index=rollout_index,
        run_id=run_id,
        harness_revision=harness_revision,
        scaffold_version=scaffold_version,
        tool_protocol_version=tool_protocol_version,
        tokenizer_revision=tokenizer_revision,
        final_patch_path=final_patch_path,
        require_env=require_env,
    )
    if return_session:
        return agent.run_session(prompt, images=images)
    return agent.run(prompt, images=images)


__all__ = ["create_agent", "run_agent", "tool"]
