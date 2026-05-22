#!/usr/bin/env python3

import asyncio
import json
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_support import TEST_RUNS_DIR, bootstrap, load_trace_records, preview, single_trace_path


TMP_DIR = TEST_RUNS_DIR / "openai_api_checks"
TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@dataclass
class OpenAIAPICheckResult:
    status: str
    detail: str
    output_preview: str


def main() -> int:
    bootstrap()

    import api.openai_server as openai_server
    from agent_base.react_agent import MultiTurnReactAgent
    from api.openai_server import (
        ServerConfig,
        build_agent_prompt,
        build_input_wrapper_messages,
        build_output_wrapper_messages,
        build_passthrough_input_plan,
        create_app,
        extract_json_object,
        make_chat_completion_response,
        prepare_openai_input,
        run_chat_completion,
    )

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "model": "RH--fake-vision-model",
        "messages": [
            {"role": "system", "content": "Answer exactly in the requested format."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare the two images. Return JSON with key answer."},
                    {"type": "image_url", "image_url": {"url": TINY_PNG_DATA_URL}},
                    {"type": "image_url", "image_url": {"url": TINY_PNG_DATA_URL}},
                ],
            },
        ],
        "response_format": {"type": "json_object"},
    }

    prepared = prepare_openai_input(payload["messages"], TMP_DIR)
    saved_image = TMP_DIR / "inputs" / "images" / "image_000.png"
    second_saved_image = TMP_DIR / "inputs" / "images" / "image_001.png"
    input_wrapper_messages = build_input_wrapper_messages(prepared=prepared, payload=payload)
    passthrough_plan = build_passthrough_input_plan(prepared=prepared, payload=payload)
    agent_prompt = build_agent_prompt(
        {
            "agent_instruction": "Use both saved images.",
            "output_contract": "Return JSON.",
            "wrapper_notes": "test",
        },
        prepared,
    )
    output_wrapper_messages = build_output_wrapper_messages(
        prepared=prepared,
        payload=payload,
        input_plan={"output_contract": "Return JSON."},
        agent_result_text="The answer is 12. Do not refer to answer.md.",
    )
    parsed_plan = extract_json_object(
        '```json\n{"agent_instruction": "Use the saved image.", "output_contract": "Return JSON.", "wrapper_notes": "ok"}\n```'
    )
    response = make_chat_completion_response(request_id="chatcmpl_test", model="RH", content='{"answer":"white"}')

    trace_dir = TMP_DIR / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    class FakeAgent(MultiTurnReactAgent):
        def __init__(self):
            super().__init__(
                function_list=[],
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
            self.seen_messages = []

        def call_llm_api(self, msgs, max_tries=10, runtime_deadline=None):
            self.seen_messages = msgs
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": "Final answer: white",
                "tool_calls": [],
            }

    agent = FakeAgent()
    session = agent._run_session(
        "Solve the image task.",
        workspace_root=str(TMP_DIR / "agent_workspace"),
        initial_content_parts=prepared.initial_content_parts,
    )
    trace_path = single_trace_path(trace_dir)
    rows = load_trace_records(trace_path) if trace_path else []
    first_user_content = agent.seen_messages[1]["content"] if len(agent.seen_messages) > 1 else None
    first_user_trace = rows[1].get("text", "") if len(rows) > 1 else ""
    session_state_path = Path(session.get("session_state_path", ""))

    api_runs_root = TMP_DIR / "api_runs"
    fake_seen: dict[str, str] = {}

    class FakeAPIAgent:
        def __init__(self, function_list, llm, trace_dir, role_prompt=None):
            self.trace_dir = Path(trace_dir)
            fake_seen["trace_dir"] = str(self.trace_dir)
            fake_seen.setdefault("trace_dirs", []).append(str(self.trace_dir))
            fake_seen.setdefault("function_lists", []).append(list(function_list or []))
            fake_seen["model"] = str(llm.get("model", ""))
            fake_seen.setdefault("models", []).append(str(llm.get("model", "")))

        def call_compaction_api(self, messages, max_output_tokens=None):
            if messages and messages[0]["content"].startswith("You are the ResearchHarness input wrapper"):
                return {
                    "status": "ok",
                    "finish_reason": "stop",
                    "content": json.dumps(
                        {
                            "agent_instruction": "Read the arithmetic image and solve it.",
                            "output_contract": "Return JSON with expression and answer.",
                            "wrapper_notes": "test",
                        }
                    ),
                    "tool_calls": [],
                }
            return {
                "status": "ok",
                "finish_reason": "stop",
                "content": '{"expression":"7 + 5","answer":12}',
                "tool_calls": [],
            }

        def _run_session(self, prompt, workspace_root=None, initial_content_parts=None):
            fake_seen["workspace_root"] = str(workspace_root)
            fake_seen.setdefault("workspace_roots", []).append(str(workspace_root))
            fake_seen["initial_content_parts"] = str(bool(initial_content_parts))
            fake_seen.setdefault("initial_content_parts_values", []).append(str(bool(initial_content_parts)))
            return {
                "result_text": "Final answer: 12",
                "termination": "result",
                "trace_path": str(self.trace_dir / "trace_fake.jsonl"),
            }

    previous_agent_cls = openai_server.MultiTurnReactAgent
    previous_default_llm_config = openai_server.default_llm_config
    openai_server.MultiTurnReactAgent = FakeAPIAgent
    openai_server.default_llm_config = lambda model_name=None: {
        "model": str(model_name or "fake-vision-model"),
        "api_key": "fake",
        "api_base": "http://fake.invalid/v1",
        "generate_cfg": {
            "max_input_tokens": 10000,
            "max_retries": 1,
            "temperature": 0.0,
            "top_p": 1.0,
            "presence_penalty": 0.0,
        },
    }
    try:
        api_response = run_chat_completion(
            payload,
            ServerConfig(api_runs_dir=api_runs_root, input_wrapper=True, output_wrapper=True),
        )
        custom_workspace = TMP_DIR / "custom_api_workspace"
        custom_workspace.mkdir(parents=True, exist_ok=True)
        custom_payload = dict(payload)
        custom_payload["workspace-root"] = str(custom_workspace)
        custom_response = run_chat_completion(
            custom_payload,
            ServerConfig(api_runs_dir=api_runs_root / "custom", input_wrapper=False, output_wrapper=False),
        )
        missing_workspace = TMP_DIR / "missing_api_workspace"
        missing_payload = {
            "model": "RH--fake-vision-model",
            "workspace-root": str(missing_workspace),
            "messages": [{"role": "user", "content": "Use the default workspace because the requested one is missing."}],
        }
        missing_workspace_response = run_chat_completion(
            missing_payload,
            ServerConfig(api_runs_dir=api_runs_root / "missing_workspace", input_wrapper=False, output_wrapper=False),
        )
        extra_tool_response = run_chat_completion(
            {
                "model": "RH",
                "messages": [{"role": "user", "content": "Use the optional editor if needed."}],
            },
            ServerConfig(
                api_runs_dir=api_runs_root / "extra_tool",
                input_wrapper=False,
                output_wrapper=False,
                extra_tools=("str_replace_editor",),
            ),
        )
        explicit_tool_response = run_chat_completion(
            {
                "model": "RH",
                "messages": [{"role": "user", "content": "Use only local code tools if needed."}],
            },
            ServerConfig(
                api_runs_dir=api_runs_root / "explicit_tools",
                input_wrapper=False,
                output_wrapper=False,
                tool_names=("Read", "Write", "Edit", "Bash"),
            ),
        )
    finally:
        openai_server.MultiTurnReactAgent = previous_agent_cls
        openai_server.default_llm_config = previous_default_llm_config

    invalid_model_rejected = False
    try:
        invalid_payload = dict(payload)
        invalid_payload["model"] = "fake-vision-model"
        run_chat_completion(
            invalid_payload,
            ServerConfig(api_runs_dir=api_runs_root / "invalid_model", input_wrapper=False, output_wrapper=False),
        )
    except openai_server.OpenAICompatError as exc:
        invalid_model_rejected = exc.status_code == 400 and "RH--" in exc.message

    workspace_alias_rejected = False
    try:
        alias_payload = {
            "model": "RH",
            "workspace_root": str(TMP_DIR),
            "messages": [{"role": "user", "content": "Use a misspelled workspace field."}],
        }
        run_chat_completion(
            alias_payload,
            ServerConfig(api_runs_dir=api_runs_root / "workspace_alias", input_wrapper=False, output_wrapper=False),
        )
    except openai_server.OpenAICompatError as exc:
        workspace_alias_rejected = exc.status_code == 400 and "workspace-root" in exc.message

    default_model_label, default_backend_model = openai_server.resolve_api_model_selection("")
    default_server_config = ServerConfig(api_runs_dir=api_runs_root / "defaults")
    high_concurrency_config = ServerConfig(api_runs_dir=api_runs_root / "concurrency", max_concurrent_runs=4)

    concurrency_seen = {"active": 0, "max_active": 0, "calls": 0}
    concurrency_lock = threading.Lock()

    def fake_slow_run(payload, config):
        with concurrency_lock:
            concurrency_seen["active"] += 1
            concurrency_seen["calls"] += 1
            concurrency_seen["max_active"] = max(concurrency_seen["max_active"], concurrency_seen["active"])
        time.sleep(0.12)
        with concurrency_lock:
            concurrency_seen["active"] -= 1
        return make_chat_completion_response(
            request_id="chatcmpl_concurrency_test",
            model=str(payload.get("model") or "RH"),
            content="ok",
        )

    previous_run_chat_completion = openai_server.run_chat_completion
    openai_server.run_chat_completion = fake_slow_run
    try:
        concurrency_app = create_app(high_concurrency_config)
        chat_route = next(route for route in concurrency_app.routes if getattr(route, "path", "") == "/v1/chat/completions")
        health_route = next(route for route in concurrency_app.routes if getattr(route, "path", "") == "/v1/health")

        async def run_concurrency_probe():
            request_payloads = [
                {"model": "RH", "messages": [{"role": "user", "content": f"concurrency probe {index}"}]}
                for index in range(8)
            ]
            async with concurrency_app.router.lifespan_context(concurrency_app):
                health_info = await health_route.endpoint()
                start = time.perf_counter()
                results = await asyncio.gather(*(chat_route.endpoint(payload) for payload in request_payloads))
                elapsed = time.perf_counter() - start
            return results, elapsed, health_info

        concurrency_results, concurrency_elapsed, concurrency_health = asyncio.run(run_concurrency_probe())
    finally:
        openai_server.run_chat_completion = previous_run_chat_completion

    run_dirs = sorted(api_runs_root.glob("run_*"))
    api_run_dir = run_dirs[0] if run_dirs else None
    api_agent_workspace = api_run_dir / "agent_workspace" if api_run_dir else None
    api_agent_trace_dir = api_run_dir / "agent_trace" if api_run_dir else None
    api_saved_image = api_agent_workspace / "inputs" / "images" / "image_000.png" if api_agent_workspace else None
    api_second_saved_image = api_agent_workspace / "inputs" / "images" / "image_001.png" if api_agent_workspace else None
    custom_run_dirs = sorted((api_runs_root / "custom").glob("run_*"))
    custom_run_dir = custom_run_dirs[0] if custom_run_dirs else None
    custom_agent_trace_dir = custom_run_dir / "agent_trace" if custom_run_dir else None
    custom_default_workspace = custom_run_dir / "agent_workspace" if custom_run_dir else None
    custom_saved_image = custom_workspace / "inputs" / "images" / "image_000.png" if custom_run_dir else None
    missing_run_dirs = sorted((api_runs_root / "missing_workspace").glob("run_*"))
    missing_run_dir = missing_run_dirs[0] if missing_run_dirs else None
    missing_default_workspace = missing_run_dir / "agent_workspace" if missing_run_dir else None
    missing_agent_trace_dir = missing_run_dir / "agent_trace" if missing_run_dir else None
    workspace_roots = fake_seen.get("workspace_roots", [])
    trace_dirs = fake_seen.get("trace_dirs", [])
    default_api_events = load_trace_records(api_agent_trace_dir / "api_trace.jsonl") if api_agent_trace_dir else []
    custom_api_events = load_trace_records(custom_agent_trace_dir / "api_trace.jsonl") if custom_agent_trace_dir else []
    missing_api_events = load_trace_records(missing_agent_trace_dir / "api_trace.jsonl") if missing_agent_trace_dir else []
    default_workspace_event = next((row for row in default_api_events if row.get("event") == "workspace_selection"), {})
    custom_workspace_event = next((row for row in custom_api_events if row.get("event") == "workspace_selection"), {})
    missing_workspace_event = next((row for row in missing_api_events if row.get("event") == "workspace_selection"), {})

    ok = (
        prepared.image_paths == ["inputs/images/image_000.png", "inputs/images/image_001.png"]
        and saved_image.exists()
        and second_saved_image.exists()
        and prepared.initial_content_parts
        and len(prepared.initial_content_parts) == 4
        and input_wrapper_messages[1]["content"].find("response_format") >= 0
        and "self-contained" in agent_prompt
        and "must not depend on local files as the only carrier" in agent_prompt
        and "self-contained" in output_wrapper_messages[0]["content"]
        and "must not depend on" in output_wrapper_messages[0]["content"]
        and passthrough_plan["agent_instruction"].find("Compare the two images.") >= 0
        and passthrough_plan["wrapper_notes"].find("Input wrapper disabled") >= 0
        and parsed_plan["output_contract"] == "Return JSON."
        and response["choices"][0]["message"]["content"] == '{"answer":"white"}'
        and session.get("result_text") == "Final answer: white"
        and isinstance(first_user_content, list)
        and any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and "inputs/images/image_000.png" in str(part.get("text", ""))
            for part in first_user_content
        )
        and any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and "inputs/images/image_001.png" in str(part.get("text", ""))
            for part in first_user_content
        )
        and sum(1 for part in first_user_content if isinstance(part, dict) and part.get("type") == "image_url") == 2
        and "base64 omitted" in first_user_trace
        and "inputs/images/image_000.png" in first_user_trace
        and "inputs/images/image_001.png" in first_user_trace
        and session_state_path.exists()
        and session_state_path.parent == trace_dir
        and session_state_path.name.startswith("session_state_")
        and trace_path is not None
        and session_state_path.name.removeprefix("session_state_").removesuffix(".json")
        == trace_path.name.removeprefix("trace_").removesuffix(".jsonl")
        and not (TMP_DIR / "agent_workspace" / "_session_state.json").exists()
        and api_response["choices"][0]["message"]["content"] == '{"expression":"7 + 5","answer":12}'
        and api_response["model"] == "RH--fake-vision-model"
        and extra_tool_response["model"] == "RH"
        and explicit_tool_response["model"] == "RH"
        and "fake-vision-model" in fake_seen.get("models", [])
        and any("str_replace_editor" in names and "AskUser" not in names for names in fake_seen.get("function_lists", []))
        and any(names == ["Read", "Write", "Edit", "Bash"] for names in fake_seen.get("function_lists", []))
        and invalid_model_rejected
        and workspace_alias_rejected
        and default_model_label == "RH"
        and bool(default_backend_model)
        and default_server_config.input_wrapper is False
        and default_server_config.output_wrapper is False
        and default_server_config.max_concurrent_runs == openai_server.DEFAULT_MAX_CONCURRENT_RUNS
        and high_concurrency_config.max_concurrent_runs == 4
        and concurrency_seen["calls"] == 8
        and concurrency_seen["max_active"] == 4
        and concurrency_elapsed < 0.6
        and concurrency_health.get("max_concurrent_runs") == 4
        and all(result["choices"][0]["message"]["content"] == "ok" for result in concurrency_results)
        and api_run_dir is not None
        and api_agent_workspace is not None
        and api_agent_workspace.is_dir()
        and api_agent_trace_dir is not None
        and api_agent_trace_dir.is_dir()
        and api_saved_image is not None
        and api_saved_image.exists()
        and api_second_saved_image is not None
        and api_second_saved_image.exists()
        and len(workspace_roots) >= 3
        and Path(workspace_roots[0]).name == "agent_workspace"
        and Path(workspace_roots[1]) == custom_workspace
        and missing_default_workspace is not None
        and Path(workspace_roots[2]) == missing_default_workspace
        and not missing_workspace.exists()
        and custom_response["choices"][0]["message"]["content"] == "Final answer: 12"
        and missing_workspace_response["choices"][0]["message"]["content"] == "Final answer: 12"
        and custom_agent_trace_dir is not None
        and custom_agent_trace_dir.is_dir()
        and custom_default_workspace is not None
        and not custom_default_workspace.exists()
        and custom_saved_image is not None
        and custom_saved_image.exists()
        and missing_agent_trace_dir is not None
        and missing_agent_trace_dir.is_dir()
        and missing_default_workspace is not None
        and missing_default_workspace.is_dir()
        and trace_dirs
        and all(Path(trace_dir_text).name == "agent_trace" for trace_dir_text in trace_dirs)
        and (api_agent_trace_dir / "api_trace.jsonl").exists()
        and (custom_agent_trace_dir / "api_trace.jsonl").exists()
        and (missing_agent_trace_dir / "api_trace.jsonl").exists()
        and default_workspace_event.get("payload", {}).get("source") == "default"
        and custom_workspace_event.get("payload", {}).get("source") == "request"
        and custom_workspace_event.get("payload", {}).get("workspace_root") == str(custom_workspace)
        and missing_workspace_event.get("payload", {}).get("source") == "default"
        and missing_workspace_event.get("payload", {}).get("reason") == "request_workspace_root_is_not_existing_directory"
    )

    result = OpenAIAPICheckResult(
        status="PASS" if ok else "FAIL",
        detail="OpenAI-compatible API helpers and initial multimodal user content are working."
        if ok
        else "OpenAI-compatible API checks failed.",
        output_preview=preview(
            json.dumps(
                {
                    "image_paths": prepared.image_paths,
                    "saved_image_exists": saved_image.exists(),
                    "second_saved_image_exists": second_saved_image.exists(),
                    "input_wrapper_messages": input_wrapper_messages,
                    "passthrough_plan": passthrough_plan,
                    "parsed_plan": parsed_plan,
                    "response": response,
                    "session_result": session.get("result_text"),
                    "first_user_content_type": type(first_user_content).__name__,
                    "first_user_trace": first_user_trace,
                    "api_response": api_response,
                    "extra_tool_response": extra_tool_response,
                    "explicit_tool_response": explicit_tool_response,
                    "api_run_dir": str(api_run_dir) if api_run_dir else "",
                    "fake_seen": fake_seen,
                    "custom_response": custom_response,
                    "custom_workspace": str(custom_workspace),
                    "custom_saved_image": str(custom_saved_image) if custom_saved_image else "",
                    "missing_workspace_response": missing_workspace_response,
                    "missing_default_workspace": str(missing_default_workspace) if missing_default_workspace else "",
                    "workspace_events": [
                        default_workspace_event,
                        custom_workspace_event,
                        missing_workspace_event,
                    ],
                    "invalid_model_rejected": invalid_model_rejected,
                    "workspace_alias_rejected": workspace_alias_rejected,
                    "default_model_selection": [default_model_label, default_backend_model],
                    "default_wrapper_config": [
                        default_server_config.input_wrapper,
                        default_server_config.output_wrapper,
                    ],
                    "default_max_concurrent_runs": default_server_config.max_concurrent_runs,
                    "concurrency_seen": concurrency_seen,
                    "concurrency_elapsed": concurrency_elapsed,
                    "concurrency_health": concurrency_health,
                },
                ensure_ascii=False,
                indent=2,
            )
        ),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
