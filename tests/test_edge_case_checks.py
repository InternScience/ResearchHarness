#!/usr/bin/env python3

import json
import os
import re
import shutil
import sys
import time
import types
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_support import TEST_RUNS_DIR, bootstrap, load_trace_records, preview, single_trace_path


TMP_DIR = TEST_RUNS_DIR / "edge_case_checks"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class EdgeCaseResult:
    status: str
    detail: str
    output_preview: str


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).replace("\r", "")


def check_workspace_root_is_created_when_missing() -> tuple[bool, str]:
    from agent_base.tools.tooling import normalize_workspace_root

    case_dir = TMP_DIR / "workspace_root_auto_create" / "fresh_workspace"
    shutil.rmtree(case_dir.parent, ignore_errors=True)
    resolved = normalize_workspace_root(case_dir)
    ok = resolved == case_dir.resolve() and resolved.is_dir()
    return ok, json.dumps({"resolved": str(resolved), "exists": resolved.exists()}, indent=2)


def check_required_env_is_enforced() -> tuple[bool, str]:
    from agent_base.utils import REQUIRED_ENV_VARS, MissingRequiredEnvError, require_required_env

    previous = {key: os.environ.get(key) for key in REQUIRED_ENV_VARS}
    try:
        for key in REQUIRED_ENV_VARS:
            os.environ.pop(key, None)
        try:
            require_required_env("test context")
        except MissingRequiredEnvError as exc:
            text = str(exc)
            missing = [key for key in REQUIRED_ENV_VARS if key in text]
            ok = len(missing) == len(REQUIRED_ENV_VARS) and "test context missing required environment variables" in text
            return ok, json.dumps({"error": text, "missing_reported": missing}, indent=2)
        return False, json.dumps({"error": "missing env did not raise"}, indent=2)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_llm_hard_timeout_interrupts_blocking_call() -> tuple[bool, str]:
    from agent_base.react_agent import LLMHardTimeoutError, llm_hard_timeout

    start = time.time()
    try:
        with llm_hard_timeout(0.2):
            time.sleep(5)
    except LLMHardTimeoutError as exc:
        elapsed = time.time() - start
        ok = elapsed < 1.5
        return ok, json.dumps({"elapsed_seconds": elapsed, "error": str(exc)}, indent=2)
    elapsed = time.time() - start
    return False, json.dumps({"elapsed_seconds": elapsed, "error": "timeout did not fire"}, indent=2)


def check_readpdf_relative_image_path() -> tuple[bool, str]:
    from agent_base.tools.tool_file import ReadPDF

    case_dir = TMP_DIR / "readpdf_relative_image"
    case_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = case_dir / "dummy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%dummy\n")
    image_dir = case_dir / "fake_extracted"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / "figure1.png"
    Image.new("RGB", (8, 8), color="white").save(image_path)

    fake_structai = types.ModuleType("structai")
    fake_structai.read_pdf = lambda _: {"text": "", "img_paths": ["fake_extracted/figure1.png"]}
    previous_structai = sys.modules.get("structai")
    sys.modules["structai"] = fake_structai
    try:
        result = ReadPDF().call({"path": str(pdf_path)}, workspace_root=case_dir)
    finally:
        if previous_structai is None:
            sys.modules.pop("structai", None)
        else:
            sys.modules["structai"] = previous_structai

    ok = (
        isinstance(result, str)
        and not result.startswith("[ReadPDF] Error")
        and "source_type: pdf" in result
        and str(image_path.resolve()) in result
    )
    return ok, str(result)


def check_terminal_interrupt_preserves_remainder() -> tuple[bool, str]:
    from agent_base.tools.tool_runtime import TerminalInterrupt, TerminalKill, TerminalRead, TerminalStart, TerminalWrite

    case_dir = TMP_DIR / "terminal_interrupt_remainder"
    case_dir.mkdir(parents=True, exist_ok=True)

    outputs: list[str] = []
    start_result = TerminalStart().call({"cwd": str(case_dir)}, workspace_root=case_dir)
    outputs.append(start_result)
    session_match = re.search(r"session_id: (term_\d+)", start_result)
    if not session_match:
        return False, "\n\n".join(outputs)
    session_id = session_match.group(1)

    try:
        write_result = TerminalWrite().call(
            {
                "session_id": session_id,
                "input": "sleep 0.2; printf 'ABCDEFGHIJ'; sleep 10",
                "yield_time_ms": 0,
            },
            workspace_root=case_dir,
        )
        outputs.append(write_result)
        time.sleep(0.4)
        interrupt_result = TerminalInterrupt().call(
            {
                "session_id": session_id,
                "max_output_chars": 4,
            },
            workspace_root=case_dir,
        )
        outputs.append(interrupt_result)
        read_result = TerminalRead().call(
            {
                "session_id": session_id,
                "yield_time_ms": 200,
                "max_output_chars": 4000,
            },
            workspace_root=case_dir,
        )
        outputs.append(read_result)
    finally:
        outputs.append(TerminalKill().call({"session_id": session_id}, workspace_root=case_dir))

    cleaned_interrupt = strip_ansi(interrupt_result)
    cleaned_read = strip_ansi(read_result)
    ok = "Sent Ctrl-C" in interrupt_result and "EFGHIJ" in cleaned_read and "ABCDEFGHIJ" in (cleaned_interrupt + cleaned_read)
    return ok, "\n\n".join(outputs)


def check_agent_runtime_limit_on_tool_execution() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "agent_runtime_limit"
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Bash"],
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
            )
            self._turn = 0

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fake_bash",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "sleep 2", "timeout": 30}),
                            },
                        }
                    ],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    previous_runtime = os.environ.get("MAX_AGENT_RUNTIME_SECONDS")
    os.environ["MAX_AGENT_RUNTIME_SECONDS"] = "1"
    try:
        agent = FakeAgent()
        started_at = time.time()
        session = agent._run_session("trigger the slow bash tool", workspace_root=str(case_dir))
        elapsed = time.time() - started_at
    finally:
        if previous_runtime is None:
            os.environ.pop("MAX_AGENT_RUNTIME_SECONDS", None)
        else:
            os.environ["MAX_AGENT_RUNTIME_SECONDS"] = previous_runtime

    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "elapsed_seconds": round(elapsed, 3),
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        isinstance(session.get("termination"), str)
        and session["termination"].startswith("agent runtime limit reached")
        and elapsed < 1.8
    )
    return ok, detail


def check_parallel_readimage_tool_message_order() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "parallel_readimage_tool_messages"
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["ReadImage"],
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
            )
            self._turn = 0
            self.seen_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.seen_messages = msgs
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_img_1",
                            "type": "function",
                            "function": {
                                "name": "ReadImage",
                                "arguments": json.dumps({"path": "img1.jpg"}),
                            },
                        },
                        {
                            "id": "call_img_2",
                            "type": "function",
                            "function": {
                                "name": "ReadImage",
                                "arguments": json.dumps({"path": "img2.jpg"}),
                            },
                        },
                        {
                            "id": "call_img_3",
                            "type": "function",
                            "function": {
                                "name": "ReadImage",
                                "arguments": json.dumps({"path": "img3.jpg"}),
                            },
                        },
                    ],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            path = tool_args["path"] if isinstance(tool_args, dict) else "unknown"
            return {
                "kind": "image_tool_result",
                "text": f"path: {path}\nllm_image_attached: true",
                "path": str(case_dir / path),
                "image_url": "data:image/jpeg;base64,ZmFrZQ==",
            }

    agent = FakeAgent()
    session = agent._run_session("inspect three images", workspace_root=str(case_dir))
    roles_after_assistant = [msg.get("role") for msg in agent.seen_messages[2:]]
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "roles_after_assistant": roles_after_assistant,
            "messages_seen": agent.seen_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and roles_after_assistant[:7] == ["assistant", "tool", "tool", "tool", "user", "user", "user"]
        and len(roles_after_assistant) == 7
    )
    return ok, detail


def check_deepseek_readimage_falls_back_to_text_only_context() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "deepseek_readimage_text_fallback"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["ReadImage"],
                llm={
                    "model": "deepseek-v4-pro",
                    "generate_cfg": {
                        "max_input_tokens": 10000,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
            )
            self._turn = 0
            self.second_round_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_img_1",
                            "type": "function",
                            "function": {
                                "name": "ReadImage",
                                "arguments": json.dumps({"path": "img1.jpg"}),
                            },
                        }
                    ],
                }
            self.second_round_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            path = tool_args["path"] if isinstance(tool_args, dict) else "unknown"
            return {
                "kind": "image_tool_result",
                "text": f"path: {path}\nllm_image_attached: true",
                "path": str(case_dir / path),
                "image_url": "data:image/jpeg;base64,ZmFrZQ==",
            }

    agent = FakeAgent()
    session = agent._run_session("inspect one image with deepseek", workspace_root=str(case_dir))
    fallback_user_message = next(
        (
            msg
            for msg in agent.second_round_messages
            if msg.get("role") == "user"
            and isinstance(msg.get("content"), str)
            and "does not accept runtime image content parts" in msg.get("content", "")
        ),
        None,
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "fallback_user_message": fallback_user_message,
            "messages_seen": agent.second_round_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and fallback_user_message is not None
    )
    return ok, detail


def check_old_image_parts_are_omitted_from_followup_requests_but_traced() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "old_image_parts_omitted"
    trace_dir = TMP_DIR / "old_image_parts_omitted_trace"
    shutil.rmtree(case_dir, ignore_errors=True)
    shutil.rmtree(trace_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)

    first_image = "data:image/png;base64,Zmlyc3Q="
    second_image = "data:image/png;base64,c2Vjb25k"

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["ReadImage"],
                llm={
                    "model": "fake-vision-model",
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
            self.calls: list[list[dict]] = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.calls.append(json.loads(json.dumps(msgs)))
            if len(self.calls) == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_img",
                            "type": "function",
                            "function": {
                                "name": "ReadImage",
                                "arguments": json.dumps({"path": "second.png"}),
                            },
                        }
                    ],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            return {
                "kind": "image_tool_result",
                "text": f"path: {case_dir / 'second.png'}\nllm_image_attached: true",
                "path": str(case_dir / "second.png"),
                "image_url": second_image,
            }

    agent = FakeAgent()
    session = agent._run_session(
        "inspect images",
        workspace_root=str(case_dir),
        initial_content_parts=[{"type": "image_url", "image_url": {"url": first_image, "detail": "auto"}}],
    )
    second_call = agent.calls[1] if len(agent.calls) > 1 else []
    second_call_text = json.dumps(second_call, ensure_ascii=False)
    trace_records = load_trace_records(single_trace_path(trace_dir))
    llm_records = [row for row in trace_records if row.get("capture_type") == "llm_call"]
    last_payload = llm_records[-1].get("payload", {}) if llm_records else {}
    request_text = json.dumps(last_payload.get("request_messages", []), ensure_ascii=False)
    image_aging = last_payload.get("image_aging", {})
    image_aging_text = json.dumps(image_aging, ensure_ascii=False)
    session_state_text = (trace_dir / "_session_state.json").read_text(encoding="utf-8")
    ok = (
        session.get("termination") == "result"
        and first_image not in second_call_text
        and second_image in second_call_text
        and "Previous image omitted" in second_call_text
        and first_image not in request_text
        and "full_messages_before_request" not in last_payload
        and image_aging.get("omitted_image_count") == 1
        and first_image not in image_aging_text
        and "base64 omitted" in image_aging_text
        and first_image in session_state_text
        and second_image in session_state_text
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "second_call_contains_first_image": first_image in second_call_text,
            "second_call_contains_second_image": second_image in second_call_text,
            "request_contains_first_image": first_image in request_text,
            "has_full_messages_before_request": "full_messages_before_request" in last_payload,
            "image_aging": image_aging,
            "image_aging_contains_first_image": first_image in image_aging_text,
            "session_state_contains_first_image": first_image in session_state_text,
            "session_state_contains_second_image": second_image in session_state_text,
            "second_call": second_call,
        },
        ensure_ascii=False,
        indent=2,
    )
    return ok, detail


def check_reasoning_content_is_preserved_across_tool_rounds() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "reasoning_content_roundtrip"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )
            self._turn = 0
            self.second_round_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_write_plan",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": "outputs/marker.txt",
                                        "content": "tool completed\n",
                                    }
                                ),
                            },
                        }
                    ],
                    "reasoning_content": "deepseek-thinking-token-stream",
                    "raw_message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write_plan",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "path": "outputs/marker.txt",
                                            "content": "tool completed\n",
                                        }
                                    ),
                                },
                                "index": 0,
                            }
                        ],
                        "reasoning_content": "deepseek-thinking-token-stream",
                    },
                }
            self.second_round_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Preserve reasoning content after tool use", workspace_root=str(case_dir))
    assistant_messages = [msg for msg in agent.second_round_messages if msg.get("role") == "assistant"]
    preserved_message = next(
        (
            msg
            for msg in assistant_messages
            if msg.get("reasoning_content") == "deepseek-thinking-token-stream"
            and msg.get("tool_calls")
            and msg["tool_calls"][0].get("index") == 0
        ),
        None,
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "assistant_messages": assistant_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and preserved_message is not None
    )
    return ok, detail


def check_reasoning_content_is_preserved_across_invalid_retry_turns() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "reasoning_content_invalid_retry"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )
            self._turn = 0
            self.second_round_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "stop",
                    "content": "<answer>I have already solved it.</answer>",
                    "tool_calls": [],
                    "reasoning_content": "deepseek-invalid-turn-reasoning",
                    "raw_message": {
                        "role": "assistant",
                        "content": "<answer>I have already solved it.</answer>",
                        "reasoning_content": "deepseek-invalid-turn-reasoning",
                    },
                }
            self.second_round_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Preserve reasoning content after invalid retry turn", workspace_root=str(case_dir))
    assistant_messages = [msg for msg in agent.second_round_messages if msg.get("role") == "assistant"]
    preserved_message = next(
        (
            msg
            for msg in assistant_messages
            if msg.get("reasoning_content") == "deepseek-invalid-turn-reasoning"
            and msg.get("content") == "<answer>I have already solved it.</answer>"
            and not msg.get("tool_calls")
        ),
        None,
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "assistant_messages": assistant_messages,
            "second_round_messages": agent.second_round_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and preserved_message is not None
    )
    return ok, detail


def check_mixed_text_and_tool_calls_are_executed() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "mixed_text_tool_calls_allowed"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )
            self._turn = 0
            self.second_round_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": "I will write the marker file.",
                    "tool_calls": [
                        {
                            "id": "call_write_marker",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": "outputs/marker.txt",
                                        "content": "mixed turn executed\n",
                                    }
                                ),
                            },
                        }
                    ],
                    "reasoning_content": "mixed-turn-reasoning",
                    "raw_message": {
                        "role": "assistant",
                        "content": "I will write the marker file.",
                        "tool_calls": [
                            {
                                "id": "call_write_marker",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "path": "outputs/marker.txt",
                                            "content": "mixed turn executed\n",
                                        }
                                    ),
                                },
                                "index": 0,
                            }
                        ],
                        "reasoning_content": "mixed-turn-reasoning",
                    },
                }
            self.second_round_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Allow mixed text and tool calls", workspace_root=str(case_dir))
    marker_path = case_dir / "outputs" / "marker.txt"
    assistant_messages = [msg for msg in agent.second_round_messages if msg.get("role") == "assistant"]
    mixed_message = next(
        (
            msg
            for msg in assistant_messages
            if msg.get("content") == "I will write the marker file."
            and msg.get("reasoning_content") == "mixed-turn-reasoning"
            and msg.get("tool_calls")
        ),
        None,
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "marker_exists": marker_path.exists(),
            "marker_text": marker_path.read_text(encoding="utf-8") if marker_path.exists() else "",
            "assistant_messages": assistant_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and marker_path.exists()
        and marker_path.read_text(encoding="utf-8") == "mixed turn executed\n"
        and mixed_message is not None
    )
    return ok, detail


def check_double_encoded_tool_arguments_are_unwrapped() -> tuple[bool, str]:
    from agent_base.react_agent import parse_tool_arguments_list

    raw_arguments = json.dumps(
        json.dumps(
            {
                "path": "outputs/test.txt",
                "content": "hello\n",
            }
        )
    )
    parsed = parse_tool_arguments_list(
        [
            {
                "id": "call_write",
                "type": "function",
                "function": {
                    "name": "Write",
                    "arguments": raw_arguments,
                },
            }
        ]
    )
    detail = json.dumps({"raw_arguments": raw_arguments, "parsed": parsed}, ensure_ascii=False, indent=2)
    ok = parsed == [{"path": "outputs/test.txt", "content": "hello\n"}]
    return ok, detail


def check_truncated_tool_call_turn_is_replayed_without_execution() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "truncated_tool_call_replay"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )
            self._turn = 0
            self.executed_args: list[object] = []
            self.second_round_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "length",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_write_big",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": "report/report.md",
                                        "content": "# Oversized draft\n",
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self._turn == 2:
                self.second_round_messages = msgs
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_write_small",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": "report/report.md",
                                        "content": "# Final report\n\nDone.\n",
                                    }
                                ),
                            },
                        }
                    ],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            self.executed_args.append(tool_args)
            return super().custom_call_tool(tool_name, tool_args, **kwargs)

    agent = FakeAgent()
    session = agent._run_session("Write the report in smaller steps", workspace_root=str(case_dir))
    report_path = case_dir / "report" / "report.md"
    corrective_user_message = next(
        (
            msg
            for msg in agent.second_round_messages
            if msg.get("role") == "user"
            and "hit the output limit while emitting native tool calls" in str(msg.get("content", ""))
        ),
        None,
    )
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "executed_args": agent.executed_args,
            "corrective_user_message": corrective_user_message,
            "report_exists": report_path.exists(),
            "report_text": report_path.read_text(encoding="utf-8") if report_path.exists() else "",
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and corrective_user_message is not None
        and agent.executed_args == [{"path": "report/report.md", "content": "# Final report\n\nDone.\n"}]
        and report_path.exists()
    )
    return ok, detail


def check_reasoning_replay_error_is_reported_without_recovery() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "reasoning_replay_error_no_recovery"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )
            self._call_count = 0
            self.retry_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._call_count += 1
            if self._call_count == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_write_plan",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": "outputs/marker.txt",
                                        "content": "tool completed\n",
                                    }
                                ),
                            },
                        }
                    ],
                    "reasoning_content": "initial reasoning",
                    "raw_message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write_plan",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "path": "outputs/marker.txt",
                                            "content": "tool completed\n",
                                        }
                                    ),
                                },
                                "index": 0,
                            }
                        ],
                        "reasoning_content": "initial reasoning",
                    },
                }
            if self._call_count == 2:
                return {
                    "status": "error",
                    "error": "llm api error: The `reasoning_content` in the thinking mode must be passed back to the API.",
                }
            self.retry_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Report reasoning replay error", workspace_root=str(case_dir))
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "call_count": agent._call_count,
            "retry_messages": agent.retry_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "llm api error"
        and "must be passed back to the API" in str(session.get("result_text", ""))
        and agent._call_count == 2
        and agent.retry_messages == []
    )
    return ok, detail


def check_compact_trigger_token_parser_supports_k_suffix() -> tuple[bool, str]:
    from agent_base.model_profiles import parse_compact_trigger_tokens

    parsed_128k = parse_compact_trigger_tokens("128k", context_window=320000)
    parsed_16k = parse_compact_trigger_tokens("16k", context_window=65536)
    parsed_32k = parse_compact_trigger_tokens("32k", context_window=65536)
    detail = json.dumps(
        {
            "parsed_128k": parsed_128k,
            "parsed_16k": parsed_16k,
            "parsed_32k": parsed_32k,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = parsed_128k == 131072 and parsed_16k == 16384 and parsed_32k == 32768
    return ok, detail


def check_context_compaction_persists_summary_and_session_state() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "context_compaction_success"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            trace_dir = case_dir / "traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            super().__init__(
                function_list=["Read"],
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 40000,
                        "max_output_tokens": 512,
                        "compact_trigger_tokens": "16k",
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
                trace_dir=str(trace_dir),
            )
            self._turn = 0
            self.third_round_messages = []
            self.compaction_requests = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read_alpha",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "notes/alpha.txt"}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 8000},
                }
            if self._turn == 2:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read_beta",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "notes/beta.txt"}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20000},
                }
            self.third_round_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
                "usage": {"prompt_tokens": 700},
            }

        def call_compaction_api(self, msgs, *, runtime_deadline=None, max_output_tokens=None):
            self.compaction_requests = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": (
                    "Goal\n- Finish the task.\n\n"
                    "Files and artifacts\n- notes/alpha.txt reviewed.\n\n"
                    "Evidence and results\n- Earlier observations were collected.\n\n"
                    "Next useful actions\n- Continue from the latest raw turn."
                ),
                "tool_calls": [],
            }

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            path = tool_args["path"] if isinstance(tool_args, dict) else "unknown"
            return f"{path}\n" + ("evidence line\n" * 320)

    agent = FakeAgent()
    session = agent._run_session("Read the evidence files and continue.", workspace_root=str(case_dir))
    session_state_path = Path(session["session_state_path"])
    trace_rows = load_trace_records(Path(session["trace_path"]))
    session_state = json.loads(session_state_path.read_text(encoding="utf-8"))
    compactions = session_state.get("compactions", [])
    llm_capture_rows = [row for row in trace_rows if row.get("capture_type") == "llm_call"]
    compaction_capture_rows = [row for row in trace_rows if row.get("capture_type") == "compaction"]
    compaction_payload = compaction_capture_rows[-1].get("payload", {}) if compaction_capture_rows else {}
    summary_message = next(
        (
            msg
            for msg in agent.third_round_messages
            if msg.get("role") == "user"
            and "Runtime memory summary from earlier turns." in str(msg.get("content", ""))
        ),
        None,
    )
    roles_after_initial = [msg.get("role") for msg in agent.third_round_messages[2:]]
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "session_state_path": str(session_state_path),
            "compactions": compactions,
            "llm_capture_count": len(llm_capture_rows),
            "compaction_capture_rows": compaction_capture_rows,
            "roles_after_initial": roles_after_initial,
            "third_round_messages": agent.third_round_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("result_text") == "done"
        and session_state_path.exists()
        and session_state_path.parent.name == "traces"
        and not (case_dir / "_session_state.json").exists()
        and session_state.get("model_profile", {}).get("compact_trigger_tokens_override") == 16384
        and len(compactions) == 1
        and compactions[0].get("status") == "ok"
        and bool(compactions[0].get("summary_text", "").strip())
        and len(llm_capture_rows) == 3
        and all(isinstance(row.get("payload", {}).get("request_messages"), list) for row in llm_capture_rows)
        and all(isinstance(row.get("payload", {}).get("response"), dict) for row in llm_capture_rows)
        and len(compaction_capture_rows) == 1
        and compaction_payload.get("status") == "ok"
        and isinstance(compaction_payload.get("summary_request"), list)
        and isinstance(compaction_payload.get("summary_response"), dict)
        and isinstance(compaction_payload.get("pre_messages"), list)
        and isinstance(compaction_payload.get("post_messages"), list)
        and summary_message is not None
        and roles_after_initial[:3] == ["user", "assistant", "tool"]
    )
    return ok, detail


def check_context_compaction_refreshes_existing_memory_without_recursive_summary() -> tuple[bool, str]:
    from agent_base.context_compact import COMPACT_MEMORY_PREFIX, compact_messages
    from agent_base.model_profiles import resolve_model_profile

    captured: dict[str, object] = {}

    def fake_llm(msgs, *, runtime_deadline=None, max_output_tokens=None):
        captured["messages"] = msgs
        captured["max_output_tokens"] = max_output_tokens
        return {
            "status": "ok",
            "finish_reason": "stop",
            "content": (
                "Goal\n- Continue the task.\n\n"
                "Files and artifacts\n- outputs/source_posterior_summary.csv exists.\n\n"
                "Evidence and results\n- Earlier posterior summaries were computed.\n\n"
                "Next useful actions\n- Implement the main analysis script."
            ),
            "tool_calls": [],
        }

    model_profile = resolve_model_profile(
        "fake-model",
        configured_max_input_tokens=40000,
        configured_max_output_tokens=512,
        compact_trigger_tokens="16k",
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Prompt:\nDo the benchmark task."},
        {
            "role": "user",
            "content": (
                COMPACT_MEMORY_PREFIX
                + "Goal\n- Older compact summary.\n\nEvidence and results\n- Old result.\n\nNext useful actions\n- Keep going."
            ),
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_alpha",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{\"path\":\"notes/alpha.txt\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_alpha", "name": "Read", "content": "alpha evidence"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_beta",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{\"path\":\"notes/beta.txt\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_beta", "name": "Read", "content": "beta evidence"},
    ]

    outcome = compact_messages(
        messages=messages,
        original_prompt_text="Do the benchmark task.",
        model_name="fake-model",
        model_profile=model_profile,
        llm_caller=fake_llm,
        token_counter=lambda msgs: len(json.dumps(msgs, ensure_ascii=False)),
    )
    request_messages = captured.get("messages") or []
    request_text = ""
    if isinstance(request_messages, list) and len(request_messages) >= 2:
        request_text = str(request_messages[1].get("content", ""))
    compact_memory_messages = [
        msg
        for msg in outcome.compacted_messages
        if msg.get("role") == "user" and str(msg.get("content", "")).startswith(COMPACT_MEMORY_PREFIX)
    ]
    detail = json.dumps(
        {
            "outcome": {
                "status": outcome.status,
                "compacted_group_count": outcome.compacted_group_count,
                "kept_group_count": outcome.kept_group_count,
                "summary_text": outcome.summary_text,
                "new_token_estimate": outcome.new_token_estimate,
            },
            "request_text": request_text,
            "compacted_messages": outcome.compacted_messages,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        outcome.status == "ok"
        and outcome.compacted_group_count == 1
        and len(compact_memory_messages) == 1
        and "Previously compressed memory to preserve and refine:" in request_text
        and "Older compact summary." in request_text
        and COMPACT_MEMORY_PREFIX not in request_text
    )
    return ok, detail


def check_context_compaction_failure_is_recorded_before_hard_stop() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "context_compaction_failure"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            trace_dir = case_dir / "traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            super().__init__(
                function_list=["Read"],
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 5000,
                        "max_output_tokens": 512,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
                trace_dir=str(trace_dir),
            )
            self._turn = 0
            self.compaction_requests = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            if self._turn == 1:
                return {
                    "status": "ok",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read_large",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "notes/large.txt"}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 4500},
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "unreachable",
                "tool_calls": [],
            }

        def call_compaction_api(self, msgs, *, runtime_deadline=None, max_output_tokens=None):
            self.compaction_requests = msgs
            return {"status": "error", "error": "llm api error: compaction unavailable"}

        def custom_call_tool(self, tool_name: str, tool_args: object, **kwargs):
            return "overflow\n" + ("token\n" * 1600)

    agent = FakeAgent()
    session = agent._run_session("Read the large file.", workspace_root=str(case_dir))
    session_state_path = Path(session["session_state_path"])
    session_state = json.loads(session_state_path.read_text(encoding="utf-8"))
    compactions = session_state.get("compactions", [])
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "session_state_path": str(session_state_path),
            "compactions": compactions,
            "compaction_requests": agent.compaction_requests,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        isinstance(session.get("termination"), str)
        and session["termination"].startswith("input token limit reached")
        and session_state_path.exists()
        and session_state_path.parent.name == "traces"
        and not (case_dir / "_session_state.json").exists()
        and session_state.get("termination", "").startswith("input token limit reached")
        and len(compactions) == 1
        and compactions[0].get("status") == "error"
        and compactions[0].get("error") == "llm api error: compaction unavailable"
        and bool(agent.compaction_requests)
    )
    return ok, detail


def check_terminal_error_can_be_accepted_after_completion_artifact() -> tuple[bool, str]:
    from benchmarks.ResearchClawBench.adapter import ResearchClawBenchAgent

    case_dir = TMP_DIR / "terminal_error_accepts_artifact"
    shutil.rmtree(case_dir, ignore_errors=True)
    (case_dir / "report").mkdir(parents=True, exist_ok=True)
    (case_dir / "report" / "report.md").write_text("# Report\n\nFinished.\n", encoding="utf-8")

    class FakeAgent(ResearchClawBenchAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
            )

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            return {
                "status": "error",
                "error": "llm api error: synthetic failure after report creation",
            }

    agent = FakeAgent()
    session = agent._run_session("Recover after terminal error", workspace_root=str(case_dir))
    detail = json.dumps(session, ensure_ascii=False, indent=2)
    ok = (
        session.get("termination") == "result"
        and "report/report.md already exists" in session.get("result_text", "")
    )
    return ok, detail


def check_plaintext_result_rejection_hits_max_rounds() -> tuple[bool, str]:
    from benchmarks.ResearchClawBench.adapter import ResearchClawBenchAgent

    case_dir = TMP_DIR / "plaintext_result_max_rounds"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(ResearchClawBenchAgent):
        def __init__(self):
            super().__init__(
                function_list=["Write"],
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
                max_rounds=2,
            )
            self._turn = 0

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self._turn += 1
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": f"Round {self._turn}: still thinking.",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Keep going until max rounds", workspace_root=str(case_dir))
    detail = json.dumps(
        {
            "turns": agent._turn,
            "termination": session.get("termination"),
            "result_text": session.get("result_text"),
            "messages": session.get("messages"),
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = agent._turn == 2 and session.get("termination") == "exceed available rounds"
    return ok, detail


def check_bash_output_bounding_and_repeat_collapse() -> tuple[bool, str]:
    from agent_base.tools.tool_runtime import Bash

    case_dir = TMP_DIR / "bash_output_bounding"
    case_dir.mkdir(parents=True, exist_ok=True)

    command = "for i in $(seq 1 20); do echo WARN; done; printf 'A%.0s' $(seq 1 500)"
    result = Bash().call(
        {
            "command": command,
            "timeout": 10,
            "max_output_chars": 140,
        },
        workspace_root=case_dir,
    )

    ok = (
        isinstance(result, str)
        and "exit_code: 0" in result
        and "previous line repeated" in result
        and "output truncated" in result
        and result.count("WARN\n") == 1
    )
    return ok, result


def check_claude_models_skip_sampling_params_in_agent_runtime() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    class FakeMessage:
        content = "done"
        tool_calls = None

    class FakeClient:
        def __init__(self):
            self.request_kwargs = None
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            return self

        def create(self, **kwargs):
            self.request_kwargs = kwargs
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        finish_reason="stop",
                        message=FakeMessage(),
                    )
                ]
            )

    claude_agent = MultiTurnReactAgent(
        function_list=[],
        llm={
            "model": "anthropic/claude-3-7-sonnet",
            "api_base": "http://fake",
            "api_key": "fake",
            "generate_cfg": {
                "max_input_tokens": 10000,
                "max_output_tokens": 100,
                "max_retries": 1,
                "temperature": 0.2,
                "top_p": 0.7,
                "presence_penalty": 0.0,
            },
        },
    )
    claude_client = FakeClient()
    claude_agent._llm_client = claude_client
    claude_agent._llm_api_base = "http://fake"
    claude_reply = claude_agent.call_llm_api([{"role": "user", "content": "hello"}], max_tries=1)

    gpt_agent = MultiTurnReactAgent(
        function_list=[],
        llm={
            "model": "gpt-5.4",
            "api_base": "http://fake",
            "api_key": "fake",
            "generate_cfg": {
                "max_input_tokens": 10000,
                "max_output_tokens": 100,
                "max_retries": 1,
                "temperature": 0.2,
                "top_p": 0.7,
                "presence_penalty": 0.0,
            },
        },
    )
    gpt_client = FakeClient()
    gpt_agent._llm_client = gpt_client
    gpt_agent._llm_api_base = "http://fake"
    gpt_reply = gpt_agent.call_llm_api([{"role": "user", "content": "hello"}], max_tries=1)

    gpt55_agent = MultiTurnReactAgent(
        function_list=[],
        llm={
            "model": "openai/gpt-5.5-20260501",
            "api_base": "http://fake",
            "api_key": "fake",
            "generate_cfg": {
                "max_input_tokens": 10000,
                "max_output_tokens": 100,
                "max_retries": 1,
                "temperature": 0.2,
                "top_p": 0.7,
                "presence_penalty": 0.0,
            },
        },
    )
    gpt55_client = FakeClient()
    gpt55_agent._llm_client = gpt55_client
    gpt55_agent._llm_api_base = "http://fake"
    gpt55_reply = gpt55_agent.call_llm_api([{"role": "user", "content": "hello"}], max_tries=1)

    detail = json.dumps(
        {
            "claude_request_kwargs": claude_client.request_kwargs,
            "claude_reply": claude_reply,
            "gpt_request_kwargs": gpt_client.request_kwargs,
            "gpt_reply": gpt_reply,
            "gpt55_request_kwargs": gpt55_client.request_kwargs,
            "gpt55_reply": gpt55_reply,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        isinstance(claude_client.request_kwargs, dict)
        and "temperature" not in claude_client.request_kwargs
        and "top_p" not in claude_client.request_kwargs
        and "presence_penalty" not in claude_client.request_kwargs
        and isinstance(gpt_client.request_kwargs, dict)
        and gpt_client.request_kwargs.get("temperature") == 0.2
        and gpt_client.request_kwargs.get("top_p") == 0.7
        and gpt_client.request_kwargs.get("presence_penalty") == 0.0
        and isinstance(gpt55_client.request_kwargs, dict)
        and gpt55_client.request_kwargs.get("temperature") == 0.2
        and gpt55_client.request_kwargs.get("top_p") == 0.7
        and "presence_penalty" not in gpt55_client.request_kwargs
    )
    return ok, detail


def check_webfetch_returns_range_bounded_page_text_without_summary_llm() -> tuple[bool, str]:
    from agent_base.tools.tool_web import WebFetch

    old_timeout = os.environ.get("WEBFETCH_TIMEOUT_SECONDS")
    old_max_chars = os.environ.get("WEBFETCH_MAX_CHARS")
    try:
        os.environ["WEBFETCH_TIMEOUT_SECONDS"] = "30"
        os.environ["WEBFETCH_MAX_CHARS"] = "20"
        fetch = WebFetch()
        fetched_urls: list[str] = []

        def fake_readpage(url, runtime_deadline=None):
            fetched_urls.append(url)
            return "\n".join(["line 1 alpha", "line 2 beta", "line 3 gamma", "line 4 delta"])

        fetch.html_readpage_jina = fake_readpage
        result = fetch.call(
            {
                "url": "https://example.com",
                "start_line": 2,
                "end_line": 4,
            },
            model_name="claude-opus-4-7",
        )
        oversized_result = fetch.call(
            {
                "url": "https://example.com",
                "max_chars": 21,
            }
        )
        missing_url_result = fetch.call({})
        invalid_url_result = fetch.call({"url": {"not": "valid"}})
        start_line_result = fetch.call({"url": "https://example.com", "start_line": 0})
        end_line_result = fetch.call({"url": "https://example.com", "start_line": 3, "end_line": 2})
        max_chars_zero_result = fetch.call({"url": "https://example.com", "max_chars": 0})
        list_result = fetch.call(
            {
                "url": ["https://example.com/a", "https://example.com/b"],
                "start_line": 1,
                "end_line": 1,
                "max_chars": 20,
            }
        )
    finally:
        if old_timeout is None:
            os.environ.pop("WEBFETCH_TIMEOUT_SECONDS", None)
        else:
            os.environ["WEBFETCH_TIMEOUT_SECONDS"] = old_timeout
        if old_max_chars is None:
            os.environ.pop("WEBFETCH_MAX_CHARS", None)
        else:
            os.environ["WEBFETCH_MAX_CHARS"] = old_max_chars

    detail = json.dumps(
        {
            "result": result,
            "result_preview": result[:200],
            "oversized_result": oversized_result,
            "missing_url_result": missing_url_result,
            "invalid_url_result": invalid_url_result,
            "start_line_result": start_line_result,
            "end_line_result": end_line_result,
            "max_chars_zero_result": max_chars_zero_result,
            "list_result": list_result,
            "fetched_urls": fetched_urls,
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        "source_type: web" in result
        and "start_line: 2" in result
        and "end_line: 4" in result
        and "truncated: true" in result
        and "line 2 beta" in result
        and "line 1 alpha" not in result
        and "Summary:" not in result
        and "Evidence in page:" not in result
        and not hasattr(fetch, "call_server")
        and "goal" not in WebFetch.parameters["properties"]
        and WebFetch.parameters["required"] == ["url"]
        and "max_chars must be <= WEBFETCH_MAX_CHARS (20)" in oversized_result
        and "Missing required parameter: url" in missing_url_result
        and "Parameter 'url' must be of type string or array" in invalid_url_result
        and "start_line must be >= 1" in start_line_result
        and "end_line must be >= start_line" in end_line_result
        and "max_chars must be > 0" in max_chars_zero_result
        and list_result.count("source_type: web") == 2
        and "=======\nurl: https://example.com/b" in list_result
        and fetched_urls == ["https://example.com", "https://example.com/a", "https://example.com/b"]
    )
    return ok, detail


def check_session_state_is_only_written_with_trace_dir() -> tuple[bool, str]:
    from agent_base.react_agent import MultiTurnReactAgent

    case_dir = TMP_DIR / "session_state_without_trace"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=[],
                llm={
                    "model": "fake-model",
                    "generate_cfg": {
                        "max_input_tokens": 10000,
                        "max_output_tokens": 512,
                        "max_retries": 1,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "presence_penalty": 0.0,
                    },
                },
            )

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session("Return done.", workspace_root=str(case_dir))
    workspace_state_path = case_dir / "_session_state.json"
    detail = json.dumps(
        {
            "termination": session.get("termination"),
            "session_state_path": session.get("session_state_path"),
            "workspace_state_exists": workspace_state_path.exists(),
        },
        ensure_ascii=False,
        indent=2,
    )
    ok = (
        session.get("termination") == "result"
        and session.get("session_state_path") == ""
        and not workspace_state_path.exists()
    )
    return ok, detail


def main() -> int:
    bootstrap()
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    checks = [
        ("Workspace root auto-create", check_workspace_root_is_created_when_missing),
        ("Required env enforced", check_required_env_is_enforced),
        ("ReadPDF relative image path", check_readpdf_relative_image_path),
        ("TerminalInterrupt remainder", check_terminal_interrupt_preserves_remainder),
        ("Agent runtime limit", check_agent_runtime_limit_on_tool_execution),
        ("LLM hard timeout", check_llm_hard_timeout_interrupts_blocking_call),
        ("Parallel ReadImage tool order", check_parallel_readimage_tool_message_order),
        ("DeepSeek ReadImage fallback", check_deepseek_readimage_falls_back_to_text_only_context),
        ("Old image parts omitted but traced", check_old_image_parts_are_omitted_from_followup_requests_but_traced),
        ("Reasoning content preserved", check_reasoning_content_is_preserved_across_tool_rounds),
        ("Reasoning content preserved on invalid retry", check_reasoning_content_is_preserved_across_invalid_retry_turns),
        ("Mixed text and tool calls executed", check_mixed_text_and_tool_calls_are_executed),
        ("Reasoning replay error reported", check_reasoning_replay_error_is_reported_without_recovery),
        ("Compact trigger parser", check_compact_trigger_token_parser_supports_k_suffix),
        ("Context compaction persists state", check_context_compaction_persists_summary_and_session_state),
        ("Session state requires trace dir", check_session_state_is_only_written_with_trace_dir),
        ("Context compaction refreshes memory", check_context_compaction_refreshes_existing_memory_without_recursive_summary),
        ("Context compaction failure recorded", check_context_compaction_failure_is_recorded_before_hard_stop),
        ("Double-encoded tool args unwrapped", check_double_encoded_tool_arguments_are_unwrapped),
        ("Truncated tool call replay", check_truncated_tool_call_turn_is_replayed_without_execution),
        ("Terminal error accepts artifact", check_terminal_error_can_be_accepted_after_completion_artifact),
        ("Claude runtime sampling params", check_claude_models_skip_sampling_params_in_agent_runtime),
        ("WebFetch range-bounded page text", check_webfetch_returns_range_bounded_page_text_without_summary_llm),
        ("Plaintext result max rounds", check_plaintext_result_rejection_hits_max_rounds),
        ("Bash output bounding", check_bash_output_bounding_and_repeat_collapse),
    ]

    failures: list[str] = []
    outputs: list[str] = []
    for name, func in checks:
        ok, detail = func()
        outputs.append(f"[{name}]\n{detail}")
        if not ok:
            failures.append(name)

    result = EdgeCaseResult(
        status="PASS" if not failures else "FAIL",
        detail="All edge-case checks passed." if not failures else f"Failed checks: {', '.join(failures)}",
        output_preview=preview("\n\n".join(outputs)),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
