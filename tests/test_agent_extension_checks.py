#!/usr/bin/env python3

import json
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_support import TEST_RUNS_DIR, bootstrap, load_trace_records, preview, single_trace_path


TMP_DIR = TEST_RUNS_DIR / "agent_extension_checks"


@dataclass
class AgentExtensionResult:
    status: str
    detail: str
    output_preview: str


def main() -> int:
    bootstrap()

    from agent_base import agent_role
    from agent_base.prompt import SYSTEM_PROMPT
    from agent_base.react_agent import (
        AVAILABLE_TOOL_MAP,
        MultiTurnReactAgent,
        _parse_cli_args,
        assistant_text_content,
        prepare_messages_for_llm,
        resolve_agent_class_for_role_prompt_files,
    )
    from agent_base.utils import (
        append_saved_image_paths_to_prompt,
        image_input_content_parts,
        stage_image_file_for_input,
    )
    from benchmarks.ResearchClawBench.adapter import ResearchClawBenchAgent

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    trace_dir = TMP_DIR / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for existing_trace in trace_dir.glob("*.jsonl"):
        existing_trace.unlink()

    @agent_role(
        name="judge",
        role_prompt="You are the Judge agent. Evaluate the provided artifact carefully and return JSON only.",
        function_list=[],
    )
    class DummyJudgeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 10000,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
                trace_dir=str(trace_dir),
            )
            self.seen_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.seen_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": '{"overall_score": 8.5, "verdict": "good"}',
                "tool_calls": [],
            }

    agent = DummyJudgeAgent()
    session = agent._run_session("Review this artifact.", workspace_root=str(TMP_DIR))
    trace_path = single_trace_path(trace_dir)
    if trace_path is None:
        raise RuntimeError(f"Expected exactly one trace file in {trace_dir}")
    rows = load_trace_records(trace_path)

    system_message = agent.seen_messages[0]["content"] if agent.seen_messages else ""
    rcb_prompt_path = ROOT / "benchmarks" / "ResearchClawBench" / "role_prompt.md"
    qa_prompt_text = (ROOT / "benchmarks" / "QA" / "role_prompt.md").read_text(encoding="utf-8")
    resolved_default_cls = resolve_agent_class_for_role_prompt_files([])
    resolved_rcb_cls = resolve_agent_class_for_role_prompt_files([str(rcb_prompt_path)])
    rcb_agent = ResearchClawBenchAgent(
        llm={
            "model": "fake-model",
            "generate_cfg": {
                "max_input_tokens": 10000,
                "max_retries": 1,
                "temperature": 0.0,
                "top_p": 1.0,
                "presence_penalty": 0.0,
            },
        },
        trace_dir=str(trace_dir),
    )
    rcb_forbidden_error = ""
    try:
        ResearchClawBenchAgent(
            function_list=["AskUser"],
            llm={
                "model": "fake-model",
                "generate_cfg": {
                    "max_input_tokens": 10000,
                    "max_retries": 1,
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "presence_penalty": 0.0,
                },
            },
            trace_dir=str(trace_dir),
        )
    except ValueError as exc:
        rcb_forbidden_error = str(exc)

    class ContinueAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=[],
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 10000,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
                trace_dir=str(trace_dir),
            )
            self.call_count = 0
            self.second_request_messages = []
            self.third_request_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.call_count += 1
            if self.call_count == 2:
                self.second_request_messages = msgs
            elif self.call_count == 3:
                self.third_request_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": f"answer {self.call_count}",
                "tool_calls": [],
            }

    continue_agent = ContinueAgent()
    continue_workspace = TMP_DIR / "continue_workspace"
    first_turn = continue_agent._run_session("Remember this: alpha.", workspace_root=str(TMP_DIR / "continue_workspace"))
    second_turn = continue_agent._run_session(
        "What did I ask you to remember?",
        workspace_root=str(continue_workspace),
        prior_messages=first_turn["messages"],
    )
    second_request_text = "\n".join(assistant_text_content(message.get("content")) for message in continue_agent.second_request_messages)
    continue_image_source = TMP_DIR / "continue_image.png"
    continue_image_source.write_bytes(b"fake continue image bytes")
    continue_saved_path, continue_data_url = stage_image_file_for_input(
        continue_image_source,
        workspace_root=continue_workspace,
        image_index=0,
    )
    continue_image_parts = image_input_content_parts(continue_data_url, continue_saved_path)
    continue_image_prompt = append_saved_image_paths_to_prompt("Inspect this follow-up image.", [continue_saved_path])
    third_turn = continue_agent._run_session(
        continue_image_prompt,
        workspace_root=str(continue_workspace),
        prior_messages=second_turn["messages"],
        initial_content_parts=continue_image_parts,
    )
    third_request_text = "\n".join(assistant_text_content(message.get("content")) for message in continue_agent.third_request_messages)
    third_request_has_image = any(
        isinstance(message.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message.get("content", []))
        for message in continue_agent.third_request_messages
    )
    third_trace_rows = load_trace_records(Path(third_turn["trace_path"]))
    continuation_runtime_text = "\n".join(
        str(row.get("text", "")) for row in third_trace_rows if row.get("role") == "runtime"
    )

    class InterruptAgent(MultiTurnReactAgent):
        def __init__(self, interrupt_event: threading.Event):
            super().__init__(
                function_list=[],
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 10000,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
                trace_dir=str(trace_dir),
            )
            self.call_count = 0
            self.interrupt_event = interrupt_event

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.call_count += 1
            if self.call_count == 1:
                self.interrupt_event.set()
                return {
                    "status": "ok",
                    "finish_reason": "stop",
                    "content": "late answer should be discarded",
                    "tool_calls": [],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "continued after interrupt",
                "tool_calls": [],
            }

    interrupt_event = threading.Event()
    interrupt_agent = InterruptAgent(interrupt_event)
    interrupt_session = interrupt_agent._run_session(
        "Start a long task.",
        workspace_root=str(TMP_DIR / "interrupt_workspace"),
        interrupt_event=interrupt_event,
    )
    interrupt_followup = interrupt_agent._run_session(
        "Continue after the interruption.",
        workspace_root=str(TMP_DIR / "interrupt_workspace"),
        prior_messages=interrupt_session["messages"],
    )
    interrupt_message_text = "\n".join(assistant_text_content(message.get("content")) for message in interrupt_session["messages"])

    cli_image_source = TMP_DIR / "source_image.png"
    cli_image_source.write_bytes(b"fake png bytes")
    cli_second_image_source = TMP_DIR / "source_image_2.jpg"
    cli_second_image_source.write_bytes(b"fake jpg bytes")
    cli_workspace = TMP_DIR / "cli_workspace"
    cli_workspace.mkdir(exist_ok=True)
    cli_saved_path, cli_data_url = stage_image_file_for_input(
        cli_image_source,
        workspace_root=cli_workspace,
        image_index=0,
    )
    cli_second_saved_path, cli_second_data_url = stage_image_file_for_input(
        cli_second_image_source,
        workspace_root=cli_workspace,
        image_index=1,
    )
    cli_content_parts = [
        *image_input_content_parts(cli_data_url, cli_saved_path),
        *image_input_content_parts(cli_second_data_url, cli_second_saved_path),
    ]
    cli_prompt = append_saved_image_paths_to_prompt("Inspect the images.", [cli_saved_path, cli_second_saved_path])
    _, _, _, _, _, parsed_image_args, parsed_chat_arg, parsed_tool_names, parsed_extra_tools = _parse_cli_args(
        ["Inspect the images.", "--images", str(cli_image_source), str(cli_second_image_source)]
    )
    *_, parsed_cli_tool_names, parsed_cli_extra_tools = _parse_cli_args(
        ["Inspect the files.", "--tool", "Read", "--tool", "Bash"]
    )
    _, _, _, _, _, _, parsed_chat_enabled, _, _ = _parse_cli_args(["Inspect the images.", "--chat"])
    _, _, _, _, _, _, parsed_chat_disabled, _, _ = _parse_cli_args(["Inspect the images.", "--no-chat"])
    aged_messages, image_aging = prepare_messages_for_llm(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": [{"type": "text", "text": cli_prompt}, *cli_content_parts]},
            {"role": "assistant", "content": "I will continue."},
            {"role": "user", "content": "continue"},
        ]
    )
    aged_user_content = aged_messages[1]["content"]
    preview_text = preview(
        json.dumps(
            {
                "termination": session.get("termination"),
                "result_text": session.get("result_text"),
                "tool_names": agent.tool_names,
                "trace_roles": [row.get("role") for row in rows],
                "default_agent_class": resolved_default_cls.__name__,
                "rcb_agent_class": resolved_rcb_cls.__name__,
                "ask_user_available": "AskUser" in AVAILABLE_TOOL_MAP,
                "rcb_has_ask_user": "AskUser" in rcb_agent.tool_names,
                "rcb_forbidden_error": rcb_forbidden_error,
                "cli_saved_path": cli_saved_path,
                "cli_second_saved_path": cli_second_saved_path,
                "parsed_image_args": parsed_image_args,
                "parsed_chat_arg": parsed_chat_arg,
                "parsed_tool_names": parsed_tool_names,
                "parsed_extra_tools": parsed_extra_tools,
                "parsed_cli_tool_names": parsed_cli_tool_names,
                "parsed_cli_extra_tools": parsed_cli_extra_tools,
                "parsed_chat_enabled": parsed_chat_enabled,
                "parsed_chat_disabled": parsed_chat_disabled,
                "image_aging": image_aging,
                "system_prompt_tail": system_message[-300:],
                "qa_prompt_mentions_synchronous": "synchronous" in qa_prompt_text.lower(),
                "continued_result": second_turn.get("result_text"),
                "continued_image_result": third_turn.get("result_text"),
                "continued_image_saved_path": continue_saved_path,
                "continuation_runtime_text": continuation_runtime_text,
                "interrupt_termination": interrupt_session.get("termination"),
                "interrupt_followup": interrupt_followup.get("result_text"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    ok = (
        agent.tool_names == []
        and agent._native_tools == []
        and SYSTEM_PROMPT.splitlines()[0] in system_message
        and "Non-interactive or benchmark-style runs:" in system_message
        and "Interactive runs:" in system_message
        and "Final answers must be complete and self-contained enough" in system_message
        and "You may reference local files" in system_message
        and "Only use `AskUser` if it is available in the current tool list" in system_message
        and "You are the Judge agent." in system_message
        and session.get("result_text") == '{"overall_score": 8.5, "verdict": "good"}'
        and not any(row.get("role") == "tool" for row in rows)
        and any(row.get("termination") == "result" for row in rows)
        and resolved_default_cls is MultiTurnReactAgent
        and resolved_rcb_cls is ResearchClawBenchAgent
        and "AskUser" in AVAILABLE_TOOL_MAP
        and "AskUser" not in rcb_agent.tool_names
        and "AskUser" in rcb_forbidden_error
        and second_turn.get("result_text") == "answer 2"
        and "answer 1" in second_request_text
        and "What did I ask you to remember?" in second_request_text
        and third_turn.get("result_text") == "answer 3"
        and third_request_has_image
        and continue_saved_path in third_request_text
        and "Continuing existing conversation with prior messages." in continuation_runtime_text
        and "prior/current non-system messages" not in continuation_runtime_text
        and interrupt_session.get("termination") == "interrupted"
        and "Interrupted by user" in interrupt_session.get("result_text", "")
        and "Start a long task." in interrupt_message_text
        and "late answer should be discarded" not in interrupt_message_text
        and interrupt_followup.get("result_text") == "continued after interrupt"
        and cli_saved_path.startswith("inputs/images/")
        and cli_second_saved_path.startswith("inputs/images/")
        and parsed_extra_tools == []
        and parsed_tool_names == []
        and parsed_cli_tool_names == ["Read", "Bash"]
        and parsed_cli_extra_tools == []
        and (cli_workspace / cli_saved_path).exists()
        and (cli_workspace / cli_second_saved_path).exists()
        and parsed_image_args == [str(cli_image_source), str(cli_second_image_source)]
        and parsed_chat_arg is None
        and parsed_chat_enabled is True
        and parsed_chat_disabled is False
        and isinstance(aged_user_content, list)
        and any("Saved local path: inputs/images/" in str(part.get("text", "")) for part in aged_user_content if isinstance(part, dict))
        and image_aging["omitted_image_count"] == 2
        and "synchronous" not in qa_prompt_text.lower()
        and "complete, independent plain-text answer" in qa_prompt_text
        and "answer.md" in qa_prompt_text
        and "do not rely on local workspace files as the answer" in qa_prompt_text
    )

    result = AgentExtensionResult(
        status="PASS" if ok else "FAIL",
        detail="Role-prompt composition and no-tool agent defaults are working."
        if ok
        else "Agent extension checks failed.",
        output_preview=preview_text,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
