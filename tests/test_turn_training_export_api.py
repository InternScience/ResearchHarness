import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import openai_server
from api.openai_server import (
    OpenAICompatError,
    REQUEST_TRAINING_EXPORT_FIELD,
    ServerConfig,
    request_training_export,
    run_chat_completion,
)
from agent_base.training_export import EXPORT_DIR_NAME


def _training_payload(**overrides):
    value = {
        "task_id": "task-api",
        "task_revision_id": "task-api@1",
        "rollout_id": "r0",
        "rollout_index": 0,
        "harness_revision": "test-rev",
    }
    value.update(overrides)
    return value


class FakeAPIAgent:
    seen = []

    def __init__(
        self,
        function_list,
        llm,
        trace_dir,
        role_prompt=None,
        training_export=None,
        run_id=None,
    ):
        self.trace_dir = Path(trace_dir)
        self.training_export = training_export
        self.run_id = run_id
        self.__class__.seen.append(self)

    def _run_session(self, prompt, workspace_root=None, initial_content_parts=None):
        return {
            "result_text": "done",
            "termination": "result",
            "trace_path": str(self.trace_dir / "trace.jsonl"),
            "training_export_path": str(self.training_export.output_dir)
            if self.training_export is not None
            else "",
        }


def _fake_llm_config(model_name=None, extra_body=None, omit_generate_params=None):
    return {
        "model": str(model_name or "fake-model"),
        "api_key": "fake",
        "api_base": "http://fake.invalid/v1",
        "generate_cfg": {
            "max_input_tokens": 32768,
            "max_output_tokens": 128,
            "max_retries": 1,
        },
    }


class TrainingExportAPITests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        FakeAPIAgent.seen.clear()

    def tearDown(self):
        self._temp.cleanup()

    def test_request_parser_freezes_outer_run_identity_and_fixed_directory(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        patch_path = workspace / "agent.patch"
        patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
        config = request_training_export(
            {
                REQUEST_TRAINING_EXPORT_FIELD: _training_payload(
                    final_patch_path="agent.patch"
                )
            },
            run_root=self.root / "run",
            workspace_root=workspace,
            run_id="run-api-1",
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.run_id, "run-api-1")
        self.assertEqual(config.output_dir, self.root / "run" / EXPORT_DIR_NAME)
        self.assertEqual(config.final_patch_path, workspace / "agent.patch")

    def test_parser_rejects_unknown_fields_and_bool_rollout_index(self):
        for value in (
            _training_payload(unknown=True),
            _training_payload(rollout_index=True),
            _training_payload(task_id=7),
        ):
            with self.subTest(value=value):
                with self.assertRaises(OpenAICompatError):
                    request_training_export(
                        {REQUEST_TRAINING_EXPORT_FIELD: value},
                        run_root=self.root / "run",
                        workspace_root=self.root,
                        run_id="run-api-1",
                    )

    def test_training_export_with_wrapper_fails_before_any_model_call(self):
        payload = {
            "model": "RH",
            "messages": [{"role": "user", "content": "hello"}],
            REQUEST_TRAINING_EXPORT_FIELD: _training_payload(),
        }
        with self.assertRaisesRegex(OpenAICompatError, "cannot be combined"):
            run_chat_completion(
                payload,
                ServerConfig(api_runs_dir=self.root / "runs", input_wrapper=True),
            )
        self.assertEqual(FakeAPIAgent.seen, [])

    def test_api_passes_same_run_id_to_agent_trace_and_export(self):
        previous_agent = openai_server.MultiTurnReactAgent
        previous_config = openai_server.default_llm_config
        openai_server.MultiTurnReactAgent = FakeAPIAgent
        openai_server.default_llm_config = _fake_llm_config
        try:
            response = run_chat_completion(
                {
                    "model": "RH",
                    "messages": [{"role": "user", "content": "hello"}],
                    REQUEST_TRAINING_EXPORT_FIELD: _training_payload(),
                },
                ServerConfig(api_runs_dir=self.root / "runs"),
            )
        finally:
            openai_server.MultiTurnReactAgent = previous_agent
            openai_server.default_llm_config = previous_config
        self.assertEqual(response["choices"][0]["message"]["content"], "done")
        self.assertEqual(len(FakeAPIAgent.seen), 1)
        agent = FakeAPIAgent.seen[0]
        self.assertEqual(agent.run_id, agent.training_export.run_id)
        self.assertEqual(agent.training_export.output_dir.name, EXPORT_DIR_NAME)
        metadata = response["researchharness"]
        self.assertEqual(metadata["run_id"], agent.run_id)
        self.assertEqual(
            metadata["training_export_path"],
            str(agent.training_export.output_dir),
        )


if __name__ == "__main__":
    unittest.main()
