"""Run ResearchHarness as a minimal OpenAI-compatible API server."""

from __future__ import annotations

import argparse
import sys

from agent_base.utils import MissingRequiredEnvError, load_default_dotenvs, require_required_env
from api.openai_server import DEFAULT_MAX_CONCURRENT_RUNS, positive_int, serve


def positive_arg(value: str) -> int:
    try:
        return positive_int(value, "--max-concurrent-runs")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve ResearchHarness through /v1/chat/completions.")
    parser.add_argument(
        "--api-runs-dir",
        required=True,
        dest="api_runs_dir",
        help="Directory where the server creates one isolated subdirectory per request.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8686, help="Port to bind. Defaults to 8686.")
    parser.add_argument(
        "--role-prompt-file",
        action="append",
        default=[],
        dest="role_prompt_files",
        help="Optional role prompt file appended to the base ResearchHarness prompt.",
    )
    parser.add_argument(
        "--input-wrapper",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable or disable the input LLM wrapper. Disabled by default.",
    )
    parser.add_argument(
        "--output-wrapper",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable or disable the output LLM wrapper. Disabled by default.",
    )
    parser.add_argument(
        "--max-concurrent-runs",
        type=positive_arg,
        default=DEFAULT_MAX_CONCURRENT_RUNS,
        help=f"Maximum concurrent agent runs handled by this server process. Defaults to {DEFAULT_MAX_CONCURRENT_RUNS}.",
    )
    parser.add_argument(
        "--extra-tool",
        action="append",
        default=[],
        dest="extra_tools",
        metavar="NAME",
        help="Enable one optional extra tool for every API run. Currently supported: str_replace_editor. May be passed multiple times.",
    )
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        dest="tool_names",
        metavar="NAME",
        help="Expose an explicit complete tool set for every API run. May be passed multiple times. Cannot be combined with --extra-tool.",
    )
    args = parser.parse_args(argv)

    load_default_dotenvs()
    try:
        require_required_env("ResearchHarness API server")
        serve(
            api_runs_dir=args.api_runs_dir,
            host=args.host,
            port=args.port,
            role_prompt_files=list(args.role_prompt_files),
            input_wrapper=args.input_wrapper,
            output_wrapper=args.output_wrapper,
            max_concurrent_runs=args.max_concurrent_runs,
            extra_tools=list(args.extra_tools),
            tool_names=list(args.tool_names),
        )
    except (MissingRequiredEnvError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
