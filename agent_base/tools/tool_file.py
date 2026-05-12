import argparse
import base64
import io
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, Union

from PIL import Image

from agent_base.tools.tooling import ToolBase, normalize_base_root, validate_tool_path, workspace_root
from agent_base.utils import PROJECT_ROOT, load_dotenv, read_text_lossy


IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

DEFAULT_LLM_IMAGE_MAX_EDGE = 1568
DEFAULT_LLM_IMAGE_MAX_BYTES = 512 * 1024
DEFAULT_LLM_IMAGE_JPEG_QUALITY = 85
MIN_LLM_IMAGE_JPEG_QUALITY = 45
MIN_LLM_IMAGE_EDGE = 256
DEFAULT_GLOB_MAX_RESULTS = 200
DEFAULT_GREP_MAX_RESULTS = 100
DEFAULT_GREP_MAX_CHARS = 20000


def resolve_file_path(path_value: str, *, base_root: Optional[Path] = None) -> Path:
    path = Path(path_value).expanduser()
    root = normalize_base_root(base_root)
    if path.is_absolute():
        return validate_tool_path(path, "Read access", base_root=root)

    direct_candidate = root / path
    if direct_candidate.exists():
        return validate_tool_path(direct_candidate.resolve(), "Read access", base_root=root)

    if base_root is None and path.exists():
        return validate_tool_path(path.resolve(), "Read access", base_root=root)

    return validate_tool_path((root / path).resolve(strict=False), "Read access", base_root=root)


def resolve_search_root(path_value: str, *, base_root: Optional[Path] = None) -> Path:
    path = Path(path_value).expanduser()
    root = normalize_base_root(base_root)
    if path.is_absolute():
        return validate_tool_path(path, "Search access", base_root=root)
    return validate_tool_path(root / path, "Search access", base_root=root)


def _is_probably_binary(path: Path, *, sample_size: int = 4096) -> bool:
    try:
        sample = path.read_bytes()[:sample_size]
    except OSError:
        return False
    return b"\x00" in sample


class Read(ToolBase):
    name = "Read"
    description = "Read a local text file with support for partial line-range reads and output truncation."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The local file path to read.",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based start line for partial reading. Default is 1.",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based end line for partial reading. If omitted, read to the end.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum number of characters to return. Default is 20000.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def _read_text_file(self, path: Path) -> str:
        return read_text_lossy(path)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[Read] {exc}"
        base_root = kwargs.get("workspace_root")

        start_line_raw = params.get("start_line", 1)
        end_line_raw = params.get("end_line")
        max_chars_raw = params.get("max_chars", 20000)
        try:
            start_line = int(start_line_raw)
            end_line = end_line_raw
            end_line = int(end_line) if end_line is not None else None
            max_chars = int(max_chars_raw)
        except (TypeError, ValueError):
            return "[Read] start_line, end_line, and max_chars must be integers when provided."
        try:
            path = resolve_file_path(params["path"], base_root=base_root)
        except ValueError as exc:
            return f"[Read] Blocked or invalid path: {exc}"

        if not path.exists():
            return f"[Read] File not found: {path}"
        if not path.is_file():
            return f"[Read] Path is not a file: {path}"
        if path.suffix.lower() == ".pdf":
            return f"[Read] PDF files are not supported by Read. Use ReadPDF instead: {path}"
        if path.suffix.lower() in IMAGE_SUFFIXES:
            return f"[Read] Image files are not supported by Read. Use ReadImage instead: {path}"
        if start_line < 1:
            return "[Read] start_line must be >= 1."
        if end_line is not None and end_line < start_line:
            return "[Read] end_line must be >= start_line."
        if max_chars <= 0:
            return "[Read] max_chars must be > 0."

        try:
            text = self._read_text_file(path)
        except OSError as exc:
            return f"[Read] Error reading file: {exc}"

        lines = text.splitlines()
        selected = lines[start_line - 1:end_line]
        content = "\n".join(selected)

        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        meta = [
            f"path: {path}",
            "source_type: text",
            f"start_line: {start_line}",
            f"end_line: {end_line if end_line is not None else len(lines)}",
            f"total_lines: {len(lines)}",
            f"truncated: {str(truncated).lower()}",
        ]
        return "\n".join(meta) + "\ncontent:\n" + content


class ReadPDF(ToolBase):
    name = "ReadPDF"
    description = "Read a local PDF file and return extracted text. When the PDF parser extracts local image assets, also return their local paths so downstream steps can inspect the actual figure files with ReadImage."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The local PDF path to read. Relative paths are resolved from the current workspace.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum number of characters to return. Default is 20000.",
            },
            "max_image_paths": {
                "type": "integer",
                "description": "Maximum number of extracted image paths to list. Default is 20.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[ReadPDF] {exc}"
        base_root = kwargs.get("workspace_root")

        try:
            max_chars = int(params.get("max_chars", 20000))
            max_image_paths = int(params.get("max_image_paths", 20))
        except (TypeError, ValueError):
            return "[ReadPDF] max_chars and max_image_paths must be integers."
        try:
            path = resolve_file_path(params["path"], base_root=base_root)
        except ValueError as exc:
            return f"[ReadPDF] Blocked or invalid path: {exc}"

        if not path.exists():
            return f"[ReadPDF] File not found: {path}"
        if not path.is_file():
            return f"[ReadPDF] Path is not a file: {path}"
        if path.suffix.lower() != ".pdf":
            return f"[ReadPDF] File is not a PDF: {path}"
        if max_chars <= 0:
            return "[ReadPDF] max_chars must be > 0."
        if max_image_paths <= 0:
            return "[ReadPDF] max_image_paths must be > 0."

        try:
            from structai import read_pdf as structai_read_pdf
        except ImportError:
            return "[ReadPDF] Missing required dependency: structai. Install requirements and configure MINERU_TOKEN to enable PDF reading."

        try:
            result = structai_read_pdf(str(path))
            if isinstance(result, list):
                result = result[0] if result else None
            if not isinstance(result, dict):
                raise ValueError(f"unexpected pdf result type: {type(result)}")
            text = result.get("text", "")
            if not isinstance(text, str):
                raise ValueError("PDF text must be a string")
            raw_img_paths = result.get("img_paths", []) or []
            if not isinstance(raw_img_paths, list):
                raise ValueError("PDF img_paths must be a list when present")
            if not text.strip() and not raw_img_paths:
                raise ValueError("PDF text is empty and no extracted images were found")
        except (OSError, ValueError, TypeError) as exc:
            return f"[ReadPDF] Error reading PDF: {exc}"

        resolved_img_paths: list[str] = []
        for raw_img_path in raw_img_paths:
            if not isinstance(raw_img_path, str) or not raw_img_path.strip():
                continue
            candidate = Path(raw_img_path).expanduser()
            if not candidate.is_absolute():
                candidate = (path.parent / candidate).resolve()
            try:
                validated = validate_tool_path(candidate, "ReadPDF extracted image access", base_root=base_root)
            except ValueError:
                continue
            resolved_img_paths.append(str(validated))

        truncated = len(text) > max_chars
        content = text[:max_chars] if truncated else text
        line_count = len(text.splitlines())
        listed_img_paths = resolved_img_paths[:max_image_paths]
        img_paths_truncated = len(resolved_img_paths) > len(listed_img_paths)
        meta = [
            f"path: {path}",
            "source_type: pdf",
            f"total_lines: {line_count}",
            f"truncated: {str(truncated).lower()}",
            f"image_count: {len(resolved_img_paths)}",
            f"image_paths_listed: {len(listed_img_paths)}",
            f"image_paths_truncated: {str(img_paths_truncated).lower()}",
        ]
        output = "\n".join(meta)
        if listed_img_paths:
            output += "\nimage_paths:\n" + "\n".join(listed_img_paths)
        return output + "\ncontent:\n" + content


class ReadImage(ToolBase):
    name = "ReadImage"
    description = "Read a local image file and return metadata. In the main agent runtime, the image is attached to the llm api request as an image content part instead of being inlined as ordinary conversation text."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The local image path to read. Relative paths are resolved from the current workspace.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def _build_llm_attachment(self, image: Image.Image) -> tuple[bytes, int, int]:
        max_edge = int(os.getenv("LLM_IMAGE_MAX_EDGE", str(DEFAULT_LLM_IMAGE_MAX_EDGE)))
        max_bytes = int(os.getenv("LLM_IMAGE_MAX_BYTES", str(DEFAULT_LLM_IMAGE_MAX_BYTES)))
        quality = int(os.getenv("LLM_IMAGE_JPEG_QUALITY", str(DEFAULT_LLM_IMAGE_JPEG_QUALITY)))

        attachment = image.copy()
        if max(attachment.size) > max_edge:
            attachment.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if attachment.mode not in {"RGB", "L"}:
            attachment = attachment.convert("RGB")

        payload = b""
        while True:
            current_quality = quality
            while True:
                buffer = io.BytesIO()
                attachment.save(buffer, format="JPEG", quality=current_quality, optimize=True)
                payload = buffer.getvalue()
                if len(payload) <= max_bytes:
                    return payload, attachment.size[0], attachment.size[1]
                if current_quality <= MIN_LLM_IMAGE_JPEG_QUALITY:
                    break
                current_quality = max(current_quality - 10, MIN_LLM_IMAGE_JPEG_QUALITY)

            width, height = attachment.size
            if max(width, height) <= MIN_LLM_IMAGE_EDGE:
                raise ValueError(
                    f"compressed image attachment still exceeds LLM_IMAGE_MAX_BYTES={max_bytes}"
                )

            shrink_ratio = 0.85
            next_width = max(int(width * shrink_ratio), MIN_LLM_IMAGE_EDGE)
            next_height = max(int(height * shrink_ratio), MIN_LLM_IMAGE_EDGE)
            if (next_width, next_height) == (width, height):
                raise ValueError(
                    f"compressed image attachment still exceeds LLM_IMAGE_MAX_BYTES={max_bytes}"
                )
            attachment = attachment.resize((next_width, next_height), Image.Resampling.LANCZOS)

    def _read_image_artifact(self, params: Union[str, dict], **kwargs) -> Union[str, dict[str, Any]]:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[ReadImage] {exc}"
        base_root = kwargs.get("workspace_root")

        try:
            path = resolve_file_path(params["path"], base_root=base_root)
        except ValueError as exc:
            return f"[ReadImage] Blocked or invalid path: {exc}"

        if not path.exists():
            return f"[ReadImage] File not found: {path}"
        if not path.is_file():
            return f"[ReadImage] Path is not a file: {path}"

        try:
            with Image.open(path) as image:
                image.load()
                format_name = image.format or "unknown"
                width, height = image.size
                mode = image.mode
                image_bytes = path.read_bytes()
                attachment_bytes, attachment_width, attachment_height = self._build_llm_attachment(image)
        except (OSError, ValueError) as exc:
            return f"[ReadImage] Error reading image: {exc}"

        mime_type = Image.MIME.get(format_name.upper(), None) if isinstance(format_name, str) else None
        if not mime_type:
            suffix = path.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                mime_type = "image/jpeg"
            elif suffix == ".png":
                mime_type = "image/png"
            elif suffix == ".gif":
                mime_type = "image/gif"
            elif suffix == ".webp":
                mime_type = "image/webp"
            elif suffix in {".tif", ".tiff"}:
                mime_type = "image/tiff"
            elif suffix == ".bmp":
                mime_type = "image/bmp"
            else:
                mime_type = "application/octet-stream"

        encoded = base64.b64encode(attachment_bytes).decode("ascii")
        data_url = f"data:image/jpeg;base64,{encoded}"
        return {
            "kind": "image_tool_result",
            "path": str(path),
            "source_type": "image",
            "format": format_name,
            "mode": mode,
            "width": width,
            "height": height,
            "mime_type": mime_type,
            "byte_count": len(image_bytes),
            "llm_attachment_format": "JPEG",
            "llm_attachment_width": attachment_width,
            "llm_attachment_height": attachment_height,
            "llm_attachment_byte_count": len(attachment_bytes),
            "data_url": data_url,
        }

    @staticmethod
    def _metadata_text(artifact: dict[str, Any]) -> str:
        meta = [
            f"path: {artifact['path']}",
            f"source_type: {artifact['source_type']}",
            f"format: {artifact['format']}",
            f"mime_type: {artifact['mime_type']}",
            f"mode: {artifact['mode']}",
            f"width: {artifact['width']}",
            f"height: {artifact['height']}",
            f"byte_count: {artifact['byte_count']}",
            f"llm_attachment_format: {artifact['llm_attachment_format']}",
            f"llm_attachment_width: {artifact['llm_attachment_width']}",
            f"llm_attachment_height: {artifact['llm_attachment_height']}",
            f"llm_attachment_byte_count: {artifact['llm_attachment_byte_count']}",
            "llm_image_attached: true",
        ]
        return "\n".join(meta)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        artifact = self._read_image_artifact(params, **kwargs)
        if isinstance(artifact, str):
            return artifact
        return self._metadata_text(artifact)

    def call_for_llm(self, params: Union[str, dict], **kwargs) -> Union[str, dict[str, Any]]:
        artifact = self._read_image_artifact(params, **kwargs)
        if isinstance(artifact, str):
            return artifact
        return {
            "kind": "image_tool_result",
            "text": self._metadata_text(artifact),
            "path": artifact["path"],
            "source_type": artifact["source_type"],
            "format": artifact["format"],
            "mime_type": artifact["mime_type"],
            "mode": artifact["mode"],
            "width": artifact["width"],
            "height": artifact["height"],
            "byte_count": artifact["byte_count"],
            "llm_attachment_format": artifact["llm_attachment_format"],
            "llm_attachment_width": artifact["llm_attachment_width"],
            "llm_attachment_height": artifact["llm_attachment_height"],
            "llm_attachment_byte_count": artifact["llm_attachment_byte_count"],
            "image_url": artifact["data_url"],
        }


class Glob(ToolBase):
    name = "Glob"
    description = "Find local files or directories by glob pattern inside the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "A pathlib-style glob pattern such as '**/*.py' or '*.md'.",
            },
            "path": {
                "type": "string",
                "description": "Optional search root. Defaults to the current workspace root.",
            },
            "include_dirs": {
                "type": "boolean",
                "description": "Whether to include directories in results. Default is false.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matched paths to return. Default is 200.",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[Glob] {exc}"
        base_root = kwargs.get("workspace_root")

        pattern = params["pattern"].strip()
        if not pattern:
            return "[Glob] pattern must be a non-empty string."

        search_root_value = str(params.get("path", "."))
        include_dirs = bool(params.get("include_dirs", False))
        try:
            max_results = int(params.get("max_results", DEFAULT_GLOB_MAX_RESULTS))
        except (TypeError, ValueError):
            return "[Glob] max_results must be an integer."
        if max_results <= 0:
            return "[Glob] max_results must be > 0."

        try:
            search_root = resolve_search_root(search_root_value, base_root=base_root)
        except ValueError as exc:
            return f"[Glob] Blocked or invalid path: {exc}"

        if not search_root.exists():
            return f"[Glob] Search root not found: {search_root}"
        if not search_root.is_dir():
            return f"[Glob] Search root is not a directory: {search_root}"

        try:
            raw_matches = sorted(search_root.glob(pattern))
        except (OSError, ValueError) as exc:
            return f"[Glob] Invalid glob pattern or filesystem error: {exc}"

        matches: list[str] = []
        truncated = False
        for candidate in raw_matches:
            try:
                resolved = validate_tool_path(candidate.resolve(strict=False), "Glob access", base_root=base_root or search_root)
            except ValueError:
                continue
            if resolved.is_dir() and not include_dirs:
                continue
            if resolved.is_file() or (include_dirs and resolved.is_dir()):
                matches.append(str(resolved))
            if len(matches) >= max_results:
                truncated = len(raw_matches) > max_results
                break

        meta = [
            f"root: {search_root}",
            f"pattern: {pattern}",
            f"include_dirs: {str(include_dirs).lower()}",
            f"match_count: {len(matches)}",
            f"truncated: {str(truncated).lower()}",
        ]
        if not matches:
            return "\n".join(meta) + "\nresults:\n"
        return "\n".join(meta) + "\nresults:\n" + "\n".join(matches)


class Grep(ToolBase):
    name = "Grep"
    description = "Search local text files for a regex pattern and return matching lines with file paths and line numbers."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "A regular expression pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Optional file or directory path to search. Defaults to the current workspace root.",
            },
            "glob": {
                "type": "string",
                "description": "Optional pathlib-style glob filter used when searching a directory. Default is '**/*'.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Whether the regex match should be case-sensitive. Default is false.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matching lines to return. Default is 100.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum number of characters to return. Default is 20000.",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def _iter_candidate_files(self, root: Path, glob_pattern: str, *, base_root: Optional[Path]) -> list[Path]:
        if root.is_file():
            return [root]
        candidates: list[Path] = []
        for candidate in root.glob(glob_pattern):
            try:
                resolved = validate_tool_path(candidate.resolve(strict=False), "Grep access", base_root=base_root or root)
            except ValueError:
                continue
            if resolved.is_file():
                candidates.append(resolved)
        return sorted(candidates)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[Grep] {exc}"
        base_root = kwargs.get("workspace_root")

        pattern = params["pattern"].strip()
        if not pattern:
            return "[Grep] pattern must be a non-empty string."

        search_root_value = str(params.get("path", "."))
        glob_pattern = str(params.get("glob", "**/*")).strip() or "**/*"
        case_sensitive = bool(params.get("case_sensitive", False))
        try:
            max_results = int(params.get("max_results", DEFAULT_GREP_MAX_RESULTS))
            max_chars = int(params.get("max_chars", DEFAULT_GREP_MAX_CHARS))
        except (TypeError, ValueError):
            return "[Grep] max_results and max_chars must be integers."
        if max_results <= 0:
            return "[Grep] max_results must be > 0."
        if max_chars <= 0:
            return "[Grep] max_chars must be > 0."

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return f"[Grep] Invalid regex pattern: {exc}"

        try:
            search_root = resolve_search_root(search_root_value, base_root=base_root)
        except ValueError as exc:
            return f"[Grep] Blocked or invalid path: {exc}"

        if not search_root.exists():
            return f"[Grep] Search root not found: {search_root}"
        if not search_root.is_file() and not search_root.is_dir():
            return f"[Grep] Search root is not a file or directory: {search_root}"

        matches: list[str] = []
        files_scanned = 0
        truncated = False
        for candidate in self._iter_candidate_files(search_root, glob_pattern, base_root=base_root):
            if candidate.suffix.lower() == ".pdf" or candidate.suffix.lower() in IMAGE_SUFFIXES:
                continue
            if _is_probably_binary(candidate):
                continue
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    files_scanned += 1
                    for line_index, raw_line in enumerate(handle, start=1):
                        line = raw_line.rstrip("\n")
                        if not compiled.search(line):
                            continue
                        entry = f"{candidate}:{line_index}: {line}"
                        projected_length = len("\n".join(matches + [entry]))
                        if projected_length > max_chars:
                            truncated = True
                            break
                        matches.append(entry)
                        if len(matches) >= max_results:
                            truncated = True
                            break
            except OSError:
                continue
            if truncated:
                break

        body = "\n".join(matches)

        meta = [
            f"root: {search_root}",
            f"pattern: {pattern}",
            f"glob: {glob_pattern}",
            f"case_sensitive: {str(case_sensitive).lower()}",
            f"files_scanned: {files_scanned}",
            f"match_count: {len(matches)}",
            f"truncated: {str(truncated).lower()}",
        ]
        if not body:
            return "\n".join(meta) + "\nresults:\n"
        return "\n".join(meta) + "\nresults:\n" + body


class Write(ToolBase):
    name = "Write"
    description = "Create a local text file with full content. Parent directories are created automatically."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The local file path to create.",
            },
            "content": {
                "type": "string",
                "description": "The full file content to write.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to overwrite an existing file. Default is false.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            base_root = kwargs.get("workspace_root") or workspace_root()
            path = validate_tool_path(params["path"], "Write access", base_root=base_root)
        except ValueError as exc:
            return f"[Write] {exc}"

        content = params["content"]
        overwrite = bool(params.get("overwrite", False))

        if path.exists() and not overwrite:
            return f"[Write] File already exists and overwrite is false: {path}"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"[Write] Wrote file: {path}"
        except OSError as exc:
            return f"[Write] Error writing file: {exc}"


class Edit(ToolBase):
    name = "Edit"
    description = "Edit a local text file using unified diff style hunks. The patch must describe the exact line-level changes to apply."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The local file path to edit.",
            },
            "patch": {
                "type": "string",
                "description": "A unified diff style patch containing one or more hunks for this file. Include hunk headers such as @@ -1,2 +1,2 @@.",
            },
        },
        "required": ["path", "patch"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def _parse_unified_patch(self, patch_text: str) -> list[dict]:
        lines = patch_text.splitlines()
        hunks: list[dict] = []
        current_hunk = None

        for line in lines:
            if line.startswith("--- ") or line.startswith("+++ "):
                continue
            if line.startswith("@@ "):
                if current_hunk is not None:
                    hunks.append(current_hunk)
                current_hunk = {"header": line, "lines": []}
                continue
            if current_hunk is None:
                continue
            if line.startswith((" ", "+", "-")):
                current_hunk["lines"].append((line[:1], line[1:]))
                continue
            if line == r"\ No newline at end of file":
                continue
            raise ValueError(f"unsupported patch line: {line}")

        if current_hunk is not None:
            hunks.append(current_hunk)

        if not hunks:
            raise ValueError("no patch hunks found")
        return hunks

    def _apply_hunks(self, original_text: str, hunks: list[dict]) -> tuple[str, int]:
        original_lines = original_text.splitlines()
        original_endswith_newline = original_text.endswith("\n")
        output_lines: list[str] = []
        cursor = 0

        for hunk_index, hunk in enumerate(hunks, start=1):
            hunk_lines = hunk["lines"]
            old_block = []
            new_block = []
            for prefix, content in hunk_lines:
                if prefix in {" ", "-"}:
                    old_block.append(content)
                if prefix in {" ", "+"}:
                    new_block.append(content)

            start_pos = None
            max_start = len(original_lines) - len(old_block)
            for pos in range(cursor, max_start + 1):
                if original_lines[pos:pos + len(old_block)] == old_block:
                    start_pos = pos
                    break

            if start_pos is None:
                old_preview = "\n".join(old_block)
                raise ValueError(f"hunk #{hunk_index} context not found:\n{old_preview}")

            output_lines.extend(original_lines[cursor:start_pos])
            output_lines.extend(new_block)
            cursor = start_pos + len(old_block)

        output_lines.extend(original_lines[cursor:])
        updated_text = "\n".join(output_lines)
        if original_endswith_newline:
            updated_text += "\n"
        return updated_text, len(hunks)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            base_root = kwargs.get("workspace_root") or workspace_root()
            path = validate_tool_path(params["path"], "Edit access", base_root=base_root)
        except ValueError as exc:
            return f"[Edit] {exc}"

        patch_text = str(params["patch"])

        if not path.exists():
            return f"[Edit] File not found: {path}"
        if not path.is_file():
            return f"[Edit] Path is not a file: {path}"
        if not patch_text.strip():
            return "[Edit] 'patch' must be a non-empty unified diff string."

        try:
            text = read_text_lossy(path)
        except OSError as exc:
            return f"[Edit] Error reading file: {exc}"

        try:
            hunks = self._parse_unified_patch(patch_text)
            updated, applied = self._apply_hunks(text, hunks)
        except ValueError as exc:
            return f"[Edit] Failed to apply patch: {exc}"

        if updated == text:
            return f"[Edit] No changes applied: {path}"

        try:
            path.write_text(updated, encoding="utf-8")
            return f"[Edit] Updated file: {path}; applied_hunks: {applied}"
        except OSError as exc:
            return f"[Edit] Error writing file: {exc}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local file tools directly.")
    subparsers = parser.add_subparsers(dest="tool", required=True)

    read_parser = subparsers.add_parser("read", help="Run Read on a text file.")
    read_parser.add_argument("path")
    read_parser.add_argument("--start-line", type=int, default=1)
    read_parser.add_argument("--end-line", type=int)
    read_parser.add_argument("--max-chars", type=int, default=20000)

    pdf_parser = subparsers.add_parser("pdf", help="Run ReadPDF on a PDF file.")
    pdf_parser.add_argument("path")
    pdf_parser.add_argument("--max-chars", type=int, default=20000)

    image_parser = subparsers.add_parser("image", help="Run ReadImage on an image file.")
    image_parser.add_argument("path")

    glob_parser = subparsers.add_parser("glob", help="Run Glob to find local files or directories.")
    glob_parser.add_argument("pattern")
    glob_parser.add_argument("--path", default=".")
    glob_parser.add_argument("--include-dirs", action="store_true")
    glob_parser.add_argument("--max-results", type=int, default=DEFAULT_GLOB_MAX_RESULTS)

    grep_parser = subparsers.add_parser("grep", help="Run Grep to search local text files.")
    grep_parser.add_argument("pattern")
    grep_parser.add_argument("--path", default=".")
    grep_parser.add_argument("--glob", default="**/*")
    grep_parser.add_argument("--case-sensitive", action="store_true")
    grep_parser.add_argument("--max-results", type=int, default=DEFAULT_GREP_MAX_RESULTS)
    grep_parser.add_argument("--max-chars", type=int, default=DEFAULT_GREP_MAX_CHARS)

    write_parser = subparsers.add_parser("write", help="Run Write on a text file.")
    write_parser.add_argument("path")
    write_parser.add_argument("content")
    write_parser.add_argument("--overwrite", action="store_true")

    edit_parser = subparsers.add_parser("edit", help="Run Edit on a text file.")
    edit_parser.add_argument("path")
    edit_parser.add_argument("patch")

    parser.add_argument("--workspace-root", help="Optional workspace root override.")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    workspace_root = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else None

    if args.tool == "read":
        result = Read().call(
            {
                "path": args.path,
                "start_line": args.start_line,
                "end_line": args.end_line,
                "max_chars": args.max_chars,
            },
            workspace_root=workspace_root,
        )
    elif args.tool == "pdf":
        result = ReadPDF().call({"path": args.path, "max_chars": args.max_chars}, workspace_root=workspace_root)
    elif args.tool == "image":
        result = ReadImage().call({"path": args.path}, workspace_root=workspace_root)
    elif args.tool == "glob":
        result = Glob().call(
            {
                "pattern": args.pattern,
                "path": args.path,
                "include_dirs": args.include_dirs,
                "max_results": args.max_results,
            },
            workspace_root=workspace_root,
        )
    elif args.tool == "grep":
        result = Grep().call(
            {
                "pattern": args.pattern,
                "path": args.path,
                "glob": args.glob,
                "case_sensitive": args.case_sensitive,
                "max_results": args.max_results,
                "max_chars": args.max_chars,
            },
            workspace_root=workspace_root,
        )
    elif args.tool == "write":
        result = Write().call(
            {"path": args.path, "content": args.content, "overwrite": args.overwrite},
            workspace_root=workspace_root,
        )
    else:
        result = Edit().call({"path": args.path, "patch": args.patch}, workspace_root=workspace_root)

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
