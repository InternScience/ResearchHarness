"""Immutable turn-level training export for single-agent ResearchHarness runs."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence


UNIT_SCHEMA = "researchharness.turn-training-unit.v1"
SUMMARY_SCHEMA = "researchharness.rollout-summary.v1"
MANIFEST_SCHEMA = "researchharness.turn-training-export-manifest.v1"
EXPORT_DIR_NAME = "turn-training-export-v1"
CANONICALIZATION = "canonical-json-v1"
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
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
            value = getattr(self, label)
            pattern = _RUN_ID_RE if label == "run_id" else _IDENTITY_RE
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"{label} is not a safe, non-empty identity")
        if isinstance(self.rollout_index, bool) or not isinstance(self.rollout_index, int) or self.rollout_index < 0:
            raise ValueError("rollout_index must be a non-negative integer")
        if not isinstance(self.scaffold_version, str):
            raise ValueError("scaffold_version must be a string")
        if not isinstance(self.tool_protocol_version, str) or not self.tool_protocol_version:
            raise ValueError("tool_protocol_version must be a non-empty string")
        if self.tokenizer_revision is not None and not isinstance(self.tokenizer_revision, str):
            raise ValueError("tokenizer_revision must be a string or null")

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


def _read_canonical_json(path: Path) -> Any:
    raw = path.read_bytes()
    value = json.loads(raw)
    if raw != canonical_json_bytes(value) + b"\n":
        raise ValueError(f"JSON artifact is not canonical-json-v1: {path}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = _read_canonical_json(path)
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
        _reject_nonfinite(value)
        rows.append(value)
    event_indexes = [row.get("event_index") for row in rows]
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 1 for index in event_indexes):
        raise ValueError("trace event_index must be a positive integer")
    if event_indexes != sorted(event_indexes) or len(event_indexes) != len(set(event_indexes)):
        raise ValueError("trace event_index must be strictly increasing")
    return rows


def _require_regular_source(path: Path, *, label: str) -> Path:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{label} must be an ordinary file")
    if source.stat().st_nlink != 1:
        raise ValueError(f"{label} must not be hardlinked")
    _reject_symlink_ancestors(source.parent)
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


def _normalized_raw_provider_response(raw_response: Mapping[str, Any]) -> dict[str, Any]:
    choices = raw_response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ValueError("raw provider response must contain a structured first choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("raw provider response first choice must contain a message")
    raw_tool_calls = message.get("tool_calls")
    if raw_tool_calls is None:
        raw_tool_calls = []
    if not isinstance(raw_tool_calls, list):
        raise ValueError("raw provider response tool_calls must be a list or null")
    tool_calls: list[dict[str, Any]] = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, Mapping):
            raise ValueError("raw provider response tool call must be an object")
        function = raw_tool_call.get("function")
        if not isinstance(function, Mapping):
            raise ValueError("raw provider response tool call function must be an object")
        tool_calls.append(
            {
                "id": raw_tool_call.get("id"),
                "type": raw_tool_call.get("type"),
                "function": {
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                },
            }
        )
    content = message.get("content")
    if content is None:
        content = ""
    usage = raw_response.get("usage")
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "reasoning_content": message.get("reasoning_content"),
            "tool_calls": tool_calls,
        },
        "finish_reason": choice.get("finish_reason"),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "provider_status": "ok",
    }


def _has_exact_provider_capture(payload: Mapping[str, Any], *, compaction: bool) -> bool:
    if compaction:
        response = payload.get("summary_response")
        return bool(
            isinstance(response, Mapping)
            and isinstance(response.get("provider_request"), Mapping)
            and response.get("provider_request")
            and isinstance(response.get("raw_provider_response"), Mapping)
            and response.get("raw_provider_response")
        )
    return bool(
        isinstance(payload.get("provider_request"), Mapping)
        and payload.get("provider_request")
        and isinstance(payload.get("raw_provider_response"), Mapping)
        and payload.get("raw_provider_response")
    )


def _main_capture_ok(payload: Mapping[str, Any], findings: Sequence[str]) -> bool:
    response = _response_source(payload)
    tool_calls = response.get("tool_calls")
    return bool(
        response.get("status") == "ok"
        and _has_exact_provider_capture(payload, compaction=False)
        and not (
            response.get("finish_reason") == "length"
            and isinstance(tool_calls, list)
            and tool_calls
        )
        and not findings
    )


def _compaction_capture_ok(payload: Mapping[str, Any], findings: Sequence[str]) -> bool:
    response = _response_source(payload, compaction=True)
    summary_text = str(payload.get("summary_text", "") or "").strip()
    response_text = str(response.get("content", "") or "").strip()
    tool_calls = response.get("tool_calls")
    provider_request = _provider_request(payload, compaction=True)
    summary_request = payload.get("summary_request")
    return bool(
        payload.get("status") == "ok"
        and response.get("status") == "ok"
        and _has_exact_provider_capture(payload, compaction=True)
        and summary_text
        and summary_text == response_text
        and isinstance(tool_calls, list)
        and not tool_calls
        and isinstance(summary_request, list)
        and summary_request == provider_request.get("messages")
        and not provider_request.get("tools")
        and not findings
    )


def _validate_request_shape(request: Mapping[str, Any], *, capture_ok: bool) -> None:
    if set(request) != {"model", "messages", "tools", "sampling", "provider_fields"}:
        raise ValueError("normalized request has an unexpected schema")
    if not isinstance(request.get("model"), str):
        raise ValueError("request.model must be a string")
    if capture_ok and not request.get("model"):
        raise ValueError("successful capture must preserve a non-empty provider model request")
    if not isinstance(request.get("messages"), list):
        raise ValueError("request.messages must be a list")
    if not isinstance(request.get("tools"), list):
        raise ValueError("request.tools must be a list")
    if not isinstance(request.get("sampling"), Mapping):
        raise ValueError("request.sampling must be an object")
    if not isinstance(request.get("provider_fields"), Mapping):
        raise ValueError("request.provider_fields must be an object")


def _validate_tool_calls(response: Mapping[str, Any]) -> None:
    message = response.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("response.message must be an object")
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        raise ValueError("response.message.tool_calls must be a list")
    seen_ids: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping) or tool_call.get("type") != "function":
            raise ValueError("tool call must be a structured function call")
        call_id = tool_call.get("id")
        function = tool_call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            raise ValueError("tool call IDs must be non-empty and unique in response order")
        if not isinstance(function, Mapping):
            raise ValueError("tool call function must be an object")
        if not isinstance(function.get("name"), str) or not function.get("name"):
            raise ValueError("tool call function name must be non-empty")
        if not isinstance(function.get("arguments"), str):
            raise ValueError("tool call arguments must preserve the provider string")
        seen_ids.add(call_id)


def _validate_provider_attempts(
    response_source: Mapping[str, Any],
    *,
    provider_request: Mapping[str, Any],
    raw_response: Mapping[str, Any],
) -> None:
    attempts = response_source.get("provider_attempts")
    status = response_source.get("status")
    if not isinstance(attempts, list) or not attempts:
        if status == "ok":
            raise ValueError("successful provider capture must preserve every provider attempt")
        if provider_request or raw_response:
            raise ValueError("failed provider capture with boundary evidence must preserve attempts")
        return
    retry_count = _nonnegative_int(
        response_source.get("provider_retry_count", len(attempts) - 1),
        label="provider_retry_count",
    )
    if retry_count != len(attempts) - 1:
        raise ValueError("provider retry count differs from captured attempt closure")
    allowed_statuses = {"accepted", "rejected_empty", "error", "parse_error"}
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping) or attempt.get("attempt_index") != index:
            raise ValueError("provider attempts must be complete and canonically indexed")
        if attempt.get("status") not in allowed_statuses:
            raise ValueError("provider attempt has an invalid status")
        if not isinstance(attempt.get("provider_request"), Mapping):
            raise ValueError("provider attempt request must be an object")
        if not isinstance(attempt.get("raw_provider_response"), Mapping):
            raise ValueError("provider attempt raw response must be an object")
        if attempt.get("provider_request") != provider_request:
            raise ValueError("provider retry request differs from the final provider request")
        if attempt.get("status") == "accepted" and index != len(attempts) - 1:
            raise ValueError("only the final provider attempt may be accepted")
        if attempt.get("status") == "rejected_empty":
            try:
                rejected_response = _normalized_raw_provider_response(
                    attempt["raw_provider_response"]
                )
            except ValueError as exc:
                raise ValueError("rejected-empty attempt lacks a parseable response") from exc
            rejected_message = rejected_response["message"]
            rejected_content = rejected_message.get("content")
            if (
                (isinstance(rejected_content, str) and rejected_content.strip())
                or (not isinstance(rejected_content, str) and rejected_content)
                or rejected_message.get("tool_calls")
            ):
                raise ValueError("rejected-empty attempt contains an acceptable response")
    final_attempt = attempts[-1]
    if final_attempt.get("provider_request") != provider_request:
        raise ValueError("final provider attempt request differs from accepted boundary")
    if final_attempt.get("raw_provider_response") != raw_response:
        raise ValueError("final provider attempt response differs from accepted boundary")
    if status == "ok" and final_attempt.get("status") != "accepted":
        raise ValueError("successful provider capture must end in one accepted attempt")
    if status != "ok" and final_attempt.get("status") == "accepted":
        raise ValueError("failed provider capture cannot end in an accepted attempt")


def _tool_result_ids(messages: object) -> list[str]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            result.append(tool_call_id)
    return result


def _contains_ordered_sequence(values: Sequence[str], expected: Sequence[str]) -> bool:
    if not expected:
        return True
    cursor = 0
    for value in values:
        if value == expected[cursor]:
            cursor += 1
            if cursor == len(expected):
                return True
    return False


def _validate_tool_result_closure(
    *,
    trace_rows: Sequence[Mapping[str, Any]],
    capture_row: Mapping[str, Any],
    response: Mapping[str, Any],
) -> None:
    message = response.get("message")
    tool_calls = message.get("tool_calls") if isinstance(message, Mapping) else []
    if not isinstance(tool_calls, list) or not tool_calls:
        return
    if response.get("finish_reason") == "length":
        return
    expected_ids = [str(tool_call.get("id", "")) for tool_call in tool_calls]
    capture_event_index = capture_row.get("event_index")
    capture_position = next(
        (
            index
            for index, row in enumerate(trace_rows)
            if row.get("event_index") == capture_event_index
        ),
        None,
    )
    if capture_position is None:
        raise ValueError("tool-call capture is missing from trace")
    next_main_position = next(
        (
            index
            for index in range(capture_position + 1, len(trace_rows))
            if trace_rows[index].get("capture_type") == "llm_call"
        ),
        None,
    )
    boundary = next_main_position if next_main_position is not None else len(trace_rows)
    intervening = trace_rows[capture_position + 1 : boundary]
    traced_result_ids: list[str] = []
    for row in intervening:
        if row.get("role") == "tool":
            ids = row.get("tool_call_ids")
            if isinstance(ids, list):
                traced_result_ids.extend(
                    str(tool_call_id)
                    for tool_call_id in ids
                    if isinstance(tool_call_id, str) and tool_call_id
                )
    trace_proves_execution = _contains_ordered_sequence(traced_result_ids, expected_ids)
    compaction_pre_proof = False
    compacted_next_input_proof = False
    for row in intervening:
        if row.get("capture_type") != "compaction":
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if _contains_ordered_sequence(_tool_result_ids(payload.get("pre_messages")), expected_ids):
            compaction_pre_proof = True
            if payload.get("status") == "ok":
                compacted_next_input_proof = True
    next_input_proof = False
    if next_main_position is not None:
        next_payload = trace_rows[next_main_position].get("payload")
        if isinstance(next_payload, Mapping):
            next_input_proof = _contains_ordered_sequence(
                _tool_result_ids(_provider_request(next_payload).get("messages")),
                expected_ids,
            )
    if not (trace_proves_execution or next_input_proof or compaction_pre_proof):
        raise ValueError("tool-call target lacks matching tool-result closure")
    if next_main_position is not None and not (
        next_input_proof or compacted_next_input_proof
    ):
        raise ValueError("tool results did not enter the next provider request")


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
        "provider_retry_count": _nonnegative_int(payload.get("provider_retry_count", response.get("provider_retry_count", 0)), label="provider_retry_count"),
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


def _nonnegative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _unit_common(config: TrainingExportConfig, row: Mapping[str, Any], unit_type: str) -> dict[str, Any]:
    turn_index = _nonnegative_int(row.get("turn_index"), label="turn_index")
    return {
        "schema_version": UNIT_SCHEMA,
        "unit_type": unit_type,
        **config.identity(),
        "turn_index": turn_index,
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
            "capture_status": "ok" if _main_capture_ok(payload, findings) else "error",
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
    successful = _compaction_capture_ok(payload, findings)
    existing_memory = str(payload.get("existing_memory_text", "") or "")
    unit = _unit_common(config, row, "memory_compaction")
    unit.update(
        {
            "capture_status": "ok" if successful else "error",
            "request": _normalized_request(provider_request),
            "response": response,
            "compaction": {
                "trigger_reason": payload.get("trigger_reason", ""),
                "prior_token_estimate": _nonnegative_int(payload.get("prior_token_estimate", 0), label="prior_token_estimate"),
                "new_token_estimate": _nonnegative_int(payload.get("new_token_estimate", 0), label="new_token_estimate"),
                "compacted_group_count": _nonnegative_int(payload.get("compacted_group_count", 0), label="compacted_group_count"),
                "kept_group_count": _nonnegative_int(payload.get("kept_group_count", 0), label="kept_group_count"),
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
    if not isinstance(value, Mapping):
        raise TypeError("termination must be a string or object")
    reason = value.get("reason", value.get("termination", ""))
    status = value.get("status")
    if not isinstance(reason, str):
        raise ValueError("termination.reason must be a string")
    if status is not None and not isinstance(status, str):
        raise ValueError("termination.status must be a string")
    for field in ("submitted", "clean_termination"):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"termination.{field} must be a boolean")
    submitted = value.get("submitted", reason == "result")
    return {
        "status": status or ("completed" if submitted else "stopped"),
        "reason": reason,
        "submitted": submitted,
        "clean_termination": value.get("clean_termination", submitted),
    }


def _all_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    casefolded: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative.casefold() in casefolded:
            raise ValueError(f"export bundle contains a case-colliding path: {relative}")
        casefolded.add(relative.casefold())
        if path.is_symlink():
            raise ValueError("export bundle may not contain symlinks")
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError("export bundle may not contain hardlinked files")
            _safe_ref_path(relative)
            paths.append(path)
        elif not path.is_dir():
            raise ValueError("export bundle contains a non-ordinary entry")
    return paths


def _copy_source(source: Path, destination: Path) -> None:
    _require_regular_source(source, label="source artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
        os.close(source_fd)
        raise ValueError("source artifact changed into a non-ordinary or hardlinked file")
    with os.fdopen(source_fd, "rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def _reject_symlink_ancestors(path: Path) -> None:
    current = path.absolute()
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"symlink path component is forbidden: {current}")
        current = current.parent


def _rename_no_replace(source: Path, target: Path) -> None:
    if os.name == "posix":
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
                return
            error = ctypes.get_errno()
            if error == 17:
                raise FileExistsError(f"immutable export already exists: {target}")
            raise OSError(error, os.strerror(error), str(target))
    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace publish is unavailable; refusing non-atomic export",
        str(target),
    )


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
    _reject_symlink_ancestors(parent)
    parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(parent)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"immutable export already exists: {target}")
    lock_path = parent / f".{target.name}.publish.lock"
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    temp: Path | None = None
    try:
        os.close(lock_fd)
        temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=parent))
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
            "capture_error_count": sum(unit["capture_status"] == "error" for unit in units),
            "unit_set_sha256": _hash_json(
                "researchharness.unit-set.v1", [unit["unit_id"] for unit in units]
            ),
            "termination": _termination(termination),
            "final_patch_ref": final_patch_ref,
            "trace_ref": trace_ref,
        }
        summary["integrity"] = {
            "canonicalization": CANONICALIZATION,
            "summary_payload_sha256": _hash_json("researchharness.summary-payload.v1", summary),
        }
        _write_json(temp / "rollout-summary.v1.json", summary)

        payload_files = [
            path for path in _all_files(temp) if path.name not in {"manifest.json", "SHA256SUMS"}
        ]
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            **config.identity(),
            "canonicalization": CANONICALIZATION,
            "provenance": {
                "harness_revision": config.harness_revision,
                "scaffold_version": config.scaffold_version,
                "tool_protocol_version": config.tool_protocol_version,
                "tokenizer_revision": config.tokenizer_revision,
            },
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
        _rename_no_replace(temp, target)
        if os.name == "posix":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return target
    except Exception:
        if temp is not None:
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


def _contains_prohibited_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        prohibited = any(str(key).casefold() in _PROHIBITED_AGENT_KEYS for key in value)
        return prohibited or any(
            _contains_prohibited_key(nested) for nested in value.values()
        )
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
    checksum_text = (root / "SHA256SUMS").read_text(encoding="utf-8")
    if checksum_text and not checksum_text.endswith("\n"):
        raise ValueError("SHA256SUMS must end with a newline")
    checksum_lines = checksum_text.splitlines()
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
    expected_manifest_keys = {
        "schema_version",
        "task_id",
        "task_revision_id",
        "rollout_id",
        "rollout_index",
        "run_id",
        "canonicalization",
        "provenance",
        "validators",
        "oracle_leak_unit_count",
        "files",
    }
    expected_summary_keys = {
        "schema_version",
        "task_id",
        "task_revision_id",
        "rollout_id",
        "rollout_index",
        "run_id",
        "main_agent_turn_count",
        "memory_compaction_count",
        "capture_error_count",
        "unit_set_sha256",
        "termination",
        "final_patch_ref",
        "trace_ref",
        "integrity",
    }
    if _contains_prohibited_key(manifest) or _contains_prohibited_key(summary):
        raise ValueError("V1 bundle metadata contains prohibited multi-agent fields")
    if set(manifest) != expected_manifest_keys:
        raise ValueError("manifest schema contains unexpected fields")
    if set(summary) != expected_summary_keys:
        raise ValueError("rollout summary schema contains unexpected fields")
    if manifest.get("canonicalization") != CANONICALIZATION:
        raise ValueError("manifest canonicalization is invalid")
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
    if manifest_paths != sorted(manifest_paths):
        raise ValueError("manifest file inventory is not canonically ordered")
    identity_fields = ("task_id", "task_revision_id", "rollout_id", "rollout_index", "run_id")
    identity = {key: summary.get(key) for key in identity_fields}
    for key in ("task_id", "task_revision_id", "rollout_id", "run_id"):
        pattern = _RUN_ID_RE if key == "run_id" else _IDENTITY_RE
        if not isinstance(identity[key], str) or pattern.fullmatch(identity[key]) is None:
            raise ValueError(f"rollout summary {key} is invalid")
    _nonnegative_int(identity["rollout_index"], label="rollout_index")
    if any(manifest.get(key) != value for key, value in identity.items()):
        raise ValueError("manifest identity differs from summary")
    manifest_provenance = manifest.get("provenance")
    provenance_fields = (
        "harness_revision",
        "scaffold_version",
        "tool_protocol_version",
        "tokenizer_revision",
    )
    if not isinstance(manifest_provenance, Mapping) or set(manifest_provenance) != set(provenance_fields):
        raise ValueError("manifest provenance is invalid")

    trace_path = _resolve_ref(root, summary.get("trace_ref"))
    schema_referenced_paths = {
        "turn-training-units.v1.jsonl",
        "rollout-summary.v1.json",
        trace_path.relative_to(root).as_posix(),
    }
    trace_rows = _read_trace(trace_path)
    if any(row.get("run_id") != identity["run_id"] for row in trace_rows):
        raise ValueError("trace run_id differs from rollout identity")
    captures = [row for row in trace_rows if row.get("capture_type") in {"llm_call", "compaction"}]
    if len(captures) != len(units):
        raise ValueError("trace capture closure differs from unit count")
    capture_by_event = {int(row.get("event_index", 0) or 0): row for row in captures}
    if len(capture_by_event) != len(captures):
        raise ValueError("trace capture event identities are not unique")
    expected_capture_order = [
        (row["capture_type"], row["event_index"]) for row in captures
    ]
    actual_capture_order = [
        (
            unit.get("source", {}).get("capture_type"),
            unit.get("source", {}).get("trace_event_index"),
        )
        for unit in units
    ]
    if actual_capture_order != expected_capture_order:
        raise ValueError("trace capture order/closure differs from training units")
    unit_ids: set[str] = set()
    main_turn_indexes: list[int] = []
    for unit_position, unit in enumerate(units):
        if unit.get("schema_version") != UNIT_SCHEMA or unit.get("unit_type") not in {"main_agent_turn", "memory_compaction"}:
            raise ValueError("training unit schema/type is invalid")
        expected_unit_keys = {
            "schema_version",
            "unit_type",
            "task_id",
            "task_revision_id",
            "rollout_id",
            "rollout_index",
            "run_id",
            "turn_index",
            "capture_status",
            "request",
            "response",
            "source",
            "provenance",
            "unit_id",
            "integrity",
        }
        if unit.get("unit_type") == "memory_compaction":
            expected_unit_keys.add("compaction")
        unit_metadata = {
            key: value
            for key, value in unit.items()
            if key not in {"request", "response"}
        }
        request_keys = unit.get("request", {}).keys() if isinstance(unit.get("request"), Mapping) else ()
        if _contains_prohibited_key(unit_metadata) or any(
            str(key).casefold() in _PROHIBITED_AGENT_KEYS for key in request_keys
        ):
            raise ValueError("V1 unit contains prohibited multi-agent fields")
        if set(unit) != expected_unit_keys:
            raise ValueError("training unit schema contains unexpected fields")
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or unit_id in unit_ids:
            raise ValueError("training unit_id is missing or duplicated")
        unit_ids.add(unit_id)
        if any(unit.get(key) != value for key, value in identity.items()):
            raise ValueError("training unit identity differs from summary")
        turn_index = _nonnegative_int(unit.get("turn_index"), label="turn_index")
        if unit.get("unit_type") == "main_agent_turn":
            main_turn_indexes.append(turn_index)
        source = unit.get("source")
        if not isinstance(source, Mapping) or source.get("trace_ref") != summary.get("trace_ref"):
            raise ValueError("training unit trace_ref differs from rollout trace")
        if set(source) != {
            "trace_ref",
            "trace_event_index",
            "capture_type",
            "provider_request_ref",
            "raw_provider_response_ref",
        }:
            raise ValueError("training unit source schema is invalid")
        for source_ref_name in (
            "trace_ref",
            "provider_request_ref",
            "raw_provider_response_ref",
        ):
            source_path = _resolve_ref(root, source.get(source_ref_name))
            schema_referenced_paths.add(source_path.relative_to(root).as_posix())
        event_index = _nonnegative_int(
            source.get("trace_event_index"),
            label="trace_event_index",
        )
        if event_index < 1:
            raise ValueError("trace_event_index must be positive")
        row = capture_by_event.get(event_index)
        expected_capture = "llm_call" if unit.get("unit_type") == "main_agent_turn" else "compaction"
        if row is None or row.get("capture_type") != expected_capture or source.get("capture_type") != expected_capture:
            raise ValueError("training unit source does not bind its trace capture")
        integrity = unit.get("integrity")
        if not isinstance(integrity, dict) or integrity.get("canonicalization") != CANONICALIZATION:
            raise ValueError("training unit integrity is invalid")
        if set(integrity) != {
            "canonicalization",
            "request_sha256",
            "response_sha256",
            "unit_payload_sha256",
        }:
            raise ValueError("training unit integrity schema is invalid")
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
        expected_response = _normalized_response(response_source)
        if unit.get("response") != expected_response:
            raise ValueError("exported response differs from accepted trace response")
        if response_source.get("status") == "ok":
            normalized_raw_response = _normalized_raw_provider_response(raw_response)
            if expected_response != normalized_raw_response:
                raise ValueError("accepted response differs from raw provider response")
        _validate_tool_calls(expected_response)
        _validate_provider_attempts(
            response_source,
            provider_request=provider_request,
            raw_response=raw_response,
        )
        if expected_capture == "llm_call":
            _validate_tool_result_closure(
                trace_rows=trace_rows,
                capture_row=row,
                response=expected_response,
            )
        findings = _oracle_findings(provider_request)
        provenance = unit.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError("training unit provenance is missing")
        if set(provenance) != {
            "harness_revision",
            "scaffold_version",
            "tool_protocol_version",
            "requested_model",
            "provider_model",
            "model_revision",
            "tokenizer_revision",
            "provider_retry_count",
            "oracle_isolation",
        }:
            raise ValueError("training unit provenance schema is invalid")
        if any(provenance.get(key) != manifest_provenance.get(key) for key in provenance_fields):
            raise ValueError("training unit provenance differs from manifest")
        oracle = provenance.get("oracle_isolation", {})
        if oracle != {"status": "leak" if findings else "clear", "findings": findings}:
            raise ValueError("oracle leak marker differs from model-visible request")
        source_response = payload.get("response")
        if not isinstance(source_response, Mapping):
            source_response = payload.get("summary_response") if isinstance(payload.get("summary_response"), Mapping) else {}
        expected_retry_count = _nonnegative_int(
            payload.get("provider_retry_count", source_response.get("provider_retry_count", 0)),
            label="provider_retry_count",
        )
        expected_requested_model = (
            payload.get("requested_model")
            or source_response.get("requested_model")
            or payload.get("model_name")
        )
        expected_provider_model = payload.get("provider_model") or source_response.get("provider_model")
        if (
            provenance.get("provider_retry_count") != expected_retry_count
            or provenance.get("requested_model") != expected_requested_model
            or provenance.get("provider_model") != expected_provider_model
            or provenance.get("model_revision") is not None
        ):
            raise ValueError("training unit provider provenance differs from trace")
        expected_status = (
            _compaction_capture_ok(payload, findings)
            if expected_capture == "compaction"
            else _main_capture_ok(payload, findings)
        )
        _validate_request_shape(unit["request"], capture_ok=expected_status)
        if unit.get("capture_status") != ("ok" if expected_status else "error"):
            raise ValueError("capture_status differs from provider-boundary evidence")
        if expected_capture == "compaction":
            compaction = unit.get("compaction")
            if not isinstance(compaction, Mapping):
                raise ValueError("memory compaction unit lacks compaction metadata")
            if set(compaction) != {
                "trigger_reason",
                "prior_token_estimate",
                "new_token_estimate",
                "compacted_group_count",
                "kept_group_count",
                "existing_memory_text_sha256",
                "pre_messages_ref",
                "post_messages_ref",
            }:
                raise ValueError("memory compaction metadata schema is invalid")
            pre = _read_canonical_json(_resolve_ref(root, compaction.get("pre_messages_ref")))
            post = _read_canonical_json(_resolve_ref(root, compaction.get("post_messages_ref")))
            for compaction_ref_name in ("pre_messages_ref", "post_messages_ref"):
                compaction_path = _resolve_ref(root, compaction.get(compaction_ref_name))
                schema_referenced_paths.add(compaction_path.relative_to(root).as_posix())
            if not isinstance(pre, list) or not isinstance(post, list):
                raise ValueError("compaction pre/post artifacts must be message lists")
            if pre != payload.get("pre_messages", []) or post != payload.get("post_messages", []):
                raise ValueError("compaction pre/post artifact differs from trace")
            if provider_request and payload.get("summary_request") != provider_request.get("messages"):
                raise ValueError(
                    "compaction summary_request differs from the provider request messages"
                )
            existing_memory = str(payload.get("existing_memory_text", "") or "")
            expected_compaction = {
                "trigger_reason": payload.get("trigger_reason", ""),
                "prior_token_estimate": _nonnegative_int(payload.get("prior_token_estimate", 0), label="prior_token_estimate"),
                "new_token_estimate": _nonnegative_int(payload.get("new_token_estimate", 0), label="new_token_estimate"),
                "compacted_group_count": _nonnegative_int(payload.get("compacted_group_count", 0), label="compacted_group_count"),
                "kept_group_count": _nonnegative_int(payload.get("kept_group_count", 0), label="kept_group_count"),
                "existing_memory_text_sha256": "sha256:" + _hash_bytes(existing_memory.encode("utf-8")),
            }
            for key, expected_value in expected_compaction.items():
                if compaction.get(key) != expected_value:
                    raise ValueError(f"compaction {key} differs from trace")
            if expected_status:
                next_main_capture = next(
                    (
                        later
                        for later in captures[unit_position + 1 :]
                        if later.get("capture_type") == "llm_call"
                    ),
                    None,
                )
                if next_main_capture is not None:
                    from agent_base.react_agent import prepare_messages_for_llm

                    prepared_post, _ = prepare_messages_for_llm(post)
                    next_payload = (
                        next_main_capture.get("payload")
                        if isinstance(next_main_capture.get("payload"), Mapping)
                        else {}
                    )
                    next_provider_request = _provider_request(next_payload)
                    if next_provider_request.get("messages") != prepared_post:
                        raise ValueError(
                            "successful compaction post_messages differ from the next provider request"
                        )

    if main_turn_indexes != sorted(set(main_turn_indexes)):
        raise ValueError("main-agent turn_index values must be strictly increasing")
    summary_main_count = _nonnegative_int(
        summary.get("main_agent_turn_count"),
        label="main_agent_turn_count",
    )
    summary_compaction_count = _nonnegative_int(
        summary.get("memory_compaction_count"),
        label="memory_compaction_count",
    )
    summary_error_count = _nonnegative_int(
        summary.get("capture_error_count"),
        label="capture_error_count",
    )
    if summary_main_count != sum(unit.get("unit_type") == "main_agent_turn" for unit in units):
        raise ValueError("rollout summary main-agent count mismatch")
    if summary_compaction_count != sum(unit.get("unit_type") == "memory_compaction" for unit in units):
        raise ValueError("rollout summary compaction count mismatch")
    if summary_error_count != sum(unit.get("capture_status") == "error" for unit in units):
        raise ValueError("rollout summary capture-error count mismatch")
    if summary.get("unit_set_sha256") != _hash_json(
        "researchharness.unit-set.v1", [unit["unit_id"] for unit in units]
    ):
        raise ValueError("rollout summary unit-set digest mismatch")
    summary_integrity = summary.get("integrity")
    if not isinstance(summary_integrity, Mapping) or summary_integrity.get("canonicalization") != CANONICALIZATION:
        raise ValueError("rollout summary integrity is invalid")
    if set(summary_integrity) != {
        "canonicalization",
        "summary_payload_sha256",
    }:
        raise ValueError("rollout summary integrity schema is invalid")
    summary_payload = json.loads(json.dumps(summary, ensure_ascii=False))
    summary_payload.pop("integrity", None)
    if summary_integrity.get("summary_payload_sha256") != _hash_json(
        "researchharness.summary-payload.v1", summary_payload
    ):
        raise ValueError("rollout summary payload digest mismatch")
    termination = summary.get("termination")
    if not isinstance(termination, Mapping):
        raise ValueError("rollout termination is missing")
    if _termination(termination) != dict(termination):
        raise ValueError("rollout termination is not canonical")
    final_patch_ref = summary.get("final_patch_ref")
    if final_patch_ref is not None:
        final_patch_path = _resolve_ref(root, final_patch_ref)
        schema_referenced_paths.add(final_patch_path.relative_to(root).as_posix())
    if set(manifest_paths) != schema_referenced_paths:
        raise ValueError("manifest contains unreferenced or missing payload artifacts")
    if "resolved" in summary or "reward" in summary:
        raise ValueError("Harness export cannot contain resolved or reward labels")
    validators = manifest.get("validators")
    expected_validators = [{"name": name, "status": "pass"} for name in _VALIDATOR_NAMES]
    if validators != expected_validators:
        raise ValueError("manifest validator closure is not canonical")
    leak_count = sum(unit.get("provenance", {}).get("oracle_isolation", {}).get("status") == "leak" for unit in units)
    manifest_leak_count = _nonnegative_int(
        manifest.get("oracle_leak_unit_count"),
        label="oracle_leak_unit_count",
    )
    if manifest_leak_count != leak_count:
        raise ValueError("manifest oracle leak count mismatch")
    return summary


__all__ = [
    "EXPORT_DIR_NAME",
    "TrainingExportConfig",
    "canonical_json_bytes",
    "export_turn_training_bundle",
    "validate_turn_training_bundle",
]
