# Turn-level training export V1

ResearchHarness can publish one immutable, self-validating training bundle for
one completed rollout. Each real provider call becomes one training unit, so a
downstream data job does not need to reconstruct model rounds from UI events.

V1 has exactly two unit types: `main_agent_turn` and `memory_compaction`.
Compaction is an isolated semantic model call, not a sub-agent.

## Bundle

The output path must end in `turn-training-export-v1`:

```text
turn-training-export-v1/
  turn-training-units.v1.jsonl
  rollout-summary.v1.json
  agent.patch                  # only when explicitly supplied
  artifacts/trace/
  artifacts/provider/
  artifacts/compaction/
  manifest.json
  SHA256SUMS
```

The Harness writes a temporary sibling, validates it, then publishes it with
no-replace semantics. Existing exports are never overwritten. Exact final
provider kwargs and full raw provider responses are retained as hashed
artifacts. Failed calls remain auditable with `capture_status="error"` and are
not SFT candidates.

The normalized target is derived and revalidated against the raw provider
response. V1 applies one explicit canonical transform: an assistant
`message.content` of JSON `null` becomes the empty string `""`; all other
content, reasoning content, finish reason, usage, tool-call order, IDs, names,
and argument strings must match the raw first choice. Every retry attempt is
kept in the trace, and the final attempt must close against the exported
provider request and raw response.

For tool rounds, each non-truncated tool-call ID must close against its tool
result and, when another model call occurs, that result must be present in the
next real provider input (directly or through a validated compaction boundary).
For compaction, `summary_request` must exactly equal the messages sent to the
provider, and successful `post_messages` must match the next real model input
after the documented image-aging transform.

The Harness does not write `resolved`, reward, advantage, or verifier outcome.
Downstream code joins those outcomes by
`task_id + task_revision_id + rollout_id`.

## CLI

```bash
python -m agent_base.react_agent \
  "Fix the authentication regression." \
  --workspace-root /work/repo \
  --training-export-dir /outputs/r0/turn-training-export-v1 \
  --task-id auth-regression \
  --task-revision-id auth-regression@base-abc123 \
  --rollout-id r0 \
  --rollout-index 0 \
  --run-id run-auth-r0 \
  --harness-revision 72e8a44737aadabc \
  --final-patch-path /outputs/r0/agent.patch
```

`--run-id` is generated when omitted. `--trace-dir` defaults to a sibling
`trace/`. The patch is optional and never guessed. V1 export cannot be combined
with CLI chat mode because one immutable config represents one rollout.

## Python API

`create_agent(...)` and `run_agent(...)` accept the same identity fields:

```python
from researchharness import run_agent
result = run_agent(
    "Fix the authentication regression.",
    workspace_root="/work/repo",
    training_export_dir="/outputs/r0/turn-training-export-v1",
    task_id="auth-regression",
    task_revision_id="auth-regression@base-abc123",
    rollout_id="r0",
    rollout_index=0,
    run_id="run-auth-r0",
    harness_revision="72e8a44737aadabc",
    final_patch_path="/outputs/r0/agent.patch",
)
```

## OpenAI-compatible API

Add a `training-export` object to the request:

```json
{
  "model": "RH",
  "messages": [{"role": "user", "content": "Fix the auth regression."}],
  "training-export": {
    "task_id": "auth-regression",
    "task_revision_id": "auth-regression@base-abc123",
    "rollout_id": "r0",
    "rollout_index": 0,
    "harness_revision": "72e8a44737aadabc",
    "final_patch_path": "agent.patch"
  }
}
```

The API uses one outer `run_id` for the agent trace and export, and writes the
bundle under `<api-runs-dir>/<run_id>/turn-training-export-v1`. API patch paths
are relative to the selected workspace. Training export fails closed when input
or output LLM wrappers are enabled because wrapper calls are outside V1's two
unit types.

## Validation

`validate_turn_training_bundle(path)` checks trace/order closure, exact request
and response matching, structured tool calls, compaction pre/post and next-input
continuity, frozen identity, canonical hashes and refs, oracle leakage,
multi-agent fields, path traversal, symlinks, hardlinks, and overwrite safety.

Only units with `capture_status="ok"` are SFT candidates. Selection policy is a
downstream responsibility.
