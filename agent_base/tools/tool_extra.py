from __future__ import annotations

import json
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union
from xml.etree import ElementTree

from agent_base.tools.tool_file import DEFAULT_LOCAL_MAX_CHARS, ReadPDF
from agent_base.tools.tooling import ToolBase, validate_tool_path


DEFAULT_EDITOR_MAX_CHARS = DEFAULT_LOCAL_MAX_CHARS
BINARY_MARKDOWN_SUFFIXES = {".xlsx", ".pptx", ".wav", ".mp3", ".m4a", ".flac", ".pdf", ".docx"}


@dataclass
class _UndoRecord:
    existed: bool
    previous_text: str


class StrReplaceEditor(ToolBase):
    name = "str_replace_editor"
    description = (
        "Optional compatibility editor for viewing, creating, exact string replacement, "
        "line insertion, and undoing the last edit to a workspace text file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Allowed commands: view, create, str_replace, insert, undo_edit.",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
            },
            "path": {
                "type": "string",
                "description": "Absolute path to a file or directory inside the workspace.",
            },
            "file_text": {
                "type": "string",
                "description": "Required for create; full text content of the file to create.",
            },
            "old_str": {
                "type": "string",
                "description": "Required for str_replace; exact unique string to replace.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text for str_replace, or text block for insert.",
            },
            "insert_line": {
                "type": "integer",
                "description": (
                    "Required for insert; insert new_str after this 1-based line. "
                    "Use 0 to insert at the start."
                ),
            },
            "view_range": {
                "type": "array",
                "description": "Optional [start_line, end_line] for file view. Use [start_line, -1] to read to EOF.",
                "items": {"type": "integer"},
            },
        },
        "required": ["command", "path"],
        "additionalProperties": False,
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)
        self._undo_records: dict[Path, _UndoRecord] = {}
        self._lock = threading.Lock()

    def call(self, params: Union[str, dict], **kwargs: Any) -> str:
        try:
            parsed = self.parse_json_args(params)
            command = str(parsed.get("command", "")).strip()
            path = self._resolve_absolute_path(
                str(parsed.get("path", "")),
                workspace_root=kwargs.get("workspace_root"),
            )
        except ValueError as exc:
            return f"[str_replace_editor] {exc}"

        if command == "view":
            return self._view(path, parsed, workspace_root=kwargs.get("workspace_root"))
        if command == "create":
            return self._create(path, parsed)
        if command == "str_replace":
            return self._str_replace(path, parsed)
        if command == "insert":
            return self._insert(path, parsed)
        if command == "undo_edit":
            return self._undo_edit(path)
        return "[str_replace_editor] command must be one of: view, create, str_replace, insert, undo_edit."

    def _resolve_absolute_path(self, path_value: str, *, workspace_root: Any) -> Path:
        path_text = str(path_value or "").strip()
        if not path_text:
            raise ValueError("path must be a non-empty absolute path.")
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute.")
        return validate_tool_path(path, "str_replace_editor access", base_root=workspace_root)

    def _view(self, path: Path, params: dict[str, Any], *, workspace_root: Any) -> str:
        if not path.exists():
            return f"[str_replace_editor] Path not found: {path}"
        if path.is_dir():
            return self._clip(self._view_directory(path))
        if not path.is_file():
            return f"[str_replace_editor] Path is neither a file nor a directory: {path}"
        if path.suffix.lower() in BINARY_MARKDOWN_SUFFIXES:
            return self._clip(self._view_binary_markdown(path, workspace_root=workspace_root))
        try:
            text = self._read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            return f"[str_replace_editor] Error reading text file: {exc}"
        try:
            selected, start_line, end_line = self._select_view_range(text, params.get("view_range"))
        except ValueError as exc:
            return f"[str_replace_editor] {exc}"
        numbered = self._number_lines(selected, start_line=start_line)
        header = {
            "path": str(path),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(text.splitlines()),
        }
        return self._clip(json.dumps(header, ensure_ascii=False) + "\n" + numbered)

    def _create(self, path: Path, params: dict[str, Any]) -> str:
        if "file_text" not in params:
            return "[str_replace_editor] Missing required parameter for create: file_text"
        if path.exists():
            return f"[str_replace_editor] Cannot create because file already exists: {path}"
        if not path.parent.exists() or not path.parent.is_dir():
            return f"[str_replace_editor] Parent directory does not exist: {path.parent}"
        file_text = str(params.get("file_text", ""))
        try:
            path.write_text(file_text, encoding="utf-8")
        except OSError as exc:
            return f"[str_replace_editor] Error creating file: {exc}"
        with self._lock:
            self._undo_records[path] = _UndoRecord(existed=False, previous_text="")
        return f"[str_replace_editor] Created file: {path}"

    def _str_replace(self, path: Path, params: dict[str, Any]) -> str:
        if "old_str" not in params:
            return "[str_replace_editor] Missing required parameter for str_replace: old_str"
        old_str = str(params.get("old_str", ""))
        new_str = str(params.get("new_str", ""))
        if not old_str:
            return "[str_replace_editor] old_str must be non-empty."
        if old_str == new_str:
            return "[str_replace_editor] old_str and new_str must be different."
        try:
            original = self._read_existing_file(path)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            return f"[str_replace_editor] {exc}"
        match_count = original.count(old_str)
        if match_count == 0:
            return "[str_replace_editor] old_str was not found exactly once; found 0 matches."
        if match_count > 1:
            return f"[str_replace_editor] old_str must be unique; found {match_count} matches."
        updated = original.replace(old_str, new_str, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"[str_replace_editor] Error writing file: {exc}"
        with self._lock:
            self._undo_records[path] = _UndoRecord(existed=True, previous_text=original)
        return f"[str_replace_editor] Replaced text in file: {path}"

    def _insert(self, path: Path, params: dict[str, Any]) -> str:
        if "insert_line" not in params:
            return "[str_replace_editor] Missing required parameter for insert: insert_line"
        if "new_str" not in params:
            return "[str_replace_editor] Missing required parameter for insert: new_str"
        try:
            insert_line = int(params.get("insert_line"))
        except (TypeError, ValueError):
            return "[str_replace_editor] insert_line must be an integer."
        new_str = str(params.get("new_str", ""))
        try:
            original = self._read_existing_file(path)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            return f"[str_replace_editor] {exc}"
        lines = original.splitlines(keepends=True)
        if insert_line < 0 or insert_line > len(lines):
            return f"[str_replace_editor] insert_line must be between 0 and {len(lines)}."
        insert_text = new_str if new_str.endswith("\n") else new_str + "\n"
        updated = "".join(lines[:insert_line]) + insert_text + "".join(lines[insert_line:])
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"[str_replace_editor] Error writing file: {exc}"
        with self._lock:
            self._undo_records[path] = _UndoRecord(existed=True, previous_text=original)
        return f"[str_replace_editor] Inserted text after line {insert_line} in file: {path}"

    def _undo_edit(self, path: Path) -> str:
        with self._lock:
            record = self._undo_records.pop(path, None)
        if record is None:
            return f"[str_replace_editor] No edit to undo for file: {path}"
        try:
            if record.existed:
                path.write_text(record.previous_text, encoding="utf-8")
                return f"[str_replace_editor] Reverted last edit to file: {path}"
            if path.exists():
                path.unlink()
            return f"[str_replace_editor] Removed file created by last edit: {path}"
        except OSError as exc:
            return f"[str_replace_editor] Error undoing edit: {exc}"

    def _read_existing_file(self, path: Path) -> str:
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        if not path.is_file():
            raise ValueError(f"Path is not a file: {path}")
        if path.suffix.lower() in BINARY_MARKDOWN_SUFFIXES:
            raise ValueError(f"Editing binary/derived formats is not supported: {path}")
        return self._read_text(path)

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _select_view_range(self, text: str, view_range: Any) -> tuple[list[str], int, int]:
        lines = text.splitlines()
        if view_range is None:
            return lines, 1, len(lines)
        if not isinstance(view_range, list) or len(view_range) != 2:
            raise ValueError("view_range must be [start_line, end_line].")
        try:
            start_line = int(view_range[0])
            end_line = int(view_range[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("view_range entries must be integers.") from exc
        if start_line < 1:
            raise ValueError("view_range start_line must be >= 1.")
        if end_line != -1 and end_line < start_line:
            raise ValueError("view_range end_line must be -1 or >= start_line.")
        resolved_end = len(lines) if end_line == -1 else min(end_line, len(lines))
        return lines[start_line - 1:resolved_end], start_line, resolved_end

    def _number_lines(self, lines: list[str], *, start_line: int) -> str:
        return "\n".join(f"{line_number:6}\t{line}" for line_number, line in enumerate(lines, start=start_line))

    def _view_directory(self, path: Path) -> str:
        rows = [str(path)]
        for child in self._iter_non_hidden(path):
            rel = child.relative_to(path)
            rows.append(f"{rel}/" if child.is_dir() else str(rel))
            if child.is_dir():
                for grandchild in self._iter_non_hidden(child):
                    grand_rel = grandchild.relative_to(path)
                    rows.append(f"{grand_rel}/" if grandchild.is_dir() else str(grand_rel))
        return "\n".join(rows)

    def _iter_non_hidden(self, path: Path) -> list[Path]:
        try:
            return sorted(
                (child for child in path.iterdir() if not child.name.startswith(".")),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            return []

    def _view_binary_markdown(self, path: Path, *, workspace_root: Any) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return ReadPDF().call(
                {"path": str(path), "max_chars": DEFAULT_EDITOR_MAX_CHARS},
                workspace_root=workspace_root,
            )
        if suffix == ".docx":
            return self._extract_docx_text(path)
        if suffix == ".pptx":
            return self._extract_pptx_text(path)
        if suffix == ".xlsx":
            return self._extract_xlsx_text(path)
        stat = path.stat()
        return f"# {path.name}\n\nBinary media file.\n\n- path: {path}\n- size_bytes: {stat.st_size}"

    def _extract_docx_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml")
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            return f"[str_replace_editor] Error reading DOCX: {exc}"
        try:
            text = self._xml_text(xml)
        except ElementTree.ParseError as exc:
            return f"[str_replace_editor] Error reading DOCX XML: {exc}"
        return f"# {path.name}\n\n" + text

    def _extract_pptx_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                slide_names = sorted(
                    name
                    for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                )
                sections = []
                for index, name in enumerate(slide_names, start=1):
                    text = self._xml_text(archive.read(name)).strip()
                    if text:
                        sections.append(f"## Slide {index}\n\n{text}")
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            return f"[str_replace_editor] Error reading PPTX: {exc}"
        return f"# {path.name}\n\n" + "\n\n".join(sections)

    def _extract_xlsx_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                names = sorted(
                    name
                    for name in archive.namelist()
                    if name.startswith("xl/worksheets/") and name.endswith(".xml")
                )
                sections = []
                for index, name in enumerate(names, start=1):
                    text = self._xml_text(archive.read(name)).strip()
                    if text:
                        sections.append(f"## Sheet {index}\n\n{text}")
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            return f"[str_replace_editor] Error reading XLSX: {exc}"
        return f"# {path.name}\n\n" + "\n\n".join(sections)

    def _xml_text(self, xml_bytes: bytes) -> str:
        root = ElementTree.fromstring(xml_bytes)
        values = [text.strip() for text in root.itertext() if text and text.strip()]
        return "\n".join(values)

    def _clip(self, text: str, max_chars: int = DEFAULT_EDITOR_MAX_CHARS) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n<response clipped>"
