import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_base.react_agent import _parse_cli_args
from agent_base.training_export import EXPORT_DIR_NAME
from researchharness.runtime import _resolve_training_export


class TrainingExportEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_cli_builds_one_frozen_config_and_sibling_trace(self):
        output = self.root / "r0" / EXPORT_DIR_NAME
        parsed = _parse_cli_args(
            [
                "fix",
                "auth",
                "--training-export-dir",
                str(output),
                "--task-id",
                "task-auth",
                "--task-revision-id",
                "task-auth@1",
                "--rollout-id",
                "r0",
                "--rollout-index",
                "0",
                "--run-id",
                "run-cli",
                "--harness-revision",
                "test-rev",
            ]
        )
        self.assertEqual(parsed[0], "fix auth")
        self.assertEqual(parsed[1], str(output.parent / "trace"))
        config = parsed[-1]
        self.assertEqual(config.output_dir, output)
        self.assertEqual(config.run_id, "run-cli")

    def test_cli_partial_configuration_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "requires"):
            _parse_cli_args(
                [
                    "fix",
                    "--training-export-dir",
                    str(self.root / EXPORT_DIR_NAME),
                ]
            )

    def test_python_resolver_rejects_bool_index_and_preserves_no_export(self):
        with self.assertRaisesRegex(ValueError, "rollout_index"):
            _resolve_training_export(
                training_export_dir=self.root / EXPORT_DIR_NAME,
                task_id="task",
                task_revision_id="task@1",
                rollout_id="r0",
                rollout_index=True,
                run_id="run-python",
                harness_revision="rev",
                scaffold_version="",
                tool_protocol_version="native-v1",
                tokenizer_revision=None,
                final_patch_path=None,
                trace_dir=None,
            )
        config, trace_dir, run_id = _resolve_training_export(
            training_export_dir=None,
            task_id=None,
            task_revision_id=None,
            rollout_id=None,
            rollout_index=None,
            run_id="ordinary-run",
            harness_revision=None,
            scaffold_version="",
            tool_protocol_version="native-v1",
            tokenizer_revision=None,
            final_patch_path=None,
            trace_dir="trace",
        )
        self.assertIsNone(config)
        self.assertEqual(trace_dir, "trace")
        self.assertEqual(run_id, "ordinary-run")


if __name__ == "__main__":
    unittest.main()
