# QA / VQA Benchmarks

This directory documents the lightweight ResearchHarness contract for
question-answering benchmarks, including plain-text QA and multimodal VQA-style
tasks.

The recommended integration is the OpenAI-compatible synchronous API server:

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs
```

For QA/VQA benchmark runs, optionally add this benchmark role overlay:

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs \
  --role-prompt-file /abs/path/to/ResearchHarness/benchmarks/QA/role_prompt.md
```

Each request creates a fresh run directory:

```text
./api_runs/
`-- run_YYYYMMDD_HHMMSS_<random>/
    |-- agent_workspace/          # visible to the agent
    |   `-- inputs/
    |       `-- images/           # user-provided images, when present
    `-- agent_trace/              # server-side trace and session state
        |-- api_trace.jsonl
        |-- trace_*.jsonl
        `-- _session_state.json
```

The input and output LLM wrappers are enabled by default:

- `--input-wrapper` / `--no-input-wrapper` controls the input normalization pass.
- `--output-wrapper` / `--no-output-wrapper` controls the final answer formatting pass.

Strict-format benchmarks should usually keep both wrappers enabled. To return
the agent's direct final text instead, run:

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs \
  --no-input-wrapper \
  --no-output-wrapper
```

External benchmark runners can then use the regular OpenAI SDK with:

```python
from openai import OpenAI

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="researchharness",
    messages=[{"role": "user", "content": "Answer the question."}],
)

answer = response.choices[0].message.content
```

## Multimodal Input

For image benchmarks, send OpenAI-style content parts. The first API version
supports one or more `data:image/...;base64,...` URLs in the same request.

```python
response = client.chat.completions.create(
    model="researchharness",
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

The API saves each submitted image under `agent_workspace/inputs/images/`,
passes the image content to the first ResearchHarness model call when the
backend model supports image parts, and includes each saved path in the
agent-visible text.

The returned answer should be self-contained for a remote evaluator. Workspace
files may support the run, but the response should not only say to consult
`answer.md`, `report.md`, an image file, or another local artifact.

## Scope

- The endpoint is synchronous and returns one final text answer.
- Each request gets a separate workspace subdirectory.
- The API uses an input wrapper, the ResearchHarness agent, and an output
  wrapper so strict benchmark output formats do not destabilize the agent loop.
- Streaming, async run status, artifact download, and remote image fetching are
  intentionally out of scope for this minimal QA contract.
