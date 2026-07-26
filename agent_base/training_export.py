"""Immutable turn-level training export for single-agent ResearchHarness runs."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence


UNIT_SCHEMA = "researchharness.turn-training-unit.v1"
SUMMARY_SCHEMA = "researchharness.rollout-summary.v1"
MANIFEST_SCHEMA = "researchharness.turn-training-export-manifest.v1"
EXPORT_DIR_NAME = "turn-training-export-v1"
CANONICALIZATION = "canonical-json-v1"
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROHIBITED_AGENT_KEYS = {
    "subagent",
    "agent_id",
    "parent_agent_id",
    "parent_run_id",
    "delegation_id",
}
_ORACLE_KEYS = {
    "gold_patch",
    "hidden_test",
    "hidden_tests",
    "verifier_internal",
    "verifier_internals",
    "resolved",
    "reward",
    "reference_answer",
    "oracle",
}
_ORACLE_PATTERNS = (
    re.compile(r"\bgold[ _-]*patch\b", re.IGNORECASE),
    re.compile(r"\bhidden[ _-]*(?:tests?|verifier)\b", re.IGNORECASE),
    re.compile(r"\bverifier[ _-]*internals?\b", re.IGNORECASE),
    re.compile(r"\bfinal[ _-]*(?:resolved|reward)\b", re.IGNORECASE),
    re.compile(r"\bother[ _-]*rollout(?:'s)?[ _-]*(?:answer|output)\b", re.IGNORECASE),
)
_VALIDATOR_NAMES = (
    "TRACE_CAPTURE_CLOSURE",
    "EXACT_REQUEST_MATCH",
    "EXACT_RESPONSE_MATCH",
    "TOOL_CALL_STRUCTURE",
    "COMPACTION_CLOSURE",
    "IDENTITY_JOIN",
    "HASH_INTEGRITY",
    "NO_MULTI_AGENT_V1",
    "ORACLE_LEAK_SCAN",
)


@dataclass(frozen=True, slots=True)
class TrainingExportConfig:
    output_dir: Path
    task_id: str
    task_revision_id: str
    rollout_id: str
    rollout_index: int
    run_id: str
    harness_revision: str
    scaffold_version: str = ""
    tool_protocol_version: str = "openai-chat-completions-native-tools-v1"
    tokenizer_revision: str | None = None
    final_patch_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if self.output_dir.name != EXPORT_DIR_NAME:
            raise ValueError(f"output_dir must end with {EXPORT_DIR_NAME!r}")
        if self.final_patch_path is not None:
            object.__setattr__(self, "final_patch_path", Path(self.final_patch_path))
        for label in (
            "task_id",
            "task_revision_id",
            "rollout_id",
            "run_id",
            "harness_revision",
        ):
            value = str(getattr(self, label))
            if _IDENTITY_RE.fullmatch(value) is None:
                raise ValueError(f"{label} is not a safe, non-empty identity")
        if isinstance(self.rollout_index, bool) or not isinstance(self.rollout_index, int) or self.rollout_index < 0:
            raise ValueError("rollout_index must be a non-negative integer")

    def identity(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_revision_id": self.task_revision_id,
            "rollout_id": self.rollout_id,
            "rollout_index": self.rollout_index,
            "run_id": self.run_id,
        }


def canonical_json_bytes(value: Any) -> bytes:
    _reject_nonfinite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical-json-v1 rejects NaN and Infinity")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical-json-v1 object keys must be strings")
            _reject_nonfinite(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_nonfinite(nested)


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_json(domain: str, value: Any, *, prefixed: bool = True) -> str:
    digest = _hash_bytes(
        canonical_json_bytes({"domain": domain, "payload": value})
    )
    return f"sha256:{digest}" if prefixed else digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, canonical_json_bytes(value) + b"\n")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _read_trace(path: Path) -> list[dict[str, Any]]:
    _require_regular_source(path, label="trace")
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError(f"trace line {line_number} is not an object")
        rows.append(value)
    return rows


def _require_regular_source(path: Path, *, label: str) -> Path:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{label} must be an ordinary file")
    return source


def _safe_ref_path(raw_path: object) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise ValueError("artifact ref path must be non-empty canonical POSIX text")
    path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw_path
    ):
        raise ValueError("artifact ref path escapes the export bundle")
    return path


def _file_ref(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    _safe_ref_path(relative)
    return {"path": relative, "sha256": _sha256_file(path)}


def _resolve_ref(root: Path, value: object) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ValueError("artifact ref must contain exactly path and sha256")
    relative = _safe_ref_path(value.get("path"))
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("artifact ref sha256 is invalid")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("artifact ref target must be one ordinary, non-hardlinked file")
    if _sha256_file(path) != digest:
        raise ValueError("artifact ref digest mismatch")
    return path


def _artifact_json(root: Path, relative: str, value: Any) -> dict[str, Any]:
    path = root / relative
    _write_json(path, value)
    return _file_ref(root, path)


def _oracle_findings(value: Any, *, location: str = "request") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in _ORACLE_KEYS:
                findings.append(f"{location}.{key}:forbidden-key")
            findings.extend(_oracle_findings(str(key), location=f"{location}.{key}"))
            findings.extend(_oracle_findings(nested, location=f"{location}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            findings.extend(_oracle_findings(nested, location=f"{location}[{index}]"))
    elif isinstance(value, str):
        for pattern in _ORACLE_PATTERNS:
            if pattern.search(value):
                findings.append(f"{location}:{pattern.pattern}")
    return sorted(set(findings))


def _provider_request(payload: Mapping[str, Any], *, compaction: bool = False) -> dict[str, Any]:
    if compaction:
        response = payload.get("summary_response")
        if isinstance(response, Mapping) and isinstance(response.get("provider_request"), Mapping):
            return dict(response["provider_request"])
        return {}
    if isinstance(payload.get("provider_request"), Mapping):
        return dict(payload["provider_request"])
    return {}


def _normalized_request(provider_request: Mapping[str, Any]) -> dict[str, Any]:
    sampling: dict[str, Any] = {}
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("presence_penalty", "presence_penalty"),
        ("max_tokens", "max_output_tokens"),
        ("max_completion_tokens", "max_output_tokens"),
    ):
        if source in provider_request:
            sampling[target] = provider_request[source]
    reserved = {
        "model",
        "messages",
        "tools",
        "temperature",
        "top_p",
        "presence_penalty",
        "max_tokens",
        "max_completion_tokens",
    }
    provider_fields = {
        key: value for key, value in provider_request.items() if key not in reserved
    }
    return {
        "model": provider_request.get("model", ""),
        "messages": provider_request.get("messages", []),
        "tools": provider_request.get("tools", []),
        "sampling": sampling,
        "provider_fields": provider_fields,
    }


def _raw_response(payload: Mapping[str, Any], *, compaction: bool = False) -> dict[str, Any]:
    if compaction:
        response = payload.get("summary_response")
        if isinstance(response, Mapping):
            raw = response.get("raw_provider_response")
            return dict(raw) if isinstance(raw, Mapping) else {}
        return {}
    raw = payload.get("raw_provider_response")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _response_source(payload: Mapping[str, Any], *, compaction: bool = False) -> dict[str, Any]:
    value = payload.get("summary_response" if compaction else "response")
    return dict(value) if isinstance(value, Mapping) else {}


def _normalized_response(response: Mapping[str, Any]) -> dict[str, Any]:
    tool_calls = response.get("tool_calls")
    if not isinstance(tool_calls, list):
        tool_calls = []
    content = response.get("content")
    if content is None:
        content = ""
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "reasoning_content": response.get("reasoning_content"),
            "tool_calls": tool_calls,
        },
        "finish_reason": response.get("finish_reason"),
        "usage": response.get("usage") if isinstance(response.get("usage"), Mapping) else {},
        "provider_status": response.get("status", "error"),
        **({"error": response.get("error")} if response.get("error") else {}),
    }


def _base_provenance(config: TrainingExportConfig, payload: Mapping[str, Any], findings: list[str]) -> dict[str, Any]:
    response = payload.get("response")
    if not isinstance(response, Mapping):
        response = payload.get("summary_response") if isinstance(payload.get("summary_response"), Mapping) else {}
    return {
        "harness_revision": config.harness_revision,
        "scaffold_version": config.scaffold_version,
        "tool_protocol_version": config.tool_protocol_version,
        "requested_model": payload.get("requested_model") or response.get("requested_model") or payload.get("model_name"),
        "provider_model": payload.get("provider_model") or response.get("provider_model"),
        "model_revision": None,
        "tokenizer_revision": config.tokenizer_revision,
        "provider_retry_count": int(payload.get("provider_retry_count", response.get("provider_retry_count", 0)) or 0),
        "oracle_isolation": {
            "status": "leak" if findings else "clear",
            "findings": findings,
        },
    }


def _finish_unit(unit: dict[str, Any]) -> dict[str, Any]:
    request_sha = _hash_json("researchharness.request.v1", unit["request"])
    response_sha = _hash_json("researchharness.response.v1", unit["response"])
    identity_preimage = {
        key: unit[key]
        for key in (
            "unit_type",
            "task_id",
            "task_revision_id",
            "rollout_id",
            "rollout_index",
            "run_id",
            "turn_index",
        )
    }
    identity_preimage.update(
        {
            "request_sha256": request_sha,
            "response_sha256": response_sha,
            "trace_event_index": unit["source"]["trace_event_index"],
        }
    )
    unit["unit_id"] = _hash_json("researchharness.unit-id.v1", identity_preimage)
    unit["integrity"] = {
        "canonicalization": CANONICALIZATION,
        "request_sha256": request_sha,
        "response_sha256": response_sha,
    }
    unit["integrity"]["unit_payload_sha256"] = _hash_json(
        "researchharness.unit-payload.v1", unit
    )
    return unit


def _unit_common(config: TrainingExportConfig, row: Mapping[str, Any], unit_type: str) -> dict[str, Any]:
    return {
        "schema_version": UNIT_SCHEMA,
        "unit_type": unit_type,
        **config.identity(),
        "turn_index": int(row.get("turn_index", 0) or 0),
    }


def _build_main_unit(config: TrainingExportConfig, root: Path, row: Mapping[str, Any], trace_ref: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    provider_request = _provider_request(payload)
    raw_response = _raw_response(payload)
    event_index = int(row.get("event_index", 0) or 0)
    request_ref = _artifact_json(root, f"artifacts/provider/main-{event_index:06d}.request.json", provider_request)
    raw_ref = _artifact_json(root, f"artifacts/provider/main-{event_index:06d}.response.json", raw_response)
    request = _normalized_request(provider_request)
    response_source = _response_source(payload)
    response = _normalized_response(response_source)
    findings = _oracle_findings(provider_request)
    unit = _unit_common(config, row, "main_agent_turn")
    unit.update(
        {
            "capture_status": "ok" if response_source.get("status") == "ok" and not findings else "error",
            "request": request,
            "response": response,
            "source": {
                "trace_ref": dict(trace_ref),
                "trace_event_index": event_index,
                "capture_type": "llm_call",
                "provider_request_ref": request_ref,
                "raw_provider_response_ref": raw_ref,
            },
            "provenance": _base_provenance(config, payload, findings),
        }
    )
    return _finish_unit(unit)


def _build_compaction_unit(config: TrainingExportConfig, root: Path, row: Mapping[str, Any], trace_ref: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    event_index = int(row.get("event_index", 0) or 0)
    provider_request = _provider_request(payload, compaction=True)
    raw_response = _raw_response(payload, compaction=True)
    request_ref = _artifact_json(root, f"artifacts/provider/compaction-{event_index:06d}.request.json", provider_request)
    raw_ref = _artifact_json(root, f"artifacts/provider/compaction-{event_index:06d}.response.json", raw_response)
    pre_ref = _artifact_json(root, f"artifacts/compaction/{event_index:06d}.pre.json", payload.get("pre_messages", []))
    post_ref = _artifact_json(root, f"artifacts/compaction/{event_index:06d}.post.json", payload.get("post_messages", []))
    response_source = _response_source(payload, compaction=True)
    response = _normalized_response(response_source)
    findings = _oracle_findings(provider_request)
    summary_text = str(payload.get("summary_text", "") or "").strip()
    successful = (
        payload.get("status") == "ok"
        and response_source.get("status") == "ok"
        and bool(summary_text)
        and not response["message"]["tool_calls"]
        and not findings
    )
    existing_memory = str(payload.get("existing_memory_text", "") or "")
    unit = _unit_common(config, row, "memory_compaction")
    unit.update(
        {
            "capture_status": "ok" if successful else "error",
            "request": _normalized_request(provider_request),
            "response": response,
            "compaction": {
                "trigger_reason": payload.get("trigger_reason", ""),
                "prior_token_estimate": int(payload.get("prior_token_estimate", 0) or 0),
                "new_token_estimate": int(payload.get("new_token_estimate", 0) or 0),
                "compacted_group_count": int(payload.get("compacted_group_count", 0) or 0),
                "kept_group_count": int(payload.get("kept_group_count", 0) or 0),
                "existing_memory_text_sha256": "sha256:" + _hash_bytes(existing_memory.encode("utf-8")),
                "pre_messages_ref": pre_ref,
                "post_messages_ref": post_ref,
            },
            "source": {
                "trace_ref": dict(trace_ref),
                "trace_event_index": event_index,
                "capture_type": "compaction",
                "provider_request_ref": request_ref,
                "raw_provider_response_ref": raw_ref,
            },
            "provenance": _base_provenance(config, payload, findings),
        }
    )
    return _finish_unit(unit)


def _termination(value: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, str):
        reason = value
        submitted = value == "result"
        return {"status": "completed" if submitted else "stopped", "reason": reason, "submitted": submitted, "clean_termination": submitted}
    reason = str(value.get("reason", value.get("termination", "")))
    submitted = bool(value.get("submitted", reason == "result"))
    return {
        "status": str(value.get("status", "completed" if submitted else "stopped")),
        "reason": reason,
        "submitted": submitted,
        "clean_termination": bool(value.get("clean_termination", submitted)),
    }


def _all_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError("export bundle may not contain symlinks")
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError("export bundle may not contain hardlinked files")
            _safe_ref_path(path.relative_to(root).as_posix())
            paths.append(path)
        elif not path.is_dir():
            raise ValueError("export bundle contains a non-ordinary entry")
    return paths


def _copy_source(source: Path, destination: Path) -> None:
    _require_regular_source(source, label="source artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def export_turn_training_bundle(
    config: TrainingExportConfig,
    *,
    trace_path: Path,
    termination: Mapping[str, Any] | str,
) -> Path:
    if not isinstance(config, TrainingExportConfig):
        raise TypeError("config must be TrainingExportConfig")
    trace_source = _require_regular_source(Path(trace_path), label="trace")
    target = config.output_dir
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"immutable export already exists: {target}")
    lock_path = parent / f".{target.name}.publish.lock"
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=parent))
    try:
        os.close(lock_fd)
        trace_copy = temp / "artifacts" / "trace" / trace_source.name
        _copy_source(trace_source, trace_copy)
        trace_ref = _file_ref(temp, trace_copy)
        rows = _read_trace(trace_copy)
        if any(row.get("run_id") != config.run_id for row in rows):
            raise ValueError("trace run_id differs from frozen training identity")
        units: list[dict[str, Any]] = []
        for row in rows:
            capture_type = row.get("capture_type")
            if capture_type == "llm_call":
                units.append(_build_main_unit(config, temp, row, trace_ref))
            elif capture_type == "compaction":
                units.append(_build_compaction_unit(config, temp, row, trace_ref))
        units_path = temp / "turn-training-units.v1.jsonl"
        _write_bytes(units_path, b"".join(canonical_json_bytes(unit) + b"\n" for unit in units))

        final_patch_ref: dict[str, Any] | None = None
        if config.final_patch_path is not None:
            patch_path = temp / "agent.patch"
            _copy_source(config.final_patch_path, patch_path)
            final_patch_ref = _file_ref(temp, patch_path)
        summary = {
            "schema_version": SUMMARY_SCHEMA,
            **config.identity(),
            "main_agent_turn_count": sum(unit["unit_type"] == "main_agent_turn" for unit in units),
            "memory_compaction_count": sum(unit["unit_type"] == "memory_compaction" for unit in units),
            "termination": _termination(termination),
            "final_patch_ref": final_patch_ref,
            "trace_ref": trace_ref,
        }
        _write_json(temp / "rollout-summary.v1.json", summary)

        payload_files = [
            path for path in _all_files(temp) if path.name not in {"manifest.json", "SHA256SUMS"}
        ]
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            **config.identity(),
            "canonicalization": CANONICALIZATION,
            "validators": [{"name": name, "status": "pass"} for name in _VALIDATOR_NAMES],
            "oracle_leak_unit_count": sum(
                unit["provenance"]["oracle_isolation"]["status"] == "leak" for unit in units
            ),
            "files": [_file_ref(temp, path) for path in payload_files],
        }
        _write_json(temp / "manifest.json", manifest)
        checksum_files = [path for path in _all_files(temp) if path.name != "SHA256SUMS"]
        checksum_text = "".join(
            f"{_sha256_file(path)}  {path.relative_to(temp).as_posix()}\n"
            for path in checksum_files
        )
        _write_bytes(temp / "SHA256SUMS", checksum_text.encode("utf-8"))
        validate_turn_training_bundle(temp)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"immutable export appeared during publish: {target}")
        os.rename(temp, target)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _walk_refs(value: Any) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if set(value) == {"path", "sha256"}:
            refs.append(value)
        else:
            for nested in value.values():
                refs.extend(_walk_refs(nested))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            refs.extend(_walk_refs(nested))
    return refs


def _contains_prohibited_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(set(value) & _PROHIBITED_AGENT_KEYS) or any(_contains_prohibited_key(nested) for nested in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_prohibited_key(nested) for nested in value)
    return False


def _load_units(path: Path) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.strip():
            continue
        if not line.endswith(b"\n"):
            raise ValueError("training JSONL line lacks trailing newline")
        value = json.loads(line)
        if not isinstance(value, dict) or line != canonical_json_bytes(value) + b"\n":
            raise ValueError("training JSONL is not canonical-json-v1")
        units.append(value)
    return units


def validate_turn_training_bundle(bundle_path: Path) -> dict[str, Any]:
    root = Path(bundle_path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("training export must be an ordinary directory")
    files = _all_files(root)
    relative_files = {path.relative_to(root).as_posix(): path for path in files}
    required = {
        "turn-training-units.v1.jsonl",
        "rollout-summary.v1.json",
        "manifest.json",
        "SHA256SUMS",
    }
    if not required <= set(relative_files):
        raise ValueError("training export is incomplete")
    checksum_lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    observed_checksums: dict[str, str] = {}
    for line in checksum_lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise ValueError("SHA256SUMS line is malformed")
        digest, raw_path = line[:64], line[66:]
        path = _safe_ref_path(raw_path).as_posix()
        if _SHA256_RE.fullmatch(digest) is None or path in observed_checksums:
            raise ValueError("SHA256SUMS contains invalid or duplicate entries")
        observed_checksums[path] = digest
    expected_checksum_paths = set(relative_files) - {"SHA256SUMS"}
    if set(observed_checksums) != expected_checksum_paths:
        raise ValueError("SHA256SUMS file set is not exact")
    for relative, digest in observed_checksums.items():
        if _sha256_file(relative_files[relative]) != digest:
            raise ValueError("SHA256SUMS digest mismatch")

    manifest = _read_json(root / "manifest.json")
    summary = _read_json(root / "rollout-summary.v1.json")
    units = _load_units(root / "turn-training-units.v1.jsonl")
    if manifest.get("schema_version") != MANIFEST_SCHEMA or summary.get("schema_version") != SUMMARY_SCHEMA:
        raise ValueError("training export schema mismatch")
    manifest_refs = manifest.get("files")
    if not isinstance(manifest_refs, list):
        raise ValueError("manifest files must be a list")
    manifest_paths: list[str] = []
    for ref in manifest_refs:
        path = _resolve_ref(root, ref)
        manifest_paths.append(path.relative_to(root).as_posix())
    expected_manifest_paths = set(relative_files) - {"manifest.json", "SHA256SUMS"}
    if len(manifest_paths) != len(set(manifest_paths)) or set(manifest_paths) != expected_manifest_paths:
        raise ValueError("manifest file closure is not exact")
    identity_fields = ("task_id", "task_revision_id", "rollout_id", "rollout_index", "run_id")
    identity = {key: summary.get(key) for key in identity_fields}
    if any(manifest.get(key) != value for key, value in identity.items()):
        raise ValueError("manifest identity differs from summary")

    trace_path = _resolve_ref(root, summary.get("trace_ref"))
    trace_rows = _read_trace(trace_path)
    captures = [row for row in trace_rows if row.get("capture_type") in {"llm_call", "compaction"}]
    if len(captures) != len(units):
        raise ValueError("trace capture closure differs from unit count")
    capture_by_event = {int(row.get("event_index", 0) or 0): row for row in captures}
    if len(capture_by_event) != len(captures):
        raise ValueError("trace capture event identities are not unique")
    for unit in units:
        if unit.get("schema_version") != UNIT_SCHEMA or unit.get("unit_type") not in {"main_agent_turn", "memory_compaction"}:
            raise ValueError("training unit schema/type is invalid")
        if _contains_prohibited_key(unit):
            raise ValueError("V1 unit contains prohibited multi-agent fields")
        if any(unit.get(key) != value for key, value in identity.items()):
            raise ValueError("training unit identity differs from summary")
        for ref in _walk_refs(unit):
            _resolve_ref(root, ref)
        event_index = unit.get("source", {}).get("trace_event_index")
        row = capture_by_event.get(event_index)
        expected_capture = "llm_call" if unit.get("unit_type") == "main_agent_turn" else "compaction"
        if row is None or row.get("capture_type") != expected_capture:
            raise ValueError("training unit source does not bind its trace capture")
        integrity = unit.get("integrity")
        if not isinstance(integrity, dict) or integrity.get("canonicalization") != CANONICALIZATION:
            raise ValueError("training unit integrity is invalid")
        if integrity.get("request_sha256") != _hash_json("researchharness.request.v1", unit.get("request")):
            raise ValueError("training unit request digest mismatch")
        if integrity.get("response_sha256") != _hash_json("researchharness.response.v1", unit.get("response")):
            raise ValueError("training unit response digest mismatch")
        payload_digest = integrity.get("unit_payload_sha256")
        payload_copy = json.loads(json.dumps(unit, ensure_ascii=False))
        payload_copy["integrity"].pop("unit_payload_sha256", None)
        if payload_digest != _hash_json("researchharness.unit-payload.v1", payload_copy):
            raise ValueError("training unit payload digest mismatch")
        identity_preimage = {
            key: unit[key]
            for key in (
                "unit_type",
                "task_id",
                "task_revision_id",
                "rollout_id",
                "rollout_index",
                "run_id",
                "turn_index",
            )
        }
        identity_preimage.update(
            {
                "request_sha256": integrity["request_sha256"],
                "response_sha256": integrity["response_sha256"],
                "trace_event_index": event_index,
            }
        )
        if unit.get("unit_id") != _hash_json("researchharness.unit-id.v1", identity_preimage):
            raise ValueError("training unit_id mismatch")
        provider_request = _read_json(_resolve_ref(root, unit["source"]["provider_request_ref"]))
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        expected_request = _provider_request(payload, compaction=expected_capture == "compaction")
        if provider_request != expected_request or unit.get("request") != _normalized_request(provider_request):
            raise ValueError("exported request differs from provider-boundary trace")
        raw_response = _read_json(_resolve_ref(root, unit["source"]["raw_provider_response_ref"]))
        if raw_response != _raw_response(payload, compaction=expected_capture == "compaction"):
            raise ValueError("raw provider response differs from trace")
        response_source = _response_source(payload, compaction=expected_capture == "compaction")
        if unit.get("response") != _normalized_response(response_source):
            raise ValueError("exported response differs from accepted trace response")
        findings = _oracle_findings(provider_request)
        oracle = unit.get("provenance", {}).get("oracle_isolation", {})
        if oracle != {"status": "leak" if findings else "clear", "findings": findings}:
            raise ValueError("oracle leak marker differs from model-visible request")
        if findings and unit.get("capture_status") != "error":
            raise ValueError("oracle-leaking unit is not fail-closed")
        if expected_capture == "compaction":
            compaction = unit.get("compaction")
            if not isinstance(compaction, Mapping):
                raise ValueError("memory compaction unit lacks compaction metadata")
            pre = json.loads(_resolve_ref(root, compaction.get("pre_messages_ref")).read_text(encoding="utf-8"))
            post = json.loads(_resolve_ref(root, compaction.get("post_messages_ref")).read_text(encoding="utf-8"))
            if pre != payload.get("pre_messages", []) or post != payload.get("post_messages", []):
                raise ValueError("compaction pre/post artifact differs from trace")

    if summary.get("main_agent_turn_count") != sum(unit.get("unit_type") == "main_agent_turn" for unit in units):
        raise ValueError("rollout summary main-agent count mismatch")
    if summary.get("memory_compaction_count") != sum(unit.get("unit_type") == "memory_compaction" for unit in units):
        raise ValueError("rollout summary compaction count mismatch")
    validators = manifest.get("validators")
    expected_validators = [{"name": name, "status": "pass"} for name in _VALIDATOR_NAMES]
    if validators != expected_validators:
        raise ValueError("manifest validator closure is not canonical")
    leak_count = sum(unit.get("provenance", {}).get("oracle_isolation", {}).get("status") == "leak" for unit in units)
    if manifest.get("oracle_leak_unit_count") != leak_count:
        raise ValueError("manifest oracle leak count mismatch")
    return summary


__all__ = [
    "EXPORT_DIR_NAME",
    "TrainingExportConfig",
    "canonical_json_bytes",
    "export_turn_training_bundle",
    "validate_turn_training_bundle",
]
