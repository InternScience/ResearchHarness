#!/usr/bin/env python3

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_support import TEST_RUNS_DIR


def main() -> int:
    import tiktoken

    from researchharness import Bash, Read, available_tool_schemas, create_agent, tool

    class FakeEncoding:
        def encode(self, text):
            return list(str(text))

    tiktoken.get_encoding = lambda _name: FakeEncoding()

    case_dir = TEST_RUNS_DIR / "python_api_tools"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    role_file = case_dir / "role.md"
    role_file.write_text("Role file prompt.", encoding="utf-8")

    @tool
    def add_numbers(a: int, b: int = 1) -> int:
        """Add two integers."""

        return a + b

    @tool(name="mark_workspace", description="Write a marker into the selected workspace.")
    def mark_workspace(filename: str, *, workspace_root) -> str:
        path = Path(workspace_root) / filename
        path.write_text("marked", encoding="utf-8")
        return path.relative_to(workspace_root).as_posix()

    explicit_agent = create_agent(
        model_name="fake-model",
        api_key="fake-key",
        api_base="http://fake.local/v1",
        timeout_seconds=12.5,
        max_input_tokens=32768,
        max_output_tokens=4096,
        max_retries=2,
        temperature=0.2,
        top_p=0.7,
        presence_penalty=0.3,
        compact_trigger_tokens="24k",
        max_llm_calls=11,
        max_rounds=12,
        max_runtime_seconds=123,
        workspace_root=str(case_dir / "agent_workspace"),
        role_prompt="Inline role prompt.",
        role_prompt_files=str(role_file),
        tools=[Read, add_numbers, mark_workspace],
        require_env=False,
    )
    add_result = explicit_agent.custom_call_tool("add_numbers", {"a": 2, "b": 3})
    marker_result = explicit_agent.custom_call_tool(
        "mark_workspace",
        {"filename": "marker.txt"},
        workspace_root=explicit_agent.workspace_root,
    )
    standalone_schema_names = [
        schema["function"]["name"] for schema in available_tool_schemas([Read, Bash, add_numbers])
    ]
    explicit_schema_names = [schema["function"]["name"] for schema in explicit_agent._native_tools]

    default_agent = create_agent(model_name="fake-model", require_env=False)
    no_tool_agent = create_agent(model_name="fake-model", tools=[], require_env=False)

    @tool(name="Read", description="Conflicting tool name.")
    def conflicting_tool(path: str) -> str:
        """Conflict with the built-in Read tool."""

        return path

    def undecorated_tool(value: str) -> str:
        return value

    @tool(description="Missing parameter annotation.")
    def missing_annotation(value):
        return value

    errors: dict[str, str] = {}
    for name, factory in {
        "conflict": lambda: create_agent(model_name="fake-model", tools=[conflicting_tool], require_env=False),
        "undecorated": lambda: create_agent(model_name="fake-model", tools=[undecorated_tool], require_env=False),
        "missing_annotation": lambda: create_agent(model_name="fake-model", tools=[missing_annotation], require_env=False),
        "duplicate_rejected": lambda: create_agent(model_name="fake-model", tools=[Read, Read], require_env=False),
        "mixed_tools_extra": lambda: create_agent(
            model_name="fake-model",
            tools=[Read],
            extra_tools=["str_replace_editor"],
            require_env=False,
        ),
    }.items():
        try:
            factory()
        except Exception as exc:
            errors[name] = str(exc)

    image_source = case_dir / "source.png"
    image_source.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d763f8ffff3f0005fe02fea73581e30000000049454e44ae426082"
        )
    )

    class EchoAgent(type(explicit_agent)):
        def _run_session(self, prompt, workspace_root=None, initial_content_parts=None, **kwargs):
            return {
                "result_text": json.dumps(
                    {
                        "prompt": prompt,
                        "workspace_root": str(workspace_root),
                        "image_parts": len(initial_content_parts or []),
                    },
                    ensure_ascii=False,
                )
            }

    echo_agent = EchoAgent(
        function_list=[],
        llm={"model": "fake-model", "generate_cfg": {}},
        workspace_root=str(case_dir / "run_workspace"),
    )
    image_run_result = json.loads(echo_agent.run("inspect image", images=str(image_source)))

    class NeedsConfig(Read):
        def __init__(self, required_config):
            super().__init__()

    try:
        create_agent(model_name="fake-model", tools=[NeedsConfig], require_env=False)
    except Exception as exc:
        errors["configured_tool_class"] = str(exc)
    try:
        available_tool_schemas([undecorated_tool])
    except Exception as exc:
        errors["schema_helper_validates_custom_tools"] = str(exc)

    details = {
        "explicit_tools": explicit_agent.tool_names,
        "llm": {
            "api_key": explicit_agent._llm_api_key,
            "api_base": explicit_agent._llm_api_base,
            "timeout_seconds": explicit_agent._llm_timeout_seconds,
            "generate_cfg": explicit_agent.llm_generate_cfg,
            "max_llm_calls": explicit_agent.max_llm_calls,
            "max_rounds": explicit_agent.max_rounds,
            "max_runtime_seconds": explicit_agent.max_runtime_seconds,
        },
        "role_prompt": explicit_agent.role_prompt,
        "workspace_root": str(explicit_agent.workspace_root),
        "add_result": add_result,
        "marker_result": marker_result,
        "standalone_schema_names": standalone_schema_names,
        "explicit_schema_names": explicit_schema_names,
        "default_has_read": "Read" in default_agent.tool_names,
        "default_has_ask_user": "AskUser" in default_agent.tool_names,
        "no_tool_names": no_tool_agent.tool_names,
        "errors": errors,
        "image_run_result": image_run_result,
        "saved_images": sorted(
            path.relative_to(case_dir / "run_workspace").as_posix()
            for path in (case_dir / "run_workspace").glob("inputs/images/*")
        ),
    }

    ok = (
        explicit_agent.tool_names == ["Read", "add_numbers", "mark_workspace"]
        and details["llm"]["api_key"] == "fake-key"
        and details["llm"]["api_base"] == "http://fake.local/v1"
        and details["llm"]["timeout_seconds"] == 12.5
        and details["llm"]["generate_cfg"]["max_input_tokens"] == 32768
        and details["llm"]["generate_cfg"]["max_output_tokens"] == 4096
        and details["llm"]["generate_cfg"]["max_retries"] == 2
        and details["llm"]["generate_cfg"]["temperature"] == 0.2
        and details["llm"]["generate_cfg"]["top_p"] == 0.7
        and details["llm"]["generate_cfg"]["presence_penalty"] == 0.3
        and details["llm"]["generate_cfg"]["compact_trigger_tokens"] == "24k"
        and details["llm"]["max_llm_calls"] == 11
        and details["llm"]["max_rounds"] == 12
        and details["llm"]["max_runtime_seconds"] == 123
        and Bash().name not in explicit_agent.tool_names
        and explicit_agent.role_prompt == "Inline role prompt.\n\nRole file prompt."
        and explicit_agent.workspace_root == (case_dir / "agent_workspace").resolve()
        and add_result == 5
        and marker_result == "marker.txt"
        and (case_dir / "agent_workspace" / "marker.txt").read_text(encoding="utf-8") == "marked"
        and standalone_schema_names == ["Read", "Bash", "add_numbers"]
        and explicit_schema_names == ["Read", "add_numbers", "mark_workspace"]
        and details["default_has_read"]
        and details["default_has_ask_user"]
        and no_tool_agent.tool_names == []
        and "conflict" in errors
        and "undecorated" in errors
        and "missing_annotation" in errors
        and "duplicate_rejected" in errors
        and "mixed_tools_extra" in errors
        and "configured_tool_class" in errors
        and "schema_helper_validates_custom_tools" in errors
        and image_run_result["workspace_root"] == str((case_dir / "run_workspace").resolve())
        and image_run_result["image_parts"] == 2
    )
    ok = ok and details["saved_images"] and details["saved_images"][0].startswith("inputs/images/image_000_")
    ok = ok and "The user attached image input" in image_run_result["prompt"]

    print(json.dumps({"ok": ok, **details}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
