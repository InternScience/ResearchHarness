import json
import os
import tempfile
import types
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_base.training_export as export_module
from agent_base.training_export import (
    EXPORT_DIR_NAME,
    TrainingExportConfig,
    canonical_json_bytes,
    export_turn_training_bundle,
    validate_turn_training_bundle,
)


RUN_ID = "run-001"
MODEL = "relay/gpt-test"


def _tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read one workspace file.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }


def _accepted_response(request, *, response_id, content, tool_calls, retry_count=0):
    raw = {
        "id": response_id,
        "model": MODEL,
        "choices": [
            {
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    attempts = []
    for attempt_index in range(retry_count):
        attempts.append(
            {
                "attempt_index": attempt_index,
                "provider_request": request,
                "raw_provider_response": {
                    "id": f"{response_id}-rejected-{attempt_index}",
                    "model": MODEL,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 0},
                },
                "status": "rejected_empty",
                "error": "empty response from llm api",
            }
        )
    attempts.append(
        {
            "attempt_index": retry_count,
            "provider_request": request,
            "raw_provider_response": raw,
            "status": "accepted",
        }
    )
    return {
        "status": "ok",
        "finish_reason": "tool_calls" if tool_calls else "stop",
        "content": content,
        "reasoning_content": None,
        "tool_calls": tool_calls,
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "provider_request": request,
        "raw_provider_response": raw,
        "provider_retry_count": retry_count,
        "provider_attempts": attempts,
        "requested_model": MODEL,
        "provider_model": MODEL,
    }


def _main_payload(request, response):
    return {
        "model_name": MODEL,
        "request_messages": request["messages"],
        "native_tools": request.get("tools", []),
        "provider_request": request,
        "raw_provider_response": response["raw_provider_response"],
        "provider_retry_count": response["provider_retry_count"],
        "requested_model": response["requested_model"],
        "provider_model": response["provider_model"],
        "response": response,
    }


def _trace_rows():
    initial = [
        {"role": "system", "content": "Fix the repository using tools."},
        {"role": "user", "content": "Inspect src/auth.py and fix token expiry."},
    ]
    call = {
        "id": "call-read-1",
        "type": "function",
        "function": {"name": "Read", "arguments": '{"file_path":"src/auth.py"}'},
    }
    first_request = {
        "model": MODEL,
        "messages": initial,
        "max_tokens": 512,
        "temperature": 0.2,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "tools": [_tool_schema()],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    first_response = _accepted_response(
        first_request,
        response_id="resp-tool",
        content=None,
        tool_calls=[call],
        retry_count=1,
    )
    pre_messages = [
        *initial,
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call-read-1", "content": "TOKEN_TTL = 60"},
    ]
    summary_messages = [
        {"role": "system", "content": "Compress history without tools."},
        {"role": "user", "content": "Summarize the auth task."},
    ]
    summary_request = {
        "model": MODEL,
        "messages": summary_messages,
        "max_tokens": 128,
        "temperature": 0.2,
        "top_p": 0.9,
        "presence_penalty": 0.0,
    }
    summary_text = "Goal: fix token expiry. Evidence: TOKEN_TTL is 60."
    summary_response = _accepted_response(
        summary_request,
        response_id="resp-memory",
        content=summary_text,
        tool_calls=[],
    )
    post_messages = [
        *initial,
        {"role": "user", "content": "Runtime memory summary from earlier turns.\n\n" + summary_text},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call-read-1", "content": "TOKEN_TTL = 60"},
    ]
    second_request = {
        "model": MODEL,
        "messages": post_messages,
        "max_tokens": 512,
        "temperature": 0.2,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "tools": [_tool_schema()],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    second_response = _accepted_response(
        second_request,
        response_id="resp-final",
        content="Updated the token expiry handling.",
        tool_calls=[],
    )
    return [
        {
            "run_id": RUN_ID,
            "event_index": 1,
            "turn_index": 1,
            "capture_type": "llm_call",
            "payload": _main_payload(first_request, first_response),
        },
        {
            "run_id": RUN_ID,
            "event_index": 2,
            "turn_index": 1,
            "capture_type": "compaction",
            "payload": {
                "trigger_reason": "usage",
                "status": "ok",
                "prior_token_estimate": 9000,
                "new_token_estimate": 2200,
                "compacted_group_count": 4,
                "kept_group_count": 1,
                "existing_memory_text": "",
                "summary_request": summary_messages,
                "summary_response": summary_response,
                "summary_text": summary_text,
                "pre_messages": pre_messages,
                "post_messages": post_messages,
            },
        },
        {
            "run_id": RUN_ID,
            "event_index": 3,
            "turn_index": 2,
            "capture_type": "llm_call",
            "payload": _main_payload(second_request, second_response),
        },
    ]


def _write_trace(path, rows=None):
    rows = _trace_rows() if rows is None else rows
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _config(tmp_path, *, name="case", patch_file=True):
    case = tmp_path / name
    case.mkdir()
    patch_path = case / "agent.patch"
    if patch_file:
        patch_path.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    return TrainingExportConfig(
        output_dir=case / EXPORT_DIR_NAME,
        task_id="task-auth",
        task_revision_id="task-auth@base-abc",
        rollout_id="r0",
        rollout_index=0,
        run_id=RUN_ID,
        harness_revision="72e8a447",
        scaffold_version="react-v1",
        tool_protocol_version="native-tools-v1",
        tokenizer_revision="tok-rev1",
        final_patch_path=patch_path if patch_file else None,
    )


def _units(bundle):
    return [
        json.loads(line)
        for line in (bundle / "turn-training-units.v1.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _write_json(path, value):
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_units(bundle, units):
    (bundle / "turn-training-units.v1.jsonl").write_bytes(
        b"".join(canonical_json_bytes(unit) + b"\n" for unit in units)
    )


def _reseal_summary(bundle, summary):
    payload = json.loads(json.dumps(summary))
    payload.pop("integrity", None)
    summary["integrity"] = {
        "canonicalization": export_module.CANONICALIZATION,
        "summary_payload_sha256": export_module._hash_json(
            "researchharness.summary-payload.v1", payload
        ),
    }
    _write_json(bundle / "rollout-summary.v1.json", summary)


def _reseal_bundle(bundle):
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload_files = [
        path
        for path in export_module._all_files(bundle)
        if path.name not in {"manifest.json", "SHA256SUMS"}
    ]
    manifest["files"] = [export_module._file_ref(bundle, path) for path in payload_files]
    _write_json(manifest_path, manifest)
    checksum_files = [
        path for path in export_module._all_files(bundle) if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{export_module._sha256_file(path)}  {path.relative_to(bundle).as_posix()}\n"
            for path in checksum_files
        ),
        encoding="utf-8",
    )


def _export(tmp_path, *, name="case", rows=None, patch_file=True):
    config = _config(tmp_path, name=name, patch_file=patch_file)
    trace = _write_trace(tmp_path / name / "trace.jsonl", rows)
    bundle = export_turn_training_bundle(
        config,
        trace_path=trace,
        termination={
            "status": "completed",
            "reason": "result",
            "submitted": True,
            "clean_termination": True,
        },
    )
    return config, bundle


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = list(tool_calls or [])
        self.reasoning_content = None

    def model_dump(self):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in self.tool_calls
            ],
        }


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 2}


class FakeResponse:
    def __init__(self, response_id, content, tool_calls=None):
        self.id = response_id
        self.model = MODEL
        self.usage = FakeUsage()
        message = FakeMessage(content, tool_calls=tool_calls)
        self.choices = [
            types.SimpleNamespace(
                finish_reason="tool_calls" if message.tool_calls else "stop",
                message=message,
            )
        ]

    def model_dump(self):
        return {
            "id": self.id,
            "model": self.model,
            "choices": [
                {
                    "finish_reason": self.choices[0].finish_reason,
                    "message": self.choices[0].message.model_dump(),
                }
            ],
            "usage": self.usage.model_dump(),
        }


class FakeMalformedResponse:
    def __init__(self, response_id):
        self.id = response_id
        self.model = MODEL
        self.usage = FakeUsage()
        self.choices = []

    def model_dump(self):
        return {
            "id": self.id,
            "model": self.model,
            "choices": [],
            "usage": self.usage.model_dump(),
        }


class FakeResponseWithoutDump:
    def __init__(self, content):
        self.model = MODEL
        self.usage = FakeUsage()
        self.choices = [
            types.SimpleNamespace(
                finish_reason="stop",
                message=FakeMessage(content),
            )
        ]


def _fake_tool_call(call_id, name, arguments):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class TrainingExportTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_contract_smoke_closes_tool_round_compaction_and_integrity(self):
        config, bundle = _export(self.tmp_path)
        summary = validate_turn_training_bundle(bundle)
        units = _units(bundle)
        self.assertEqual(bundle, config.output_dir)
        self.assertEqual(summary["main_agent_turn_count"], 2)
        self.assertEqual(summary["memory_compaction_count"], 1)
        self.assertEqual(summary["capture_error_count"], 0)
        self.assertEqual([unit["unit_type"] for unit in units], [
            "main_agent_turn", "memory_compaction", "main_agent_turn"
        ])
        self.assertEqual(units[0]["response"]["message"]["tool_calls"][0]["id"], "call-read-1")
        self.assertEqual(units[0]["provenance"]["provider_retry_count"], 1)
        self.assertEqual(units[0]["provenance"]["requested_model"], MODEL)
        self.assertEqual(units[0]["provenance"]["provider_model"], MODEL)
        self.assertTrue(any(
            message.get("role") == "tool" and message.get("tool_call_id") == "call-read-1"
            for message in units[2]["request"]["messages"]
        ))
        self.assertEqual(units[1]["response"]["message"]["content"], _trace_rows()[1]["payload"]["summary_text"])
        raw_ref = units[0]["source"]["raw_provider_response_ref"]
        self.assertEqual(json.loads((bundle / raw_ref["path"]).read_text())["id"], "resp-tool")
        self.assertEqual(summary["final_patch_ref"]["path"], "agent.patch")
        self.assertNotIn("resolved", summary)
        self.assertNotIn("reward", summary)

    def test_missing_provider_boundary_is_audited_as_error(self):
        row = _trace_rows()[0]
        row["payload"].pop("provider_request")
        row["payload"].pop("raw_provider_response")
        row["payload"]["provider_retry_count"] = 2
        row["payload"]["response"] = {
            "status": "error",
            "error": "provider unavailable",
            "content": "",
            "tool_calls": [],
            "provider_retry_count": 2,
            "requested_model": MODEL,
            "provider_model": None,
        }
        _, bundle = _export(self.tmp_path, name="provider-error", rows=[row], patch_file=False)
        self.assertEqual(_units(bundle)[0]["capture_status"], "error")
        self.assertEqual(validate_turn_training_bundle(bundle)["capture_error_count"], 1)

    def test_failed_compaction_is_preserved_as_error_unit(self):
        rows = _trace_rows()
        failed = rows[1]
        failed["payload"].update(
            status="error",
            summary_response={"status": "error", "error": "summary unavailable", "tool_calls": []},
            summary_text="",
            post_messages=failed["payload"]["pre_messages"],
        )
        _, bundle = _export(self.tmp_path, name="compact-error", rows=[rows[0], failed], patch_file=False)
        self.assertEqual([unit["capture_status"] for unit in _units(bundle)], ["ok", "error"])

    def test_successful_compaction_must_match_next_real_request(self):
        rows = _trace_rows()
        rows[2]["payload"]["provider_request"]["messages"] = [{"role": "user", "content": "wrong"}]
        config = _config(self.tmp_path, name="bad-closure", patch_file=False)
        trace = _write_trace(self.tmp_path / "bad-closure/trace.jsonl", rows)
        with self.assertRaisesRegex(ValueError, "next provider request"):
            export_turn_training_bundle(config, trace_path=trace, termination="result")
        self.assertFalse(config.output_dir.exists())

    def test_compaction_next_input_uses_real_image_aging_transform(self):
        from agent_base.react_agent import prepare_messages_for_llm

        rows = _trace_rows()[1:]
        rows[0]["event_index"] = 1
        rows[1]["event_index"] = 2
        post_messages = rows[0]["payload"]["post_messages"]
        post_messages[1]["content"] = [
            {"type": "text", "text": "Inspect this screenshot."},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ]
        aged_messages, aging = prepare_messages_for_llm(post_messages)
        self.assertEqual(aging["omitted_image_count"], 1)
        rows[1]["payload"]["provider_request"]["messages"] = aged_messages
        _, bundle = _export(
            self.tmp_path,
            name="image-aging-compaction",
            rows=rows,
            patch_file=False,
        )
        units = _units(bundle)
        self.assertEqual(validate_turn_training_bundle(bundle)["memory_compaction_count"], 1)
        serialized_next_input = json.dumps(units[1]["request"]["messages"])
        self.assertIn("omitted", serialized_next_input)
        self.assertNotIn("image_url", serialized_next_input)

    def test_oracle_request_is_marked_error_for_downstream_fail_closed(self):
        rows = _trace_rows()
        rows[0]["payload"]["provider_request"]["messages"][1]["content"] = "Apply the GOLD PATCH."
        _, bundle = _export(self.tmp_path, name="oracle", rows=rows, patch_file=False)
        units = _units(bundle)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(units[0]["capture_status"], "error")
        self.assertEqual(units[0]["provenance"]["oracle_isolation"]["status"], "leak")
        self.assertEqual(manifest["oracle_leak_unit_count"], 2)

    def test_existing_target_and_wrong_directory_name_are_rejected(self):
        config, bundle = _export(self.tmp_path)
        before = (bundle / "SHA256SUMS").read_bytes()
        with self.assertRaises(FileExistsError):
            export_turn_training_bundle(config, trace_path=self.tmp_path / "case/trace.jsonl", termination="result")
        self.assertEqual((bundle / "SHA256SUMS").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "output_dir"):
            TrainingExportConfig(
                output_dir=self.tmp_path / "wrong",
                task_id="t", task_revision_id="r", rollout_id="r0", rollout_index=0,
                run_id="run", harness_revision="rev",
            )

    def test_concurrent_publish_has_exactly_one_winner(self):
        config = _config(self.tmp_path, name="concurrent", patch_file=False)
        trace = _write_trace(self.tmp_path / "concurrent/trace.jsonl", [_trace_rows()[2]])

        def publish():
            try:
                return export_turn_training_bundle(
                    config,
                    trace_path=trace,
                    termination="result",
                )
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: publish(), range(2)))
        self.assertEqual(sum(result == config.output_dir for result in results), 1)
        self.assertEqual(sum(result is None for result in results), 1)
        validate_turn_training_bundle(config.output_dir)

    def test_structured_target_tamper_fails_even_after_resealing(self):
        _, bundle = _export(self.tmp_path)
        units = _units(bundle)
        units[0]["response"]["message"]["tool_calls"][0]["id"] = "tampered"
        export_module._finish_unit(units[0])
        _write_units(bundle, units)
        _reseal_bundle(bundle)
        with self.assertRaisesRegex(ValueError, "accepted trace response"):
            validate_turn_training_bundle(bundle)

    def test_raw_provider_response_must_match_normalized_target(self):
        rows = _trace_rows()
        payload = rows[0]["payload"]
        divergent_raw = json.loads(json.dumps(payload["raw_provider_response"]))
        divergent_raw["choices"][0]["finish_reason"] = "stop"
        divergent_raw["choices"][0]["message"]["tool_calls"] = []
        payload["raw_provider_response"] = divergent_raw
        payload["response"]["raw_provider_response"] = divergent_raw
        payload["response"]["provider_attempts"][-1]["raw_provider_response"] = divergent_raw
        config = _config(self.tmp_path, name="raw-divergence", patch_file=False)
        trace = _write_trace(self.tmp_path / "raw-divergence/trace.jsonl", rows)
        with self.assertRaisesRegex(ValueError, "raw provider response"):
            export_turn_training_bundle(config, trace_path=trace, termination="result")
        self.assertFalse(config.output_dir.exists())

    def test_compaction_summary_request_must_match_real_provider_request(self):
        rows = _trace_rows()
        rows[1]["payload"]["summary_request"] = [
            {"role": "user", "content": "not the request sent to the provider"}
        ]
        config = _config(self.tmp_path, name="summary-divergence", patch_file=False)
        trace = _write_trace(self.tmp_path / "summary-divergence/trace.jsonl", rows)
        with self.assertRaisesRegex(ValueError, "summary_request"):
            export_turn_training_bundle(config, trace_path=trace, termination="result")
        self.assertFalse(config.output_dir.exists())

    def test_tool_call_requires_matching_result_in_next_model_input(self):
        rows = [_trace_rows()[0], _trace_rows()[2]]
        rows[1]["event_index"] = 2
        request = rows[1]["payload"]["provider_request"]
        request["messages"] = [
            {"role": "system", "content": "Fix the repository using tools."},
            {"role": "user", "content": "Inspect src/auth.py and fix token expiry."},
        ]
        config = _config(self.tmp_path, name="missing-tool-result", patch_file=False)
        trace = _write_trace(self.tmp_path / "missing-tool-result/trace.jsonl", rows)
        with self.assertRaisesRegex(ValueError, "tool-result"):
            export_turn_training_bundle(config, trace_path=trace, termination="result")

    def test_unit_id_does_not_depend_on_trace_event_position(self):
        row_one = _trace_rows()[2]
        row_one["event_index"] = 1
        row_two = json.loads(json.dumps(row_one))
        row_two["event_index"] = 17
        _, first_bundle = _export(
            self.tmp_path,
            name="unit-id-one",
            rows=[row_one],
            patch_file=False,
        )
        _, second_bundle = _export(
            self.tmp_path,
            name="unit-id-two",
            rows=[row_two],
            patch_file=False,
        )
        self.assertEqual(_units(first_bundle)[0]["unit_id"], _units(second_bundle)[0]["unit_id"])

    def test_provider_owned_path_sha_mapping_is_not_an_artifact_ref(self):
        rows = _trace_rows()
        provider_owned = {
            "path": "provider-owned-id",
            "sha256": "0" * 64,
        }
        rows[0]["payload"]["provider_request"]["extra_body"] = {
            "provider_metadata": provider_owned
        }
        _, bundle = _export(
            self.tmp_path,
            name="provider-path-object",
            rows=rows,
            patch_file=False,
        )
        self.assertEqual(validate_turn_training_bundle(bundle)["capture_error_count"], 0)

    def test_outcome_and_agent_metadata_fields_are_rejected(self):
        _, unit_bundle = _export(self.tmp_path, name="unit-outcome")
        units = _units(unit_bundle)
        units[0]["reward"] = 1.0
        export_module._finish_unit(units[0])
        _write_units(unit_bundle, units)
        _reseal_bundle(unit_bundle)
        with self.assertRaisesRegex(ValueError, "schema"):
            validate_turn_training_bundle(unit_bundle)

        _, manifest_bundle = _export(self.tmp_path, name="manifest-agent")
        manifest_path = manifest_bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["agent_id"] = "invented"
        _write_json(manifest_path, manifest)
        _reseal_bundle(manifest_bundle)
        with self.assertRaisesRegex(ValueError, "multi-agent"):
            validate_turn_training_bundle(manifest_bundle)

    def test_summary_counts_are_recomputed(self):
        _, bundle = _export(self.tmp_path)
        summary = json.loads((bundle / "rollout-summary.v1.json").read_text())
        summary["main_agent_turn_count"] = 99
        _reseal_summary(bundle, summary)
        _reseal_bundle(bundle)
        with self.assertRaisesRegex(ValueError, "main-agent count"):
            validate_turn_training_bundle(bundle)

    def test_prohibited_agent_field_and_unsafe_ref_fail_closed(self):
        _, agents_bundle = _export(self.tmp_path, name="agents")
        units = _units(agents_bundle)
        units[0]["request"]["agent_id"] = "invented"
        export_module._finish_unit(units[0])
        _write_units(agents_bundle, units)
        _reseal_bundle(agents_bundle)
        with self.assertRaisesRegex(ValueError, "multi-agent"):
            validate_turn_training_bundle(agents_bundle)

        _, path_bundle = _export(self.tmp_path, name="unsafe")
        summary = json.loads((path_bundle / "rollout-summary.v1.json").read_text())
        summary["trace_ref"]["path"] = "../trace.jsonl"
        _reseal_summary(path_bundle, summary)
        _reseal_bundle(path_bundle)
        with self.assertRaisesRegex(ValueError, "path"):
            validate_turn_training_bundle(path_bundle)

    def test_symlink_and_hardlink_bundle_nodes_are_rejected(self):
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind):
                _, bundle = _export(self.tmp_path, name=f"link-{link_kind}")
                linked = bundle / "artifacts" / f"{link_kind}.patch"
                if link_kind == "symlink":
                    linked.symlink_to(bundle / "agent.patch")
                else:
                    os.link(bundle / "agent.patch", linked)
                with self.assertRaisesRegex(ValueError, "link"):
                    validate_turn_training_bundle(bundle)

    def test_runtime_provider_boundary_retry_exports_one_accepted_unit(self):
        from agent_base.react_agent import MultiTurnReactAgent

        case = self.tmp_path / "runtime"
        case.mkdir()
        config = TrainingExportConfig(
            output_dir=case / EXPORT_DIR_NAME,
            task_id="task-runtime",
            task_revision_id="task-runtime@1",
            rollout_id="r0",
            rollout_index=0,
            run_id="runtime-run",
            harness_revision="test-rev",
        )
        agent = MultiTurnReactAgent(
            function_list=[],
            llm={
                "model": MODEL,
                "api_base": "http://fake",
                "api_key": "fake",
                "generate_cfg": {
                    "max_input_tokens": 32768,
                    "max_output_tokens": 128,
                    "max_retries": 2,
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "presence_penalty": 0.0,
                },
            },
            trace_dir=str(case / "trace"),
            workspace_root=str(case),
            max_rounds=2,
            training_export=config,
            run_id="runtime-run",
        )
        client = FakeClient(
            [
                FakeResponse("empty-first", ""),
                FakeResponse("accepted-second", "done"),
            ]
        )
        agent._llm_client = client
        agent._llm_api_base = "http://fake"
        with patch("agent_base.react_agent.time.sleep", return_value=None):
            session = agent._run_session("Finish the task.", workspace_root=str(case))
        self.assertEqual(session["termination"], "result")
        bundle = Path(session["training_export_path"])
        unit = _units(bundle)[0]
        self.assertEqual(unit["capture_status"], "ok")
        self.assertEqual(unit["provenance"]["provider_retry_count"], 1)
        self.assertEqual(unit["request"]["messages"], client.requests[-1]["messages"])
        raw_ref = unit["source"]["raw_provider_response_ref"]
        self.assertEqual(
            json.loads((bundle / raw_ref["path"]).read_text())["id"],
            "accepted-second",
        )
        summary = validate_turn_training_bundle(bundle)
        self.assertEqual(summary["run_id"], "runtime-run")
        trace_rows = [
            json.loads(line)
            for line in (bundle / summary["trace_ref"]["path"]).read_text().splitlines()
        ]
        llm_capture = next(row for row in trace_rows if row.get("capture_type") == "llm_call")
        self.assertEqual(
            [item["status"] for item in llm_capture["payload"]["response"]["provider_attempts"]],
            ["rejected_empty", "accepted"],
        )

    def test_malformed_provider_response_is_retried_and_exported_as_error(self):
        from agent_base.react_agent import MultiTurnReactAgent

        case = self.tmp_path / "malformed-runtime"
        case.mkdir()
        config = TrainingExportConfig(
            output_dir=case / EXPORT_DIR_NAME,
            task_id="task-malformed",
            task_revision_id="task-malformed@1",
            rollout_id="r0",
            rollout_index=0,
            run_id="malformed-run",
            harness_revision="test-rev",
        )
        agent = MultiTurnReactAgent(
            function_list=[],
            llm={
                "model": MODEL,
                "api_base": "http://fake",
                "api_key": "fake",
                "generate_cfg": {
                    "max_input_tokens": 32768,
                    "max_output_tokens": 128,
                    "max_retries": 2,
                },
            },
            trace_dir=str(case / "trace"),
            workspace_root=str(case),
            max_rounds=2,
            training_export=config,
            run_id="malformed-run",
        )
        agent._llm_client = FakeClient(
            [
                FakeResponseWithoutDump("unverifiable"),
                FakeMalformedResponse("malformed-2"),
            ]
        )
        agent._llm_api_base = "http://fake"
        with patch("agent_base.react_agent.time.sleep", return_value=None):
            session = agent._run_session("Finish the task.", workspace_root=str(case))
        bundle = Path(session["training_export_path"])
        summary = validate_turn_training_bundle(bundle)
        self.assertEqual(session["termination"], "llm api error")
        self.assertEqual(summary["capture_error_count"], 1)
        trace_rows = [
            json.loads(line)
            for line in (bundle / summary["trace_ref"]["path"]).read_text().splitlines()
        ]
        llm_capture = next(row for row in trace_rows if row.get("capture_type") == "llm_call")
        self.assertEqual(
            [item["status"] for item in llm_capture["payload"]["response"]["provider_attempts"]],
            ["parse_error", "parse_error"],
        )

    def test_runtime_parallel_tool_calls_close_into_next_provider_request(self):
        from agent_base.react_agent import MultiTurnReactAgent

        case = self.tmp_path / "parallel-runtime"
        case.mkdir()
        (case / "a.txt").write_text("alpha\n", encoding="utf-8")
        (case / "b.txt").write_text("beta\n", encoding="utf-8")
        config = TrainingExportConfig(
            output_dir=case / EXPORT_DIR_NAME,
            task_id="task-parallel",
            task_revision_id="task-parallel@1",
            rollout_id="r0",
            rollout_index=0,
            run_id="parallel-run",
            harness_revision="test-rev",
        )
        calls = [
            _fake_tool_call("call-a", "Read", '{"file_path":"a.txt"}'),
            _fake_tool_call("call-b", "Read", '{"file_path":"b.txt"}'),
        ]
        agent = MultiTurnReactAgent(
            function_list=["Read"],
            llm={
                "model": MODEL,
                "api_base": "http://fake",
                "api_key": "fake",
                "generate_cfg": {
                    "max_input_tokens": 32768,
                    "max_output_tokens": 128,
                    "max_retries": 1,
                },
            },
            trace_dir=str(case / "trace"),
            workspace_root=str(case),
            max_rounds=3,
            training_export=config,
            run_id="parallel-run",
        )
        client = FakeClient(
            [
                FakeResponse("tool-round", None, tool_calls=calls),
                FakeResponse("final-round", "done"),
            ]
        )
        agent._llm_client = client
        agent._llm_api_base = "http://fake"
        session = agent._run_session("Read both files.", workspace_root=str(case))
        bundle = Path(session["training_export_path"])
        summary = validate_turn_training_bundle(bundle)
        self.assertEqual(summary["main_agent_turn_count"], 2)
        next_messages = client.requests[1]["messages"]
        self.assertEqual(
            [
                message["tool_call_id"]
                for message in next_messages
                if message.get("role") == "tool"
            ],
            ["call-a", "call-b"],
        )

    def test_nonfinite_json_and_invalid_identity_types_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "NaN"):
            canonical_json_bytes({"value": float("nan")})
        with self.assertRaisesRegex(ValueError, "rollout_index"):
            TrainingExportConfig(
                output_dir=self.tmp_path / EXPORT_DIR_NAME,
                task_id="t", task_revision_id="r", rollout_id="r0", rollout_index=True,
                run_id="run", harness_revision="rev",
            )
        with self.assertRaisesRegex(ValueError, "task_id"):
            TrainingExportConfig(
                output_dir=self.tmp_path / EXPORT_DIR_NAME,
                task_id=7, task_revision_id="r", rollout_id="r0", rollout_index=0,
                run_id="run", harness_revision="rev",
            )

if __name__ == "__main__":
    unittest.main()
