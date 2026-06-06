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
    from agent_base.react_agent import (
        AVAILABLE_TOOL_MAP,
        OPTIONAL_TOOL_MAP,
        MultiTurnReactAgent,
        _parse_cli_args,
        default_tool_names,
        resolve_extra_tool_names,
    )
    from agent_base.tools.tool_extra import StrReplaceEditor

    case_dir = TEST_RUNS_DIR / "extra_tools"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)
    sample = case_dir / "sample.txt"
    sample.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")

    tool = StrReplaceEditor()
    view_result = tool.call(
        {"command": "view", "path": str(sample), "view_range": [1, 2]},
        workspace_root=case_dir,
    )
    duplicate_result = tool.call(
        {"command": "str_replace", "path": str(sample), "old_str": "alpha", "new_str": "ALPHA"},
        workspace_root=case_dir,
    )
    replace_result = tool.call(
        {"command": "str_replace", "path": str(sample), "old_str": "beta", "new_str": "BETA"},
        workspace_root=case_dir,
    )
    replaced_text = sample.read_text(encoding="utf-8")
    undo_replace_result = tool.call({"command": "undo_edit", "path": str(sample)}, workspace_root=case_dir)
    undo_replace_text = sample.read_text(encoding="utf-8")
    insert_result = tool.call(
        {"command": "insert", "path": str(sample), "insert_line": 1, "new_str": "inserted"},
        workspace_root=case_dir,
    )
    inserted_text = sample.read_text(encoding="utf-8")
    created = case_dir / "created.txt"
    create_result = tool.call(
        {"command": "create", "path": str(created), "file_text": "created\n"},
        workspace_root=case_dir,
    )
    undo_create_result = tool.call({"command": "undo_edit", "path": str(created)}, workspace_root=case_dir)
    boundary_result = tool.call(
        {"command": "view", "path": str(ROOT / "outside_extra_tool.txt")},
        workspace_root=case_dir,
    )

    extra_names = resolve_extra_tool_names(["str_replace_editor", "str_replace_editor"])
    tool_names = default_tool_names(extra_tools=extra_names)
    agent = MultiTurnReactAgent(
        function_list=tool_names,
        llm={"model": "fake-model", "generate_cfg": {}},
    )
    *_, parsed_extra_tools, parsed_llm_extra_body = _parse_cli_args(["hello", "--extra-tool", "str_replace_editor"])

    details = {
        "default_has_extra": "str_replace_editor" in AVAILABLE_TOOL_MAP,
        "optional_has_extra": "str_replace_editor" in OPTIONAL_TOOL_MAP,
        "extra_names": extra_names,
        "agent_has_extra": "str_replace_editor" in agent.tool_names,
        "parsed_extra_tools": parsed_extra_tools,
        "parsed_llm_extra_body": parsed_llm_extra_body,
        "view_result": view_result,
        "duplicate_result": duplicate_result,
        "replace_result": replace_result,
        "replaced_text": replaced_text,
        "undo_replace_result": undo_replace_result,
        "undo_replace_text": undo_replace_text,
        "insert_result": insert_result,
        "inserted_text": inserted_text,
        "create_result": create_result,
        "undo_create_result": undo_create_result,
        "created_exists_after_undo": created.exists(),
        "boundary_result": boundary_result,
    }

    ok = (
        "str_replace_editor" not in AVAILABLE_TOOL_MAP
        and "str_replace_editor" in OPTIONAL_TOOL_MAP
        and extra_names == ["str_replace_editor"]
        and "str_replace_editor" in agent.tool_names
        and parsed_extra_tools == ["str_replace_editor"]
        and parsed_llm_extra_body == {}
        and "     1\talpha" in view_result
        and "     2\tbeta" in view_result
        and "must be unique" in duplicate_result
        and "Replaced text" in replace_result
        and replaced_text == "alpha\nBETA\nalpha\n"
        and "Reverted last edit" in undo_replace_result
        and undo_replace_text == "alpha\nbeta\nalpha\n"
        and "Inserted text" in insert_result
        and inserted_text == "alpha\ninserted\nbeta\nalpha\n"
        and "Created file" in create_result
        and "Removed file created" in undo_create_result
        and not created.exists()
        and "limited to the workspace root" in boundary_result
    )
    print(json.dumps({"ok": ok, **details}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
