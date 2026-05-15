# QA / VQA Benchmarks

This directory documents the lightweight ResearchHarness contract for
question-answering benchmarks, including plain-text QA and multimodal VQA-style
tasks.

## Recommended Server Command

For ordinary QA/VQA benchmark runs, start the OpenAI-compatible synchronous API
server with the QA benchmark role overlay and no wrappers:

```bash
python3 run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file ./benchmarks/QA/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

For large benchmark batches, raise `--max-concurrent-runs` when local resources
and backend API quota allow more simultaneous agent runs.

For strict-format QA benchmarks, wrapper passes are optional and should be
enabled only when they match the benchmark contract:

```bash
python3 run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file ./benchmarks/QA/role_prompt.md \
  --input-wrapper \
  --output-wrapper
```

In practice, `--output-wrapper` is often more useful than `--input-wrapper`
because it can format the final answer without rewriting the original question.
Use `--input-wrapper` only when input normalization is known to be safe for the
benchmark.

By default, each request creates a fresh run directory:

```text
./api_runs/
└── run_YYYYMMDD_HHMMSS_<random>/
    ├── agent_workspace/          # visible to the agent
    │   └── inputs/
    │       └── images/           # user-provided images, when present
    └── agent_trace/              # server-side trace and session state
        ├── api_trace.jsonl
        ├── trace_*.jsonl
        └── _session_state.json
```

## OpenAI Test Example

Benchmark runners may pass `workspace-root` in the OpenAI request body when a
case should run inside an already prepared workspace:

```python
from pathlib import Path

from openai import OpenAI


workspace = Path("./workspace/qa_example").resolve()
workspace.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[{"role": "user", "content": "Answer the question."}],
    extra_body={"workspace-root": str(workspace)},
)

print(response.choices[0].message.content)
```

If `workspace-root` is absent, relative, or not an existing directory, RH falls
back to the default per-request `agent_workspace/`. The `agent_trace/` directory
is always created under `--api-runs-dir/run_.../` for auditability. For custom
workspaces, uploaded images are saved under `inputs/images/<run_id>/` inside
that workspace. Use exactly `workspace-root`; synonymous request fields such as
`workspace_root` are rejected.

The input and output LLM wrappers are disabled by default in normal deployment
mode:

- `--input-wrapper` / `--no-input-wrapper` controls the input normalization pass.
- `--output-wrapper` / `--no-output-wrapper` controls the final answer formatting pass.

To return the agent's direct final text, use the default QA deployment command
without wrapper flags. Advanced deployments can manually combine role prompts
and wrapper flags as needed.

External benchmark runners can then use the regular OpenAI SDK with:

```python
from openai import OpenAI

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[{"role": "user", "content": "Answer the question."}],
)

answer = response.choices[0].message.content
```

## Multimodal Input

For image benchmarks, send OpenAI-style content parts. The first API version
supports one or more `data:image/...;base64,...` URLs in the same request.

```python
response = client.chat.completions.create(
    model="RH--gpt-5.5",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is shown? Return JSON with key answer."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ],
)
```

Use `RH` or omit `model` for the server's default `MODEL_NAME`. Use
`RH--<llm-model-name>` with exactly two hyphens for a per-request backend model
override. Direct model names such as `gpt-5.5` are rejected so benchmark runners
do not accidentally confuse the ResearchHarness endpoint label with the backend
LLM selection.

The API saves each submitted image inside the selected workspace, passes the
image content to the first ResearchHarness model call when the backend model
supports image parts, and includes each saved path in the agent-visible text.
With the default workspace this is `agent_workspace/inputs/images/`; with a
custom `workspace-root`, this is `inputs/images/<run_id>/` inside that
workspace.

The returned answer should be self-contained for a remote evaluator. Workspace
files may support the run, but the response should not only say to consult
`answer.md`, `report.md`, an image file, or another local artifact.

## Scope

- The endpoint is synchronous and returns one final text answer.
- Each request gets a separate workspace subdirectory.
- QA benchmark mode can use the ResearchHarness agent directly, or optionally
  add input/output wrappers when the benchmark contract benefits from them.
- Streaming, async run status, artifact download, and remote image fetching are
  intentionally out of scope for this minimal QA contract.
