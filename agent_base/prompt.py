import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class PromptAsset:
    name: str
    path: Path
    description: str


PROMPT_ASSETS = {
    "system_base": PromptAsset(
        name="system_base",
        path=PROMPTS_DIR / "system_base.md",
        description="Base general-purpose system prompt for the harness.",
    ),
}


def _read_prompt_asset(asset: PromptAsset) -> str:
    return asset.path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = _read_prompt_asset(PROMPT_ASSETS["system_base"])


def _normalize_extra_blocks(blocks: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw_block in blocks or []:
        block = str(raw_block or "").strip()
        if block:
            normalized.append(block)
    return normalized


def composed_system_prompt(*, current_date: str, extra_blocks: Iterable[str] | None = None) -> str:
    blocks = [SYSTEM_PROMPT.rstrip()]
    for block in _normalize_extra_blocks(extra_blocks):
        blocks.append(block.rstrip())
    blocks.append(f"Current date: {current_date}")
    return "\n\n".join(blocks)


def _show_asset(name: str) -> str:
    asset = PROMPT_ASSETS.get(name)
    if asset is None:
        valid = ", ".join(sorted(PROMPT_ASSETS))
        raise ValueError(f"Unknown prompt asset '{name}'. Available assets: {valid}")
    return _read_prompt_asset(asset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect prompt assets.")
    parser.add_argument("--show-system", action="store_true", help="Print the composed system prompt.")
    parser.add_argument("--show-asset", metavar="NAME", help="Print one prompt asset by name.")
    parser.add_argument("--list-assets", action="store_true", help="List registered prompt assets.")
    parser.add_argument(
        "--with-extra-file",
        action="append",
        default=[],
        dest="extra_files",
        help="Append one extra prompt block file when printing the composed system prompt. May be passed multiple times.",
    )
    args = parser.parse_args(argv)

    extra_blocks = [Path(path).read_text(encoding="utf-8") for path in args.extra_files]

    if args.list_assets:
        for asset in sorted(PROMPT_ASSETS.values(), key=lambda item: item.name):
            print(f"{asset.name}: {asset.description}")
        return 0

    if args.show_asset:
        print(_show_asset(args.show_asset))
        return 0

    if args.show_system:
        print(composed_system_prompt(current_date="<DATE>", extra_blocks=extra_blocks))
        return 0

    print(f"prompt_asset_dir={PROMPTS_DIR}")
    print(f"system_prompt_chars={len(composed_system_prompt(current_date='<DATE>', extra_blocks=extra_blocks))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
