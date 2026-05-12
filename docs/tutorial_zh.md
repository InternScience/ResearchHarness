# ResearchHarness 教程

本文介绍如何通过命令行和 OpenAI-compatible API 使用 ResearchHarness。

ResearchHarness 是一个轻量、通用的 tool-using LLM agent harness。它可以作为：

- 命令行本地 agent，
- agent benchmark 的公平执行底座，
- OpenAI-compatible 同步 API 后端，
- 面向代码、文件、报告、PDF、图片、网页任务的个人助手运行时。

## 1. 安装

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

推荐使用 Python 3.10+。

## 2. 配置环境变量

复制 `.env.example` 为 `.env`，并填写必需变量。

必需变量：

| 变量 | 含义 |
| --- | --- |
| `API_KEY` | OpenAI-compatible LLM 服务的 API key。 |
| `API_BASE` | OpenAI-compatible chat-completions endpoint 的 base URL。 |
| `MODEL_NAME` | ResearchHarness 使用的主模型。 |
| `SERPER_KEY_ID` | `WebSearch` 和 `ScholarSearch` 使用的 Serper key：https://serper.dev/ |
| `JINA_API_KEYS` | `WebFetch` 使用的 Jina key：https://jina.ai/ |
| `MINERU_TOKEN` | `ReadPDF` 使用的 MinerU token：https://mineru.net/ |

可选变量：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `WORKSPACE_ROOT` | `./workspace` | 未显式传入 workspace 时使用的默认 workspace root。 |
| `MAX_LLM_CALL_PER_RUN` | `100` | 单次 agent run 最多允许的 LLM 调用次数。 |
| `MAX_AGENT_ROUNDS` | `100` | ReAct loop 最大轮次。 |
| `MAX_AGENT_RUNTIME_SECONDS` | `9000` | 单次 agent run 的最大运行秒数。 |
| `LLM_TIMEOUT_SECONDS` | `600` | 单次 LLM API 请求超时时间。 |
| `LLM_MAX_OUTPUT_TOKENS` | `10000` | 请求模型输出的最大 token 数。 |
| `MAX_INPUT_TOKENS` | `320000` | runtime token accounting 使用的输入 token 预算。 |
| `LLM_MAX_RETRIES` | `10` | 瞬时 LLM API 错误最大重试次数。 |
| `TEMPERATURE` | `0.6` | 主模型 temperature。 |
| `TOP_P` | `0.95` | 主模型 top-p。 |
| `PRESENCE_PENALTY` | `1.1` | provider 支持时使用的 presence penalty。 |
| `AUTO_COMPACT_TRIGGER_TOKENS` | `128k` | 自动上下文压缩触发阈值。 |
| `IMAGE_PART_TOKEN_ESTIMATE` | `1536` | 每个 image content part 的 token 估计。 |
| `LLM_IMAGE_MAX_EDGE` | `1568` | 发送给多模态模型的图片最大边长。 |
| `LLM_IMAGE_MAX_BYTES` | `524288` | 发送给多模态模型的压缩图片最大字节数。 |
| `LLM_IMAGE_JPEG_QUALITY` | `85` | 图片压缩时的初始 JPEG 质量。 |
| `DEBUG_AGENT` | `false` | 打印 agent loop 详细调试日志。 |
| `DEBUG_SEARCH` | `false` | 打印 WebSearch 调试日志。 |
| `DEBUG_SCHOLAR` | `false` | 打印 ScholarSearch 调试日志。 |
| `DEBUG_VISIT` | `false` | 打印 WebFetch 调试日志。 |

正式使用前，先运行：

```bash
python3 tests/test_tool_availability.py
```

预期结果是全部工具通过。缺 key、缺依赖、服务额度耗尽、外部工具不可用都应该视为失败，不应 skip。

如果 `WebSearch`、`ScholarSearch`、`WebFetch` 或 `ReadPDF` 出现 network、TLS、upload、download、PDF parsing 相关错误，优先尝试关闭 VPN / proxy 后重跑测试。

## 3. 命令行使用

直接运行一个 prompt：

```bash
python3 run_agent.py "Who proposed the transformer architecture, and in what year was the paper published?"
```

指定 workspace：

```bash
python3 run_agent.py "Summarize this project." \
  --workspace-root ./workspace
```

`./workspace` 可以替换为任何其他 workspace 目录。

保存 trace：

```bash
python3 run_agent.py "Summarize this project." \
  --workspace-root ./workspace \
  --trace-dir ./traces
```

`./traces` 可以替换为任何其他 trace 目录。

如果不传 `--trace-dir`，CLI 运行不会写 trace 文件。

追加 role prompt：

```bash
python3 run_agent.py "Answer this QA task." \
  --workspace-root ./workspace \
  --role-prompt-file benchmarks/QA/role_prompt.md
```

### CLI 参数

| 参数 | 是否必需 | 含义 |
| --- | --- | --- |
| 位置参数 `prompt` | 是，除非使用 `--prompt-file` | prompt 文本。 |
| `--prompt-file PATH` | 否 | 从 UTF-8 文件读取 prompt。 |
| `--workspace-root PATH` | 否 | 本地文件工具、Bash、Terminal 使用的 workspace root；不存在会自动创建。 |
| `--trace-dir PATH` | 否 | 写入 `trace_*.jsonl` 的目录。 |
| `--role-prompt-file PATH` | 否，可重复 | 追加 role-specific prompt 到 base system prompt。 |

## 4. OpenAI-Compatible API Server

ResearchHarness 可以部署为同步 OpenAI-compatible endpoint：

```http
POST /v1/chat/completions
```

这样，现有 OpenAI SDK 客户端只需要修改 `base_url` 就可以调用 ResearchHarness。

### 启动服务

默认部署：

```bash
python3 serve_openai.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686
```

QA/VQA benchmark 部署，可以额外加 role prompt：

```bash
python3 serve_openai.py \
  --api-runs-dir ./api_runs \
  --host 127.0.0.1 \
  --port 8686 \
  --role-prompt-file benchmarks/QA/role_prompt.md
```

### API Server 参数

| 参数 | 是否必需 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `--api-runs-dir PATH` | 是 | 无 | API runs 的父目录；每个请求会创建一个子目录。 |
| `--host HOST` | 否 | `127.0.0.1` | 服务监听 host。 |
| `--port PORT` | 否 | `8686` | 服务监听端口。 |
| `--role-prompt-file PATH` | 否，可重复 | 无 | 追加 role prompt 到 base ResearchHarness prompt。 |
| `--input-wrapper` / `--no-input-wrapper` | 否 | 开启 | 开启或关闭输入 LLM wrapper。 |
| `--output-wrapper` / `--no-output-wrapper` | 否 | 开启 | 开启或关闭输出 LLM wrapper。 |

### Wrapper 模式

默认两个 wrapper 都开启。

严格格式 benchmark 模式：

```bash
python3 serve_openai.py \
  --api-runs-dir ./api_runs \
  --role-prompt-file benchmarks/QA/role_prompt.md \
  --input-wrapper \
  --output-wrapper
```

直接 agent 模式：

```bash
python3 serve_openai.py \
  --api-runs-dir ./api_runs \
  --no-input-wrapper \
  --no-output-wrapper
```

输入简单但最终答案需要严格格式：

```bash
python3 serve_openai.py \
  --api-runs-dir ./api_runs \
  --no-input-wrapper \
  --output-wrapper
```

input wrapper 的作用是把原始用户请求整理为适合 agent 稳定执行的任务。output wrapper 的作用是把 agent 的最终结果整理为用户要求的答案格式。wrapper 不应该引入新事实，只做输入规范化和输出格式化。

```mermaid
flowchart LR
    U[User Input] --> IW[Input Wrapper LLM]
    IW --> A[ResearchHarness Agent]
    A --> OW[Output Wrapper LLM]
    OW --> O[Output]
```

## 5. API Workspace 结构

每个 API 请求会创建一个 run 目录：

```text
./api_runs/
`-- run_YYYYMMDD_HHMMSS_<random>/
    |-- agent_workspace/
    |   `-- inputs/
    |       `-- images/
    `-- agent_trace/
        |-- api_trace.jsonl
        |-- trace_*.jsonl
        `-- _session_state.json
```

含义：

| 路径 | 含义 |
| --- | --- |
| `run_YYYYMMDD_HHMMSS_<random>/` | 单个请求对应的 run 根目录。 |
| `agent_workspace/` | agent 唯一可见的 workspace；文件工具、Bash、`ls`、`cat` 都从这里开始。 |
| `agent_workspace/inputs/images/` | API 请求中用户提交的图片。 |
| `agent_trace/` | API trace、agent trace 和 runtime 记录。 |

对于多模态请求，图片会同时走两条路径：当底层模型支持多模态输入时，
图片内容会作为初始多模态输入直接传给模型；同一张图片也会保存到
`agent_workspace/inputs/images/`，让 agent 在工具执行过程中可以引用或检查
稳定的本地路径。

这个结构把 agent 可见工作目录和服务端记录目录隔离开。
在 API 部署模式下，trace 默认保存：每个请求都会在自己的 `agent_trace/`
目录下写入 `api_trace.jsonl`、`trace_*.jsonl` 和 `_session_state.json`。

## 6. 纯文本 OpenAI SDK 请求

```python
from openai import OpenAI

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="researchharness",
    messages=[
        {"role": "user", "content": "Answer in one sentence: what is 2 + 2?"}
    ],
)

print(response.choices[0].message.content)
```

## 7. 多模态 OpenAI SDK 请求

第一版 API 支持 `data:image/...;base64,...` 形式的图片 URL。API server 不支持远程图片 URL，也不支持让外部请求直接传本地文件路径。

下面的示例在代码中生成一张图片，并要求返回 JSON。

```python
import base64
from io import BytesIO

from PIL import Image, ImageDraw
from openai import OpenAI

image = Image.new("RGB", (320, 120), "white")
draw = ImageDraw.Draw(image)
draw.text((40, 45), "7 + 5 = ?", fill="black")
buffer = BytesIO()
image.save(buffer, format="PNG")
data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8686/v1")

response = client.chat.completions.create(
    model="researchharness",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "The image contains a simple arithmetic expression. "
                        "Return JSON with exactly two keys: expression and answer."
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ],
)

print(response.choices[0].message.content)
```

预期答案形状：

```json
{"expression":"7 + 5","answer":12}
```

## 8. API 请求与返回协议

### `POST /v1/chat/completions`

支持的请求字段：

| 字段 | 是否必需 | 含义 |
| --- | --- | --- |
| `model` | 是 | 客户端看到的 model label；不会覆盖 `.env` 中的 `MODEL_NAME`。 |
| `messages` | 是 | OpenAI-style chat messages。 |
| `stream` | 否 | 必须不存在或为 `false`；当前不支持 streaming。 |
| `n` | 否 | 必须不存在或为 `1`。 |
| `max_tokens` | 否 | output wrapper 最大输出 token。 |
| `max_completion_tokens` | 否 | output wrapper 最大输出 token 的兼容别名。 |
| `response_format` | 否 | 作为输出格式提示传给 wrapper。 |

支持的 message role：

| Role | 是否支持 |
| --- | --- |
| `system` | 支持 |
| `user` | 支持 |
| `assistant` | 支持 |
| `tool` | 不支持 |

支持的 content 形式：

```json
{"role": "user", "content": "plain text"}
```

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "question"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]
}
```

返回结构：

```json
{
  "id": "chatcmpl_...",
  "object": "chat.completion",
  "created": 1770000000,
  "model": "researchharness",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "final answer"
      },
      "finish_reason": "stop"
    }
  ]
}
```

调用方通常只需要读取：

```python
response.choices[0].message.content
```

### `GET /v1/health`

返回：

```json
{
  "status": "ok",
  "api_runs_dir": "./api_runs",
  "input_wrapper": true,
  "output_wrapper": true
}
```

## 9. 工具能力

ResearchHarness 当前包含：

| 工具 | 用途 |
| --- | --- |
| `Glob` | 按模式发现文件。 |
| `Grep` | 在文件中搜索文本。 |
| `Read` | 有边界地读取文本文件。 |
| `ReadPDF` | 通过 MinerU/structai 解析 PDF。 |
| `ReadImage` | 读取本地图片，并把图片内容传给支持 vision 的模型。 |
| `Write` | 在 workspace 内写文件。 |
| `Edit` | 在 workspace 内 patch 文件。 |
| `Bash` | 在 workspace 内执行 shell 命令。 |
| `WebSearch` | 通过 Serper 进行网页搜索。 |
| `ScholarSearch` | 通过 Serper 进行学术搜索。 |
| `WebFetch` | 通过 Jina 和配置模型抓取、总结网页。 |
| `AskUser` | 交互式运行中向用户提问；某些 benchmark adapter 会禁用。 |
| `TerminalStart` / `TerminalWrite` / `TerminalRead` / `TerminalInterrupt` / `TerminalKill` | 持久终端会话。 |

## 10. Trace 与记录

CLI 运行只有在传入 `--trace-dir` 时才会写 trace。如果不传
`--trace-dir`，CLI 运行不会写 trace 文件。

API 运行时，记录在：

```text
./api_runs/run_.../agent_trace/
```

重要文件：

| 文件 | 含义 |
| --- | --- |
| `api_trace.jsonl` | input wrapper、agent result、output wrapper 记录。 |
| `trace_*.jsonl` | agent runtime 的 flat trace。 |
| `_session_state.json` | 当前 session state；启用 trace 时和 `trace_*.jsonl` 写在同一目录。 |

trace 会记录工具调用、工具结果、LLM call capture payload、context compaction、错误和终止状态。

## 11. Benchmark Adapter

tracked benchmark contract 放在 `benchmarks/` 下。

当前 tracked adapter：

| Benchmark | 目录 | 说明 |
| --- | --- | --- |
| ResearchClawBench | `benchmarks/ResearchClawBench/` | CLI 方式接入，包含 role prompt 和 adapter。 |
| QA / VQA | `benchmarks/QA/` | OpenAI-compatible API 方式接入，支持纯文本和多模态 QA。 |

benchmark-specific 行为应放在 `benchmarks/`，不要塞进 `agent_base/`。

## 12. 测试

推荐检查：

```bash
python3 tests/test_tool_availability.py
python3 tests/test_openai_api_checks.py
python3 tests/test_agent_extension_checks.py
python3 tests/test_edge_case_checks.py
python3 tests/test_toolchain_validation.py
```

如果使用 conda：

```bash
/home/xwh/miniconda3/bin/conda run -n agent python3 tests/test_openai_api_checks.py
```

## 13. 排障

常见问题：

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 缺少 required env | `.env` 不完整 | 填写所有必需变量。 |
| Web/PDF 工具失败 | VPN/proxy/TLS/服务问题 | 关闭 VPN/proxy 后重跑工具可用性测试。 |
| 图片请求返回 400 | 图片不是 `data:image/...;base64,...` | 把图片转成 base64 data URL。 |
| 后端模型拒绝图片 | 当前模型 endpoint 不支持 vision | 换用支持 vision 的模型，或改为纯文本任务。 |
| API 报 streaming 错误 | 请求里传了 `stream=true` | 当前只支持同步请求。 |
| 输出格式不符合预期 | output wrapper 关闭，或用户格式要求不明确 | 开启 `--output-wrapper`，并清楚说明输出格式。 |

## 14. 当前边界

第一版 API 暂不包括：

- streaming，
- async run status，
- cancellation，
- artifact download endpoint，
- 远程图片 URL 下载，
- 用户认证，
- 多租户访问控制。

这些能力以后可以作为外层服务继续扩展，不需要破坏核心 harness loop。
