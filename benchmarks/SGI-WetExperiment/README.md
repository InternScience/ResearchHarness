# SGI-WetExperiment

This directory contains the ResearchHarness benchmark role overlay for
[SGI-WetExperiment](https://huggingface.co/datasets/InternScience/SGI-WetExperiment).

SGI-WetExperiment is a strict experimental-process formatting benchmark. It is
not a generic QA/VQA task, so the generic QA wrappers are not recommended.

## Recommended Server Command

```bash
python3 run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file ./benchmarks/SGI-WetExperiment/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

## OpenAI Test Example

The example below uses the first real `SGI-WetExperiment` test item and appends
the same action-sequence output requirement used by the official SGI-Bench
evaluation script.

```python
from pathlib import Path

from datasets import load_dataset
from openai import OpenAI


OUTPUT_REQUIREMENTS = """
The final answer should be enclosed by <answer> and </answer>.

Example:
<answer>
dataset = <Load dataset>(
    source="imagenet"
)

model_init = <Initialize model>(
    model_type="CNN"
)

model_trained = <Train model>(
    model=model_init,
    data=dataset
)

metrics = <Calculate metrics>(
    model=model_trained,
    data=dataset
)
</answer>
"""

dataset = load_dataset("InternScience/SGI-WetExperiment", split="test")
row = dataset[0]
prompt = row["question"] + OUTPUT_REQUIREMENTS

workspace = Path("./workspace/sgi_wetexperiment_example").resolve()
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

- The scorer expects a strict action-call text format.
- Output rewriting can collapse required multi-line calls into one-line calls.
- Input rewriting can lose action-pool entries or move them away from the model
  context.
- The benchmark-specific role prompt should guide the agent to draft, validate,
  and return the final action sequence directly.

## Local Format Validator

The role prompt asks the agent to validate against the same broad shape used by
the benchmark parser:

```python
import re


def parse_experiment_steps(text):
    step_pattern = r'(\w+)\s*=\s*<([^>]+)>\(\s*([\s\S]*?)(?=\n\s*\)\s*$)'
    param_pattern = r'^\s*(\w+)\s*=\s*(.*?)\s*(?:,)?\s*$'
    steps = []

    for match in re.finditer(step_pattern, text, re.MULTILINE):
        output_var = match.group(1).strip()
        action_name = match.group(2).strip()
        params = match.group(3).strip()

        param_dict = {}
        param_lines = [line.strip() for line in params.split('\n') if line.strip() and line.strip() != ')']
        for line in param_lines:
            param_match = re.match(param_pattern, line)
            if param_match:
                key = param_match.group(1)
                value = param_match.group(2).strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                param_dict[key] = value

        steps.append({"action": action_name, "input": param_dict, "output": output_var})

    return steps
```

This validator is documentation for the expected output shape. The actual
benchmark runner remains responsible for scoring.
