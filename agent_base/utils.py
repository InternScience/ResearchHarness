import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional, Union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_LAST_LOADED: dict[tuple[str, str], str] = {}
REQUIRED_ENV_VARS = (
    "API_KEY",
    "API_BASE",
    "MODEL_NAME",
    "SERPER_KEY_ID",
    "JINA_API_KEYS",
    "MINERU_TOKEN",
)


class MissingRequiredEnvError(RuntimeError):
    pass


def load_dotenv(path: Union[str, Path]) -> None:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    env_id = str(env_path.resolve())
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value:
            lexer = shlex.shlex(value, posix=True)
            lexer.whitespace = ""
            lexer.commenters = "#"
            parsed_value = "".join(list(lexer)).strip()
        else:
            parsed_value = ""
        marker = (env_id, key)
        existing = os.environ.get(key)
        previous_loaded = _DOTENV_LAST_LOADED.get(marker)
        if existing is None or existing == previous_loaded:
            os.environ[key] = parsed_value
        _DOTENV_LAST_LOADED[marker] = parsed_value


def env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def missing_required_env(required: tuple[str, ...] = REQUIRED_ENV_VARS) -> list[str]:
    return [key for key in required if not os.getenv(key, "").strip()]


def require_required_env(context: str = "ResearchHarness") -> None:
    missing = missing_required_env()
    if not missing:
        return
    raise MissingRequiredEnvError(
        f"{context} missing required environment variables: {', '.join(missing)}. "
        "Set them in .env or the process environment before running."
    )


def safe_jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_jsonable(item) for item in value]
    return str(value)


def append_jsonl(path: Union[str, Path], record: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_text_lossy(path: Union[str, Path]) -> str:
    file_path = Path(path)
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return file_path.read_text(encoding="utf-8", errors="replace")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect shared agent_base utilities.")
    parser.add_argument("--dotenv", help="Optional dotenv path to load before printing the summary.")
    args = parser.parse_args(argv)

    if args.dotenv:
        load_dotenv(args.dotenv)

    payload = {
        "project_root": str(PROJECT_ROOT),
        "dotenv_loaded": bool(args.dotenv),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
