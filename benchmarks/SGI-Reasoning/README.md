# SGI-Reasoning

This directory contains the ResearchHarness benchmark role overlay for
[SGI-Reasoning](https://huggingface.co/datasets/InternScience/SGI-Reasoning).

SGI-Reasoning is a multimodal scientific reasoning benchmark. It is not a
generic VQA task, so the generic QA wrappers are not recommended.

## Recommended Server Command

```bash
python3 run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file ./benchmarks/SGI-Reasoning/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

## OpenAI Test Example

The example below uses the first real `SGI-Reasoning` test item. The prompt
matches the official SGI-Bench multiple-choice instruction shape, and the image
is saved locally at
[`example_imgs/SGI_Reasoning_0000_0.png`](example_imgs/SGI_Reasoning_0000_0.png).

![SGI-Reasoning example image](example_imgs/SGI_Reasoning_0000_0.png)

```python
import base64
from pathlib import Path

from openai import OpenAI


CONTEXT = """
Please solve the following multiple-choice question step-by-step. Each question is provided with several options labeled A, B, C, D, E, etc. Carefully analyze the question and each option, reason step-by-step, then select the single most correct option.

Your final output **must** include both **the reasoning** and **the final answer**. The final answer must meet two core requirements:
1. It consists solely of the corresponding letter of the correct option (e.g., A, B, C, D, E, etc.);
2. This letter is enclosed in the \\boxed{} format. Example: \\boxed{A}
""".strip()

QUESTION = "The first image shows the postoperative detection steps of iREX biosensor. Compared to traditional \"lateral flow\" designs, its \"vertical flow\" design may bring significant spatial and structural advantages when detecting multiple extracellular vesicle proteins simultaneously. If a patient's serum extracellular vesicle SERS signal is 8.5 miles away from the Mahalanobis distance of MDA-MB-231 cell line extracellular vesicles before surgery, the distance becomes 2.3 miles after surgery. Please calculate the percentage change in similarity between the patient's postoperative exosome characteristics and the MDA-MB-231 cell line compared to preoperative characteristics, and keep the results as integers."
OPTIONS = ["92", "73", "85", "120", "135", "66", "79", "150", "270", "230"]

prompt = CONTEXT + "\n\nQuestion:\n" + QUESTION + "\n\nOptions:\n"
for index, option in enumerate(OPTIONS):
    prompt += f"{chr(ord('A') + index)}. {option}\n"

image_path = Path("benchmarks/SGI-Reasoning/example_imgs/SGI_Reasoning_0000_0.png")
image_bytes = image_path.read_bytes()
data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

workspace = Path("./workspace/sgi_reasoning_example").resolve()
workspace.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ],
    extra_body={"workspace-root": str(workspace)},
)

print(response.choices[0].message.content)
```

## Rationale

- The benchmark evaluates both the multiple-choice answer and the reasoning
  validity.
- The official answer extraction expects the final option letter in
  `\boxed{...}` format.
- Input/output rewriting can drop images, options, or the exact boxed answer.
- The benchmark-specific role prompt should guide the agent to inspect images,
  use image processing or literature search when useful, and return complete
  reasoning plus a boxed option.

Use `RH` or `RH--<llm-model-name>` in the OpenAI SDK request as usual.
