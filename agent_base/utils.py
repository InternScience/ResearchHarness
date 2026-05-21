import argparse
import base64
import json
import os
import re
import shutil
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_LAST_LOADED: dict[tuple[str, str], str] = {}
REQUIRED_ENV_VARS = (
    "API_KEY",
    "API_BASE",
    "MODEL_NAME",
    "SERPER_KEY",
    "JINA_KEY",
    "MINERU_TOKEN",
)
IMAGE_INPUT_REL_DIR = Path("inputs") / "images"
MAX_INPUT_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


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


def load_default_dotenvs() -> None:
    """Load dotenv files for both installed and source-tree usage."""
    cwd_env = Path.cwd() / ".env"
    project_env = PROJECT_ROOT / ".env"
    load_dotenv(cwd_env)
    if cwd_env.resolve() != project_env.resolve():
        load_dotenv(project_env)


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


def read_role_prompt_files(paths: Iterable[str]) -> str:
    blocks: list[str] = []
    for raw_path in paths:
        path_text = str(raw_path).strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.exists():
            raise ValueError(f"Role prompt file does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"Role prompt path is not a file: {path}")
        blocks.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(block for block in blocks if block.strip())


def _safe_image_stem(name: str, fallback: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name).stem).strip("._")
    return stem or fallback


def _unique_image_path(image_dir: Path, *, image_index: int, stem: str, suffix: str) -> Path:
    base_name = f"image_{image_index:03d}_{stem}{suffix}"
    candidate = image_dir / base_name
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = image_dir / f"image_{image_index:03d}_{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def image_input_content_parts(data_url: str, saved_path: str, *, detail: str = "auto") -> list[dict[str, Any]]:
    """Build standard initial content parts for a saved user image."""
    return [
        {"type": "text", "text": f"[User-provided image saved at {saved_path}]"},
        {"type": "image_url", "image_url": {"url": data_url, "detail": detail or "auto"}},
    ]


def stage_image_bytes_for_input(
    raw: bytes,
    *,
    workspace_root: Union[str, Path],
    filename: str,
    image_index: int,
    suffix: str,
    max_bytes: int = MAX_INPUT_IMAGE_BYTES,
) -> str:
    if not raw:
        raise ValueError("image input is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"image input exceeds {max_bytes} bytes")
    normalized_suffix = suffix.lower()
    if normalized_suffix not in IMAGE_MIME_BY_EXTENSION:
        raise ValueError(f"unsupported image extension: {suffix}")
    root = Path(workspace_root).expanduser().resolve()
    image_dir = root / IMAGE_INPUT_REL_DIR
    image_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_image_stem(filename, f"image_{image_index:03d}")
    dest = _unique_image_path(image_dir, image_index=image_index, stem=stem, suffix=normalized_suffix)
    dest.write_bytes(raw)
    return dest.relative_to(root).as_posix()


def stage_image_file_for_input(
    source_path: Union[str, Path],
    *,
    workspace_root: Union[str, Path],
    image_index: int,
    max_bytes: int = MAX_INPUT_IMAGE_BYTES,
) -> tuple[str, str]:
    source = Path(source_path).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    else:
        source = source.resolve()
    if not source.exists():
        raise ValueError(f"image path does not exist: {source}")
    if not source.is_file():
        raise ValueError(f"image path is not a file: {source}")
    suffix = source.suffix.lower()
    mime_type = IMAGE_MIME_BY_EXTENSION.get(suffix)
    if mime_type is None:
        raise ValueError(f"unsupported image extension for {source}; expected one of {', '.join(sorted(IMAGE_MIME_BY_EXTENSION))}")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError(f"image file is empty: {source}")
    if size > max_bytes:
        raise ValueError(f"image file exceeds {max_bytes} bytes: {source}")
    root = Path(workspace_root).expanduser().resolve()
    image_dir = root / IMAGE_INPUT_REL_DIR
    image_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_image_stem(source.name, f"image_{image_index:03d}")
    dest = _unique_image_path(image_dir, image_index=image_index, stem=stem, suffix=suffix)
    shutil.copyfile(source, dest)
    rel_path = dest.relative_to(root).as_posix()
    data_url = f"data:{mime_type};base64," + base64.b64encode(dest.read_bytes()).decode("ascii")
    return rel_path, data_url


def append_saved_image_paths_to_prompt(prompt: str, saved_paths: Iterable[str]) -> str:
    paths = [str(path).strip() for path in saved_paths if str(path).strip()]
    if not paths:
        return prompt
    lines = "\n".join(f"- {path}" for path in paths)
    return (
        f"{prompt.strip()}\n\n"
        "The user attached image input. The images are saved locally inside the workspace:\n"
        f"{lines}\n"
        "Use the direct image input when the model supports vision. If tool-based inspection is needed, use ReadImage on the saved local paths."
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
