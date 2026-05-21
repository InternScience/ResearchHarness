"""Small public Python API for embedding ResearchHarness."""

from __future__ import annotations

from typing import Optional, Sequence

from agent_base.react_agent import MultiTurnReactAgent, default_llm_config, default_tool_names
from agent_base.utils import load_default_dotenvs, read_role_prompt_files, require_required_env


def create_agent(
    *,
    model_name: Optional[str] = None,
    trace_dir: Optional[str] = None,
    role_prompt: Optional[str] = None,
    role_prompt_files: Optional[Sequence[str]] = None,
    extra_tools: Optional[Sequence[str]] = None,
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
        role_blocks.append(read_role_prompt_files(role_prompt_files))
    resolved_role_prompt = "\n\n".join(block for block in role_blocks if block.strip())
    return MultiTurnReactAgent(
        function_list=default_tool_names(extra_tools=extra_tools) if extra_tools else None,
        llm=default_llm_config(model_name=model_name),
        trace_dir=trace_dir,
        role_prompt=resolved_role_prompt or None,
    )


def run_agent(
    prompt: str,
    *,
    workspace_root: Optional[str] = None,
    model_name: Optional[str] = None,
    trace_dir: Optional[str] = None,
    role_prompt: Optional[str] = None,
    role_prompt_files: Optional[Sequence[str]] = None,
    extra_tools: Optional[Sequence[str]] = None,
    require_env: bool = True,
) -> str:
    """Run ResearchHarness once and return the final assistant text."""
    agent = create_agent(
        model_name=model_name,
        trace_dir=trace_dir,
        role_prompt=role_prompt,
        role_prompt_files=role_prompt_files,
        extra_tools=extra_tools,
        require_env=require_env,
    )
    return agent.run(prompt, workspace_root=workspace_root)

