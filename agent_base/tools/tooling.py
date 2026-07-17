import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, Union

import json5
from agent_base.utils import PROJECT_ROOT, load_dotenv

WORKSPACE_ROOT_ENV = "WORKSPACE_ROOT"

SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
    "authorized_keys",
    "credentials",
}
SENSITIVE_PATH_PARTS = {
    ".git",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
}
SENSITIVE_COMMAND_TOKENS = [
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "/etc/passwd",
    "/etc/shadow",
    "/root/.ssh",
    "/root/.aws",
    "~/.ssh",
    "~/.aws",
]
BLOCKED_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[\s;&|])sudo(\s|$)"), "sudo escalation is blocked"),
    (re.compile(r"(^|[\s;&|])su(\s|$)"), "user switching is blocked"),
    (re.compile(r"(^|[\s;&|])(shutdown|reboot|poweroff|halt)(\s|$)"), "system power-control commands are blocked"),
    (re.compile(r"(^|[\s;&|])mkfs(?:\.\w+)?(\s|$)"), "disk-formatting commands are blocked"),
    (re.compile(r"(^|[\s;&|])(fdisk|parted)(\s|$)"), "disk-partitioning commands are blocked"),
    (re.compile(r":\s*\(\)\s*\{\s*:\|:&\s*\};:"), "fork-bomb patterns are blocked"),
    (re.compile(r"\brm\s+-rf\s+/(\s|$)"), "destructive root deletion is blocked"),
    (re.compile(r"\brm\s+-rf\s+~(/|\s|$)"), "destructive home deletion is blocked"),
]
SENSITIVE_ENV_EXACT = {
    "API_KEY",
    "SERPER_KEY",
    "TAVILY_API_KEY",
    "JINA_KEY",
    "MINERU_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
}
SENSITIVE_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "COOKIE",
)
SAFE_ENV_ALWAYS = {
    "PATH",
    "LANG",
    "TERM",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TZ",
    "COLORTERM",
    "PWD",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "VIRTUAL_ENV",
    "LOGNAME",
    "USER",
    "USERNAME",
    "SHELL",
    "SHLVL",
    "_",
}


def workspace_root() -> Path:
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    root = Path(configured).expanduser() if configured else PROJECT_ROOT
    return root.resolve()


def normalize_base_root(base_root: Optional[Union[str, Path]]) -> Path:
    if base_root is None:
        return workspace_root()
    return Path(base_root).expanduser().resolve()


def normalize_workspace_root(path_value: Optional[Union[str, Path]]) -> Path:
    if path_value is None or str(path_value).strip() == "":
        return workspace_root()
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"Workspace directory is not a directory: {path}")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_workspace_path(path_value: Union[str, Path], *, base_root: Optional[Path] = None) -> Path:
    path = Path(path_value).expanduser()
    root = normalize_base_root(base_root)
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


def is_sensitive_path(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    lowered_name = path.name.lower()
    if lowered_name in SENSITIVE_FILE_NAMES:
        return True
    return any(part in SENSITIVE_PATH_PARTS for part in lowered_parts)


def validate_tool_path(path_value: Union[str, Path], purpose: str, *, allow_sensitive: bool = False, base_root: Optional[Path] = None) -> Path:
    path = resolve_workspace_path(path_value, base_root=base_root)
    root = normalize_base_root(base_root)
    if not _is_relative_to(path, root):
        raise ValueError(f"{purpose} is limited to the workspace root: {root}")
    if not allow_sensitive and is_sensitive_path(path):
        raise ValueError(f"{purpose} to sensitive paths is blocked: {path}")
    return path


def command_safety_issue(command: str) -> Optional[str]:
    lowered = command.lower()
    for pattern, reason in BLOCKED_COMMAND_PATTERNS:
        if pattern.search(command):
            return reason
    for token in SENSITIVE_COMMAND_TOKENS:
        if token.lower() in lowered:
            return f"access to sensitive path/token '{token}' is blocked"
    return None


def sanitized_subprocess_env(*, base_root: Optional[Path] = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env.keys()):
        upper = key.upper()
        if upper in SAFE_ENV_ALWAYS:
            continue
        if upper in SENSITIVE_ENV_EXACT or any(marker in upper for marker in SENSITIVE_ENV_MARKERS):
            env.pop(key, None)
    safe_home = str(normalize_base_root(base_root))
    env["HOME"] = safe_home
    env["PWD"] = safe_home
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "C.UTF-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _matches_schema_type(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


def _schema_type_label(type_spec: Any) -> str:
    if isinstance(type_spec, list):
        return " or ".join(str(item) for item in type_spec)
    return str(type_spec)


def _validate_schema_value(param_name: str, value: Any, schema: dict[str, Any]) -> None:
    type_spec = schema.get("type")
    if type_spec is not None:
        allowed_types = type_spec if isinstance(type_spec, list) else [type_spec]
        if not any(_matches_schema_type(value, expected_type) for expected_type in allowed_types):
            raise ValueError(f"Parameter '{param_name}' must be of type {_schema_type_label(type_spec)}.")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ValueError(f"Parameter '{param_name}' must contain at least {min_items} item(s).")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(f"{param_name}[{index}]", item, item_schema)


class ToolBase:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        if not self.name:
            raise ValueError(f"{self.__class__.__name__}.name must be set.")
        if not isinstance(self.parameters, dict):
            raise ValueError(f"{self.__class__.__name__}.parameters must be a JSON-schema-like dict.")

    def call(self, params: Union[str, dict], **kwargs):
        raise NotImplementedError

    def parse_json_args(self, params: Union[str, dict], strict_json: bool = False) -> dict:
        if isinstance(params, str):
            try:
                if strict_json:
                    parsed = json.loads(params)
                else:
                    parsed = json5.loads(params)
            except (TypeError, ValueError) as exc:
                raise ValueError("Parameters must be formatted as a valid JSON object.") from exc
        else:
            parsed = params

        if not isinstance(parsed, dict):
            raise ValueError("Parameters must decode to a JSON object.")

        required = self.parameters.get("required", [])
        for key in required:
            if key not in parsed:
                raise ValueError(f"Missing required parameter: {key}")

        properties = self.parameters.get("properties", {})
        if isinstance(properties, dict):
            for key, value in parsed.items():
                schema = properties.get(key)
                if isinstance(schema, dict):
                    _validate_schema_value(key, value, schema)
        return parsed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect workspace and path resolution helpers.")
    parser.add_argument("--workspace-root", help="Optional workspace root override for this invocation.")
    parser.add_argument("--path", help="Optional path to resolve inside the workspace.")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    workspace_root = normalize_workspace_root(args.workspace_root)
    payload: dict[str, str] = {
        "project_root": str(PROJECT_ROOT),
        "workspace_root": str(workspace_root),
    }
    if args.path:
        payload["resolved_path"] = str(resolve_workspace_path(args.path, base_root=workspace_root))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
