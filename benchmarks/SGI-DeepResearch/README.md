# SGI-DeepResearch

This directory contains the ResearchHarness benchmark role overlay for
[SGI-DeepResearch](https://huggingface.co/datasets/InternScience/SGI-DeepResearch).

SGI-DeepResearch is a scientific deep-research QA benchmark. It is not a
generic QA task, so the generic QA wrappers are not recommended.

## Recommended Server Command

```bash
python3 run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file ./benchmarks/SGI-DeepResearch/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

## OpenAI Test Example

The example below uses the first real `SGI-DeepResearch` test item and appends
the same output requirement shape used by the official SGI-Bench evaluation
script.

```python
from pathlib import Path

from datasets import load_dataset
from openai import OpenAI


OUTPUT_REQUIREMENTS = """
You can reason step by step before giving the final answer. The final answer should be enclosed by <answer> and </answer>.

Example:
Step 1. ...
Step 2. ...
...
<answer>1.00</answer>
"""

dataset = load_dataset("InternScience/SGI-DeepResearch", split="test")
row = dataset[0]
prompt = row["question"] + OUTPUT_REQUIREMENTS

workspace = Path("./workspace/sgi_deepresearch_example").resolve()
workspace.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[{"role": "user", "content": prompt}],
    extra_body={"workspace-root": str(workspace)},
)

print(response.choices[0].message.content)
```

## Rationale

- The benchmark evaluates both the reasoning process and the final answer.
- The official evaluation expects the final answer to be recoverable from an
  `<answer>...</answer>` span.
- Input/output rewriting can remove cited evidence, intermediate calculations,
  or the final answer span.
- The benchmark-specific role prompt should guide the agent to search relevant
  literature, reason, calculate when needed, and then return a complete answer
  directly.

Use `RH` or `RH--<llm-model-name>` in the OpenAI SDK request as usual.
