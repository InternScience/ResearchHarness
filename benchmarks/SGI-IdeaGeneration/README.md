# SGI-IdeaGeneration

This directory contains the ResearchHarness benchmark role overlay for
[SGI-IdeaGeneration](https://huggingface.co/datasets/InternScience/SGI-IdeaGeneration).

SGI-IdeaGeneration is a scientific proposal-generation benchmark. It is not a
generic summarization task, so the generic QA wrappers are not recommended.

## Recommended Server Command

```bash
python3 /abs/path/to/ResearchHarness/run_server.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file /abs/path/to/ResearchHarness/benchmarks/SGI-IdeaGeneration/role_prompt.md \
  --no-input-wrapper \
  --no-output-wrapper
```

## OpenAI Test Example

The example below uses the first real `SGI-IdeaGeneration` test item and
appends the same proposal JSON example shape used by the official SGI-Bench
evaluation script.

````python
import json
from pathlib import Path

from datasets import load_dataset
from openai import OpenAI


EXAMPLE = {
    "Idea": "We propose an adaptive optimization framework based on a dynamic feature interaction network. This framework captures feature correlations through a hierarchical attention mechanism and combines it with a data distribution-aware dynamic weight adjustment strategy to improve the model's adaptability to heterogeneous data while ensuring computational efficiency.",
    "ImplementationSteps": {
        "1": "Data preprocessing: missing value filling, outlier handling, feature normalization and type conversion, and building a basic feature set",
        "2": "Feature engineering: generating statistically derived features, time series features, and cross-features, and building a feature candidate pool",
        "3": "Model architecture design: building a basic network module, integrating a hierarchical attention mechanism with a dynamic interaction layer",
        "4": "Dynamic weight mechanism implementation: designing a data distribution-aware weight adjustment function and embedding it into the network's intermediate layers",
        "5": "Model training and tuning: adopting a phased training strategy, using grid search and early stopping to optimize hyperparameters",
        "6": "Performance Verification: Conduct comparative experiments on multiple datasets to analyze model performance differences in different scenarios.",
    },
    "ImplementationOrder": ["1-2", "2-3", "3-4", "4-5", "1-5", "5-6"],
    "Dataset": "Contains three types of public datasets and one actual business data: 1) Public structured dataset (approximately 500,000 samples, 30+ features); 2) Text-numeric mixed dataset (approximately 200,000 samples, including text embedding features); 3) Time series sparse dataset (approximately 100,000 samples, spanning 1 year); 4) Real transaction data from an e-commerce platform (approximately 1 million samples, including user behavior and product attribute features)",
    "EvaluationMetrics": {
        "Prediction Accuracy": "AUC and F1-score are used for classification tasks; MAE and RMSE are used for regression tasks to evaluate the basic predictive ability of the model.",
        "Robustness": "Performance decay rate is calculated through data perturbation testing (adding noise and simulating feature loss) to measure model stability.",
        "Efficiency": "Record model training time, inference latency, and memory usage to evaluate computing resource consumption.",
        "Interpretability": "Use SHAP values and feature importance ranking to quantify the feature contribution to model decisions.",
        "Generalization": "Performance retention across datasets to evaluate the model's adaptability to unseen data.",
    },
    "ExpectedOutcome": "The proposed framework outperforms existing mainstream methods in comprehensive performance (accuracy, robustness, and efficiency) across multiple datasets, particularly in scenarios with uneven data distribution and cross-scenario migration. It also enhances model interpretability through a dynamic feature interaction mechanism, providing effective support for practical business decision-making.",
}

dataset = load_dataset("InternScience/SGI-IdeaGeneration", split="test")
row = dataset[0]
prompt = row["question"] + f"""

### Example:
```json
{json.dumps(EXAMPLE, indent=4)}
```"""

workspace = Path("./workspace/sgi_ideageneration_example").resolve()
workspace.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="RH",
    messages=[{"role": "user", "content": prompt}],
    extra_body={"workspace-root": str(workspace)},
)

print(response.choices[0].message.content)
````

## Rationale

- The benchmark parses a structured proposal with fixed fields.
- The scoring emphasizes effectiveness, novelty, detailedness, and feasibility.
- Input rewriting can weaken domain context or constraints.
- Output rewriting can break JSON or convert a proposal into a generic summary.
- The benchmark-specific role prompt should guide the agent to inspect recent
  literature, identify limitations, and propose a concrete new method directly.

Use `RH` or `RH--<llm-model-name>` in the OpenAI SDK request as usual.
