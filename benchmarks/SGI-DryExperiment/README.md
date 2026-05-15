# SGI-DryExperiment

This directory contains the ResearchHarness benchmark role overlay for
[SGI-DryExperiment](https://huggingface.co/datasets/InternScience/SGI-DryExperiment).

SGI-DryExperiment is a strict code-completion benchmark. It is not a generic
QA/VQA task, so the generic QA wrappers are not recommended.

## Recommended Server Command

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file /abs/path/to/ResearchHarness/benchmarks/SGI-DryExperiment/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

## Rationale

- The dataset prompt already contains the full code context and function names.
- Input rewriting can drop or distort code blocks, signatures, or unit-test
  evidence.
- Output rewriting can add prose or alter code formatting.
- The benchmark-specific role prompt should guide the agent to create local
  files, debug the code, and then return the completed functions directly.

Use `RH` or `RH--<llm-model-name>` in the OpenAI SDK request as usual.
