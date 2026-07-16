import re
from collections.abc import Iterable
from typing import Any


_MODEL_NAME_SPLIT_RE = re.compile(r"[/:\s]+")
OMITTABLE_GENERATE_PARAMS = frozenset({"temperature", "top_p", "presence_penalty"})


def normalize_omit_generate_params(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, dict):
        raise ValueError("omit_generate_params must be a string or a sequence of strings.")
    elif isinstance(value, Iterable):
        parts = [str(part).strip() for part in value]
    else:
        raise ValueError("omit_generate_params must be a string or a sequence of strings.")
    normalized = tuple(part for part in parts if part)
    unknown = sorted(set(normalized) - OMITTABLE_GENERATE_PARAMS)
    if unknown:
        allowed = ", ".join(sorted(OMITTABLE_GENERATE_PARAMS))
        raise ValueError(f"Unsupported omit_generate_params values: {unknown}. Allowed values: {allowed}.")
    return normalized


def model_rejects_sampling_params(model_name: str) -> bool:
    normalized = str(model_name or "").strip().casefold()
    if not normalized:
        return False
    parts = [part for part in _MODEL_NAME_SPLIT_RE.split(normalized) if part]
    return any(part.startswith("claude") for part in parts)


def model_rejects_presence_penalty(model_name: str) -> bool:
    normalized = str(model_name or "").strip().casefold()
    if not normalized:
        return False
    parts = [part for part in _MODEL_NAME_SPLIT_RE.split(normalized) if part]
    return any(part.startswith("gpt-5.5") for part in parts)


def apply_sampling_params(
    request_kwargs: dict[str, Any],
    *,
    model_name: str,
    temperature: Any = None,
    top_p: Any = None,
    presence_penalty: Any = None,
    omit_generate_params: Any = None,
) -> None:
    omitted = set(normalize_omit_generate_params(omit_generate_params))
    if model_rejects_sampling_params(model_name):
        return
    if temperature is not None and "temperature" not in omitted:
        request_kwargs["temperature"] = temperature
    if top_p is not None and "top_p" not in omitted:
        request_kwargs["top_p"] = top_p
    if (
        presence_penalty is not None
        and "presence_penalty" not in omitted
        and not model_rejects_presence_penalty(model_name)
    ):
        request_kwargs["presence_penalty"] = presence_penalty
