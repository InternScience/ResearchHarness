#!/usr/bin/env python3

import argparse
import json
import re
import shutil
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_support import API_TEST_RUNS_DIR, TEST_RUNS_DIR, bootstrap, preview, subprocess_python


SGI_BENCHES = [
    "SGI-DeepResearch",
    "SGI-DryExperiment",
    "SGI-IdeaGeneration",
    "SGI-Reasoning",
    "SGI-WetExperiment",
]


@dataclass
class SGIReadmeCheckResult:
    name: str
    status: str
    duration_seconds: float | None
    detail: str
    output_preview: str = ""
    stderr_preview: str = ""
    server_log_preview: str = ""


def extract_fenced_block_after_heading(readme_text: str, heading: str, language: str) -> str:
    section_start = readme_text.index(heading)
    section = readme_text[section_start:]
    match = re.search(rf"(?m)^(`{{3,}}){re.escape(language)}\s*$", section)
    if not match:
        raise RuntimeError(f"No {language!r} fenced block found after {heading!r}")
    fence = match.group(1)
    content_start = match.end()
    close = re.compile(rf"(?m)^{re.escape(fence)}\s*$").search(section, content_start)
    if not close:
        raise RuntimeError(f"No closing fence found after {heading!r}")
    return section[content_start:close.start()].strip() + "\n"


def readme_path(bench: str) -> Path:
    return ROOT / "benchmarks" / bench / "README.md"


def extract_server_command(bench: str) -> list[str]:
    text = readme_path(bench).read_text(encoding="utf-8")
    raw = extract_fenced_block_after_heading(text, "## Recommended Server Command", "bash")
    command = " ".join(line.rstrip("\\").strip() for line in raw.splitlines() if line.strip())
    parts = shlex.split(command)
    if parts and parts[0] in {"python", "python3"}:
        parts = subprocess_python() + parts[1:]
    return parts


def extract_server_port(command: list[str]) -> int:
    try:
        index = command.index("--port")
        return int(command[index + 1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("Recommended Server Command must include --port.") from exc


def replace_arg_value(command: list[str], name: str, value: str) -> list[str]:
    updated = list(command)
    try:
        index = updated.index(name)
    except ValueError as exc:
        raise RuntimeError(f"Recommended Server Command must include {name}.") from exc
    if index + 1 >= len(updated):
        raise RuntimeError(f"Recommended Server Command has {name} without a value.")
    updated[index + 1] = value
    return updated


def extract_python_example(bench: str) -> str:
    text = readme_path(bench).read_text(encoding="utf-8")
    return extract_fenced_block_after_heading(text, "## OpenAI Test Example", "python")


def replace_example_workspace(python_example: str, workspace: Path) -> str:
    replacement = f"workspace = Path({str(workspace)!r}).resolve()"
    updated, count = re.subn(
        r'workspace = Path\("\./workspace/[^"]+"\)\.resolve\(\)',
        replacement,
        python_example,
        count=1,
    )
    if count != 1:
        raise RuntimeError("README Python example must define a Path('./workspace/...').resolve() workspace.")
    return updated


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_health(port: int, timeout_seconds: float) -> dict:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/v1/health"
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def stop_server(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if process.stdout is None:
        return ""
    return process.stdout.read()


def validate_output(bench: str, output: str) -> tuple[bool, str]:
    if bench == "SGI-DeepResearch":
        answer = re.search(r"<answer>(.*?)</answer>", output, re.S | re.I)
        value = answer.group(1).strip() if answer else ""
        return bool(value), f"answer={value or None}"

    if bench == "SGI-DryExperiment":
        answer = re.search(r"<answer>(.*?)</answer>", output, re.S | re.I)
        body = answer.group(1) if answer else ""
        ok = bool(
            answer
            and "def " in body
            and "calculate_chirp_mass" in body
            and "estimate_final_mass_spin" in body
        )
        return ok, f"has_answer={bool(answer)} function_defs={body.count('def ')}"

    if bench == "SGI-IdeaGeneration":
        text = output.strip()
        if text.startswith("```json"):
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception as exc:
            return False, f"json_parse_error={exc}"
        required = {
            "Idea",
            "ImplementationSteps",
            "ImplementationOrder",
            "Dataset",
            "EvaluationMetrics",
            "ExpectedOutcome",
        }
        missing = sorted(required - set(obj))
        return not missing, f"json_keys={sorted(obj)} missing={missing}"

    if bench == "SGI-Reasoning":
        boxed = re.search(r"\\boxed\{([^}]+)\}", output)
        value = boxed.group(1).strip() if boxed else ""
        return bool(value), f"boxed={value or None}"

    if bench == "SGI-WetExperiment":
        answer = re.search(r"<answer>(.*?)</answer>", output, re.S | re.I)
        body = answer.group(1) if answer else output
        steps = re.findall(r"(?m)^\s*\w+\s*=\s*<[^>]+>\(\s*$", body)
        closings = re.findall(r"(?m)^\s*\)\s*$", body)
        ok = bool(answer and steps and len(closings) >= len(steps))
        return ok, f"has_answer={bool(answer)} steps={len(steps)} closings={len(closings)}"

    raise RuntimeError(f"Unknown SGI benchmark: {bench}")


def run_one_bench(bench: str, server_timeout: float, client_timeout: float) -> SGIReadmeCheckResult:
    started_at = time.time()
    server_command = extract_server_command(bench)
    port = extract_server_port(server_command)
    if port_is_open(port):
        return SGIReadmeCheckResult(
            name=bench,
            status="FAIL",
            duration_seconds=0.0,
            detail=f"Port {port} is already in use; stop the existing server before running this README smoke test.",
        )

    api_runs_dir = API_TEST_RUNS_DIR / "sgi_readme" / bench
    workspace = TEST_RUNS_DIR / "sgi_readme" / bench
    shutil.rmtree(api_runs_dir, ignore_errors=True)
    shutil.rmtree(workspace, ignore_errors=True)
    api_runs_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    server_command = replace_arg_value(server_command, "--api-runs-dir", str(api_runs_dir))
    python_example = replace_example_workspace(extract_python_example(bench), workspace)
    server = subprocess.Popen(
        server_command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        bufsize=1,
    )
    result: SGIReadmeCheckResult
    try:
        wait_for_health(port, server_timeout)
        client = subprocess.run(
            subprocess_python() + ["-c", python_example],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=client_timeout,
        )
        output = client.stdout.strip()
        if client.returncode != 0:
            result = SGIReadmeCheckResult(
                name=bench,
                status="FAIL",
                duration_seconds=round(time.time() - started_at, 3),
                detail=f"README Python example exited with code {client.returncode}.",
                output_preview=preview(output),
                stderr_preview=preview(client.stderr),
            )
            return result
        ok, detail = validate_output(bench, output)
        result = SGIReadmeCheckResult(
            name=bench,
            status="PASS" if ok else "FAIL",
            duration_seconds=round(time.time() - started_at, 3),
            detail=detail,
            output_preview=preview(output),
            stderr_preview=preview(client.stderr),
        )
        return result
    except Exception as exc:
        result = SGIReadmeCheckResult(
            name=bench,
            status="FAIL",
            duration_seconds=round(time.time() - started_at, 3),
            detail=f"{type(exc).__name__}: {exc}",
        )
        return result
    finally:
        server_log = stop_server(server)
        if server_log and "result" in locals() and result.status != "PASS":
            result.server_log_preview = preview(server_log)


def run_checks(benches: list[str], server_timeout: float, client_timeout: float) -> list[SGIReadmeCheckResult]:
    results: list[SGIReadmeCheckResult] = []
    for bench in benches:
        print(f"\n[SGI README] {bench}", flush=True)
        result = run_one_bench(bench, server_timeout=server_timeout, client_timeout=client_timeout)
        print(f"[{result.status}] {result.detail}", flush=True)
        if result.output_preview:
            print(f"  output: {result.output_preview}", flush=True)
        if result.stderr_preview:
            print(f"  stderr: {result.stderr_preview}", flush=True)
        if result.status != "PASS" and result.server_log_preview:
            print(f"  server: {result.server_log_preview}", flush=True)
        results.append(result)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the five SGI benchmark README server commands and OpenAI SDK examples."
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=SGI_BENCHES,
        default=SGI_BENCHES,
        help="Run only the selected SGI README smoke checks.",
    )
    parser.add_argument("--server-timeout", type=float, default=60.0, help="Seconds to wait for server health.")
    parser.add_argument("--client-timeout", type=float, default=1800.0, help="Seconds to wait for each README example.")
    parser.add_argument("--json", action="store_true", help="Print JSON results.")
    return parser.parse_args()


def main() -> int:
    bootstrap()
    args = parse_args()
    results = run_checks(
        benches=list(args.only),
        server_timeout=args.server_timeout,
        client_timeout=args.client_timeout,
    )
    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    failed = [result for result in results if result.status != "PASS"]
    print(f"\nSummary: total={len(results)}, passed={len(results) - len(failed)}, failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
