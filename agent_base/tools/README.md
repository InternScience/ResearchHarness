# Tools

This document describes the default tool surface exposed to the model. Default tool names use PascalCase consistently. Optional compatibility tools may use their external protocol names.

The current implementation is grouped by category:

- `agent_base/tools/tool_file.py`
- `agent_base/tools/tool_extra.py`
- `agent_base/tools/tool_runtime.py`
- `agent_base/tools/tool_user.py`
- `agent_base/tools/tool_web.py`

## Overview

The current tool set is:

- `Glob`
- `Grep`
- `Read`
- `ReadPDF`
- `ReadImage`
- `Write`
- `Edit`
- `Bash`
- `WebSearch`
- `ScholarSearch`
- `WebFetch`
- `AskUser`
- `TerminalStart`
- `TerminalWrite`
- `TerminalRead`
- `TerminalInterrupt`
- `TerminalKill`

Optional extra tools are not loaded by default. Enable them explicitly with `--extra-tool NAME`.

- `str_replace_editor`

## Execution Semantics

Each tool call should represent one clear request.

Examples:

- `WebSearch.query`: one search query string
- `ScholarSearch.query`: one scholarly search query string
- `WebFetch.url`: one page URL string
- `Read.path`: one local file path

Do not pack multiple searches, URLs, or file paths into one tool argument.
Instead, issue multiple independent tool calls in the same assistant turn.

For efficiency, the runtime executes adjacent read-only tool calls concurrently,
with a default maximum of three calls per parallel block. Tool result messages
and trace events are still written back in the original model-emitted order.

Current read-only calls eligible for this parallel execution are:

- `Glob`
- `Grep`
- `Read`
- `ReadImage`
- `WebSearch`
- `ScholarSearch`
- `WebFetch`

Tools with mutation, shell, terminal, external parsing, or human-interaction
semantics are not parallelized by default. This includes `Write`, `Edit`,
`Bash`, `ReadPDF`, `AskUser`, `Terminal*`, and optional editing tools such as
`str_replace_editor`.

Example execution plan:

```text
Read, Read, Edit, Read
```

is executed as:

```text
[Read + Read]  concurrent
[Edit]         sequential
[Read]         sequential after Edit
```

This preserves the dependency boundary around `Edit`: the final `Read` cannot
run before the edit has completed.

## Tool Matrix

| Tool | Category | Arguments | Description | Return Shape / Notes |
| --- | --- | --- | --- | --- |
| `Glob` | Local files | `pattern`, `path?`, `include_dirs?`, `max_results?` | Discover files or directories by pathname pattern inside the workspace. | Returns `root`, `match_count`, `truncated`, and `results`. Best for pathname discovery rather than reading content. |
| `Grep` | Local files | `pattern`, `path?`, `glob?`, `case_sensitive?`, `max_results?`, `max_chars?` | Search local text files by content and return matching lines. | Returns search metadata plus matched file paths, line numbers, and line text. Skips obvious binary files, images, and PDFs. |
| `Read` | Local files | `path`, `start_line?`, `end_line?`, `max_chars?` | Read a local text file, optionally by line range. | Returns normalized path, line metadata, truncation status, and `content`. Redirects PDF/image tasks toward `ReadPDF` or `ReadImage`. |
| `ReadPDF` | Local files | `path`, `max_chars?`, `max_image_paths?` | Read a local PDF, extract text, and expose extracted image paths when available. | Returns text content plus `image_paths` and image-count metadata. Depends on [`structai`](https://github.com/black-yt/structai) and `MINERU_TOKEN`. |
| `ReadImage` | Local files | `path` | Read a local image and expose image metadata for runtime multimodal use. | Returns image metadata only. During agent runs, the runtime sends a compressed attachment to the LLM API as an `image_url` content part. |
| `Write` | Local files | `path`, `content`, `overwrite?` | Create a text file or overwrite one when explicitly allowed. | Creates parent directories automatically. Returns an error if the file exists and `overwrite=false`. |
| `Edit` | Local files | `path`, `patch` | Apply a targeted patch to a local text file. | Expects unified-diff / hunk-style input. Context-based matching, not a full `patch(1)` implementation. |
| `Bash` | Runtime | `command`, `timeout?`, `workdir?` | Run one-shot shell commands for deterministic local execution, parsing, and validation. | Returns `stdout` and `stderr`. Primary local execution tool for short Python, `rg`, `find`, `git`, and structured local processing. |
| `WebSearch` | Web | `query` | Perform one general web search. Call it multiple times in one assistant turn for multiple queries. | Returns a text summary headed by `## Web Results` with title, link, snippet, and date/source when available. Uses Serper. |
| `ScholarSearch` | Web | `query` | Perform one academic search for papers, year, abstract, and citations. Call it multiple times in one assistant turn for multiple queries. | Returns a text summary headed by `## Scholar Results` with title, PDF link, publication info, year, citation count, and abstract. Uses Serper Scholar. |
| `WebFetch` | Web | `url`, `start_line?`, `end_line?`, `max_chars?` | Fetch a page and return cleaned, range-bounded webpage text. | Uses Jina Reader only. Returns metadata plus page content so the main agent can inspect and summarize the evidence itself. |
| `AskUser` | Human interaction | `question`, `context?` | Ask the human user one concise clarification question when essential information cannot be determined from tools or existing instructions. | Writes the question to the interactive terminal and returns the user's answer. If no interactive terminal is available, returns an explicit unavailable message. |
| `TerminalStart` | Runtime | `cwd?`, `shell?`, `rows?`, `cols?` | Start a persistent terminal session. | Returns session metadata such as `session_id`, `pid`, `cwd`, `shell`, `alive`, and `returncode`. |
| `TerminalWrite` | Runtime | `session_id`, `input`, `append_newline?`, `yield_time_ms?`, `max_output_chars?` | Send input to a persistent terminal session and read incremental output. | Best for stateful shells, REPLs, and long-running foreground processes. |
| `TerminalRead` | Runtime | `session_id`, `yield_time_ms?`, `max_output_chars?` | Read unread output from an existing persistent terminal session. | Useful when a process is still running and output arrives over time. |
| `TerminalInterrupt` | Runtime | `session_id`, `max_output_chars?` | Send `Ctrl-C` to the foreground process in a terminal session without destroying the session. | Use when a long-running process must be interrupted but the shell should remain alive. |
| `TerminalKill` | Runtime | `session_id`, `force?` | Terminate a persistent terminal session and release resources. | Final cleanup step for terminal sessions that are no longer needed. |
| `str_replace_editor` | Optional compatibility | `command`, `path`, `file_text?`, `old_str?`, `new_str?`, `insert_line?`, `view_range?` | Text editing compatibility tool. | Not loaded by default. Enable with `--extra-tool str_replace_editor`. Requires absolute paths inside the workspace. |

## Glob

Purpose:

- Discover local files or directories by glob pattern.
- Good for pathname discovery, not for reading file contents.

Arguments:

- `pattern`: string, a `pathlib`-style glob such as `**/*.py`
- `path`: optional string, search root, defaults to the current workspace
- `include_dirs`: optional boolean, defaults to `false`
- `max_results`: optional integer, defaults to `200`

Returns:

- `root`
- `pattern`
- `include_dirs`
- `match_count`
- `truncated`
- `results`

## Grep

Purpose:

- Search local text files by content.
- Return matched file paths, line numbers, and line text.

Arguments:

- `pattern`: string, regular expression
- `path`: optional string, file or directory path, defaults to the current workspace
- `glob`: optional string, file filter when scanning a directory, defaults to `**/*`
- `case_sensitive`: optional boolean, defaults to `false`
- `max_results`: optional integer, defaults to `100`
- `max_chars`: optional integer, defaults to `20000`

Behavior:

- If `path` is a file, only that file is searched.
- If `path` is a directory, matching text files are searched recursively.
- Images, PDFs, and obviously binary files are skipped.

Returns:

- `root`
- `pattern`
- `glob`
- `case_sensitive`
- `files_scanned`
- `match_count`
- `truncated`
- `results`

## Read

Purpose:

- Read a local text file.
- Support partial line ranges.
- Support long-text truncation.

Arguments:

- `path`: string, file path
- `start_line`: optional integer, 1-based start line
- `end_line`: optional integer, 1-based end line
- `max_chars`: optional integer, maximum returned characters, defaults to `20000`

Behavior:

- Only text files are handled directly.
- If the input is a PDF, the tool tells the model to use `ReadPDF`.
- If the input is an image, the tool tells the model to use `ReadImage`.

Returns:

- `path`
- `source_type: text`
- `start_line`
- `end_line`
- `total_lines`
- `truncated`
- `content`

## ReadPDF

Purpose:

- Read a local PDF.
- Return extracted text.
- Return extracted local image paths when the PDF parser produces image assets.

Arguments:

- `path`: string, PDF path
- `max_chars`: optional integer, maximum returned characters, defaults to `20000`
- `max_image_paths`: optional integer, maximum listed extracted image paths, defaults to `20`

Behavior:

- Calls `structai.read_pdf(...)` from [`structai`](https://github.com/black-yt/structai) underneath.
- Uses the returned `text` and `img_paths`.
- Depends on `MINERU_TOKEN`.
- If [`structai`](https://github.com/black-yt/structai) is missing, returns a clear dependency error instead of breaking unrelated file tools.
- For PDF figure tasks, prefer `ReadPDF` first to discover extracted text and extracted image paths, then use `ReadImage` on the actual extracted image file.

Returns:

- `path`
- `source_type: pdf`
- `total_lines`
- `truncated`
- `image_count`
- `image_paths_listed`
- `image_paths_truncated`
- `image_paths`
- `content`

## ReadImage

Purpose:

- Read a local image.
- Return image metadata.
- During a main agent run, pass a compressed image to the LLM API as an `image_url` content part instead of stuffing raw base64 text into ordinary message text.

Arguments:

- `path`: string, image path

Behavior:

- Uses `PIL.Image.open(...)` underneath.
- The runtime creates a compressed JPEG attachment for the LLM request and sends it as an inline `data:` URL in an `image_url` content part.
- Trace records and direct tool output keep image metadata only, not the full binary payload.

Returns:

- `path`
- `source_type`
- `format`
- `mime_type`
- `mode`
- `width`
- `height`
- `byte_count`
- `llm_attachment_format`
- `llm_attachment_width`
- `llm_attachment_height`
- `llm_attachment_byte_count`

## Write

Purpose:

- Create a text file.
- Overwrite an existing file when explicitly requested.

Arguments:

- `path`: string, destination file path
- `content`: string, complete file content
- `overwrite`: optional boolean, defaults to `false`

Behavior:

- Parent directories are created automatically.
- If `overwrite=false` and the file already exists, the tool returns an error.

## Edit

Purpose:

- Edit a local text file partially.
- Best for targeted patches, not full-file rewrites.

Arguments:

- `path`: string, destination file path
- `patch`: string, unified-diff / hunk-style patch

Behavior:

- Requires explicit hunks such as `@@ -1,2 +1,2 @@`.
- The current implementation matches by surrounding context blocks rather than implementing full `patch(1)` line-number semantics.

Returns:

- updated file path on success
- applied hunk count

## Bash

Purpose:

- Execute one-shot shell commands.
- Handle paths, search, git, conda, and local script orchestration.
- Serve as the primary local execution tool for temporary Python, deterministic computation, validation, formatting, and parsing.

Arguments:

- `command`: string, shell command to execute
- `timeout`: optional integer, seconds, defaults to `30`
- `workdir`: optional string, working directory

Behavior:

- Uses local `bash`.
- Returns both `stdout` and `stderr`.
- Timeout produces an explicit error.
- Short scripts are well suited to a heredoc such as `python3 - <<'PY'`.

Recommended use cases:

- pathname and file discovery
- `rg`, `find`, `git`
- local Python or other CLI programs
- deterministic CSV / JSON / text processing
- local computation and validation against absolute paths returned by file tools

## WebSearch

Purpose:

- General web search.
- Handles one query per tool call.
- For multiple complementary queries, issue multiple `WebSearch` tool calls in the same assistant turn.

Arguments:

- `query`: string, one search query

Behavior:

- Calls Serper's Google Search endpoint.
- Reads `SERPER_KEY` at runtime.

Returns:

- query summary text
- `## Web Results`
- title, link, snippet, and date/source when available

## ScholarSearch

Purpose:

- Academic search.
- Return paper title, year, abstract, citation count, and related metadata.
- Handles one query per tool call.
- For multiple complementary queries, issue multiple `ScholarSearch` tool calls in the same assistant turn.

Arguments:

- `query`: string, one academic search query

Behavior:

- Calls Serper's Google Scholar endpoint.
- Reads `SERPER_KEY` at runtime.

Returns:

- query summary text
- `## Scholar Results`
- title, PDF link, `publicationInfo`, year, citation count, and abstract

## WebFetch

Purpose:

- Visit a webpage.
- Return cleaned page text for the main agent to inspect.
- Keep default output bounded while allowing follow-up range reads for the full page.

Arguments:

- `url`: string, page URL
- `start_line`: optional integer, 1-based start line, defaults to `1`
- `end_line`: optional integer, 1-based end line
- `max_chars`: optional integer, maximum returned characters, defaults to and cannot exceed `WEBFETCH_MAX_CHARS` or `30000`

Behavior:

- Fetches page text through Jina Reader.
- Use multiple WebFetch calls when you need to inspect multiple URLs.
- Applies simple deterministic cleanup to whitespace and blank lines.
- Applies the requested line range and per-call character limit.
- Does not call an LLM inside the tool; the main agent is responsible for reading, reasoning over, and summarizing the returned content.

Dependencies:

- `JINA_KEY`

Returns:

- `url`
- `source_type: web`
- `start_line`, `end_line`, `total_lines`
- `total_chars`, `max_chars`, `returned_chars`, `truncated`
- `content`

## TerminalStart

Purpose:

- Start a persistent terminal session.

Arguments:

- `cwd`: optional string, working directory
- `shell`: optional string, shell path
- `rows`: optional integer, terminal rows, defaults to `30`
- `cols`: optional integer, terminal columns, defaults to `120`

Returns:

- `session_id`
- `pid`
- `cwd`
- `shell`
- `alive`
- `returncode`

## TerminalWrite

Purpose:

- Send input to an existing terminal session and read output.

Arguments:

- `session_id`: string, session id
- `input`: string, text to send
- `append_newline`: optional boolean, defaults to `true`
- `yield_time_ms`: optional integer, defaults to `200`
- `max_output_chars`: optional integer, defaults to `20000`

## TerminalRead

Purpose:

- Read unread output from an existing terminal session.

Arguments:

- `session_id`: string, session id
- `yield_time_ms`: optional integer, defaults to `200`
- `max_output_chars`: optional integer, defaults to `20000`

## TerminalInterrupt

Purpose:

- Send `Ctrl-C` to the foreground process in a terminal session.
- Keep the session alive.

Arguments:

- `session_id`: string, session id
- `max_output_chars`: optional integer, defaults to `20000`

## TerminalKill

Purpose:

- Terminate a terminal session.
- Release related resources.

Arguments:

- `session_id`: string, session id
- `force`: optional boolean, defaults to `false`

## AskUser

Purpose:

- Ask the human user for essential missing information, preference, or approval.
- Use only when the answer cannot be determined from workspace files, available tools, or existing instructions.

Arguments:

- `question`: string, concise question to ask.
- `context`: optional string, brief explanation of why the question is necessary.

Behavior:

- Writes the question to the interactive terminal and waits for one user answer.
- Returns an explicit unavailable message instead of blocking when no interactive terminal exists.
- Not available in ResearchClawBench runs.

## str_replace_editor

Purpose:

- Provide an optional compatibility editor for external agents that expect a `str_replace_editor` tool.
- Keep compatibility editing outside the default ResearchHarness tool set.

Enable:

```bash
python3 run_agent.py "..." --workspace-root ./workspace --extra-tool str_replace_editor
python3 run_server.py --api-runs-dir ./api_runs --extra-tool str_replace_editor
python3 run_frontend.py --extra-tool str_replace_editor
```

Behavior:

- Requires absolute paths inside the active workspace.
- Supports `view`, `create`, `str_replace`, `insert`, and `undo_edit`.
- `str_replace` requires an exact, unique `old_str` match.
- `create` refuses to overwrite an existing file.
- `undo_edit` reverts the last successful edit recorded for that file by this tool instance.
- Text files are displayed with `cat -n`-style line numbers.
- Directory view lists non-hidden files and directories up to two levels deep.
- PDFs are routed through `ReadPDF`; Office files use lightweight archive text extraction; audio files return metadata only.

## Suggested Usage

- Use `Glob` first for pathname discovery.
- Use `Grep` first for local text search.
- Use `Read` for local text files.
- Use `ReadPDF` for local PDFs.
- Use `ReadImage` for local images.
- Use `Edit` for targeted file changes.
- Use `Write` for full-file writes.
- Use `Bash` for one-shot system commands.
- Use `AskUser` only when a human answer is genuinely necessary.
- Use `str_replace_editor` only when an external compatibility layer requires that exact editing protocol.
- Use `Terminal*` only when persistent interactive shell state is actually needed.
- Route pure Python analysis through `Bash` rather than introducing a separate Python tool.
