# [ResearchClawBench](https://github.com/InternScience/ResearchClawBench)

This directory contains the tracked files needed to document how `ResearchHarness`
should be integrated into
[ResearchClawBench](https://github.com/InternScience/ResearchClawBench).

ResearchHarness is intended to serve here as a **general and fair execution
substrate** for tool-using LLM evaluation, while
[ResearchClawBench](https://github.com/InternScience/ResearchClawBench) remains in
charge of task construction, hidden-answer isolation, and scoring.

## Recommended [`agents.json`](https://github.com/InternScience/ResearchClawBench/blob/main/evaluation/agents.json) Entry

Use a single direct command that launches the thin top-level ResearchHarness
entrypoint.

```json
{
  "researchharness": {
    "label": "ResearchHarness",
    "icon": "H",
    "logo": "/static/logos/rh.svg",
    "cmd": "python3 /abs/path/to/ResearchHarness/run_agent.py <PROMPT> --workspace-root <WORKSPACE> --role-prompt-file /abs/path/to/ResearchHarness/benchmarks/ResearchClawBench/role_prompt.md"
  }
}
```

## API Server Deployment

ResearchHarness can also be exposed through its OpenAI-compatible API server
and called by a benchmark runner. Start the server with the ResearchClawBench
role prompt:

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file /abs/path/to/ResearchHarness/benchmarks/ResearchClawBench/role_prompt.md
```

Then send each RCB case through the OpenAI SDK and pass the prepared RCB
workspace as `workspace-root`:

```python
from openai import OpenAI

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[{"role": "user", "content": rcb_prompt}],
    extra_body={"workspace-root": "/abs/path/to/rcb/workspace"},
)

print(response.choices[0].message.content)
```

Use `RH--<llm-model-name>` instead of `RH` when the request should override the
server's default backend model. The API server keeps `agent_trace/` under
`--api-runs-dir/run_.../`, while the agent works inside the supplied
`workspace-root`.

## Why This Shape

- [ResearchClawBench](https://github.com/InternScience/ResearchClawBench)
  already prepares the workspace, writes `INSTRUCTIONS.md`,
  and isolates hidden checklist data.
- `ResearchHarness` should only execute the agent through a stable harness
  interface.
- The command stays a simple one-line `agents.json` entry. The entrypoint
  automatically selects the lightweight adapter in
  `benchmarks/ResearchClawBench/adapter.py` when this benchmark role prompt is
  used.

## Notes

- Replace `/abs/path/to/ResearchHarness/` with the real local checkout path.
- The command should stay one-line and non-interactive.
- Optional extra tools can be added directly to the same command. For example,
  add `--extra-tool str_replace_editor` if the benchmark configuration should
  expose the text editing compatibility tool. `--extra-tool` may
  be passed multiple times when more optional tools exist.
- By default, no ResearchHarness trace is saved. If you want to save traces
  during evaluation, create a separate trace directory and add
  `--trace-dir /path/to/trace-dir` directly to the command. Do not use
  `<WORKSPACE>` as the trace directory, because that exposes `trace_*.jsonl` and
  `_session_state.json` to the evaluated agent.
- The adapter prevents premature termination on long tasks by refusing to accept
  plain-text completion before `report/report.md` exists in the workspace.
- The adapter excludes `AskUser`; RCB runs must remain fully non-interactive.
- Any local batch helpers or ad hoc benchmark scripts should remain untracked
  and live outside the formal integration contract.
