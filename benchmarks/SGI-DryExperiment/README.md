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

## OpenAI Test Example

The example below uses the first real `SGI-DryExperiment` test item and appends
the same function-completion output requirement used by the official SGI-Bench
evaluation script.

```python
from pathlib import Path

from datasets import load_dataset
from openai import OpenAI


OUTPUT_REQUIREMENTS = """
Output the completed function enclosed within <answer> and </answer> tags.

Example 1:
<answer>
def hello():
    print("Hello")
</answer>

Example 2:
<answer>
def add(a, b):
    return a+b

def minus(a, b):
    return a-b
</answer>

"""

dataset = load_dataset("InternScience/SGI-DryExperiment", split="test")
row = dataset[0]
prompt = row["question"] + OUTPUT_REQUIREMENTS

workspace = Path("./workspace/sgi_dryexperiment_example").resolve()
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

- The dataset prompt already contains the full code context and function names.
- Input rewriting can drop or distort code blocks, signatures, or unit-test
  evidence.
- Output rewriting can add prose or alter code formatting.
- The benchmark-specific role prompt should guide the agent to create local
  files, debug the code, and then return the completed functions directly.

Use `RH` or `RH--<llm-model-name>` in the OpenAI SDK request as usual.
