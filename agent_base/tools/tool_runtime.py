import argparse
import atexit
import itertools
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from typing import Optional, Union
import sys

from agent_base.utils import PROJECT_ROOT, load_dotenv
from agent_base.tools.tooling import (
    ToolBase,
    command_safety_issue,
    sanitized_subprocess_env,
    validate_tool_path,
    workspace_root,
)


DEFAULT_BUFFER_LIMIT = 200000
DEFAULT_OUTPUT_CHARS = 8192
DEFAULT_YIELD_MS = 200
REPEAT_COLLAPSE_THRESHOLD = 3


def _default_shell() -> str:
    return shutil.which("bash") or "/bin/bash"


def _resolve_cwd(path_value: Optional[str], *, base_root: Optional[Path] = None) -> Path:
    if not path_value:
        return (base_root or workspace_root()).resolve()
    return validate_tool_path(path_value, "Working directory", base_root=base_root)


def _set_terminal_size(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except (ImportError, OSError):
        return


def _disable_echo(fd: int) -> None:
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        return


def _collapse_repeated_lines(text: str, *, threshold: int = REPEAT_COLLAPSE_THRESHOLD) -> str:
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    collapsed: list[str] = []
    current = lines[0]
    count = 1
    for line in lines[1:]:
        if line == current:
            count += 1
            continue
        if count >= threshold:
            collapsed.append(current)
            collapsed.append(f"[previous line repeated {count - 1} additional times]\n")
        else:
            collapsed.extend([current] * count)
        current = line
        count = 1
    if count >= threshold:
        collapsed.append(current)
        collapsed.append(f"[previous line repeated {count - 1} additional times]\n")
    else:
        collapsed.extend([current] * count)
    return "".join(collapsed)


def _bounded_output(text: str, *, max_output_chars: int = DEFAULT_OUTPUT_CHARS) -> str:
    if not text:
        return text
    compressed = _collapse_repeated_lines(text)
    if len(compressed) <= max_output_chars:
        return compressed
    omitted = len(compressed) - max_output_chars
    suffix = f"\n[output truncated: omitted {omitted} chars]\n"
    keep = max(0, max_output_chars - len(suffix))
    return compressed[:keep] + suffix


def _looks_binary_output(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    if b"\x00" in sample:
        return True
    allowed_controls = {9, 10, 12, 13}
    control_count = sum(1 for byte in sample if byte < 32 and byte not in allowed_controls)
    return control_count / max(len(sample), 1) > 0.05


def _truncate_output_bytes(data: bytes, *, max_output_chars: int) -> tuple[bytes, bool, int]:
    max_output_bytes = max(1, int(max_output_chars))
    if len(data) <= max_output_bytes:
        return data, False, 0
    omitted = len(data) - max_output_bytes
    suffix = f"\n[output truncated before decoding: omitted {omitted} bytes]\n".encode("utf-8")
    keep = max(0, max_output_bytes - len(suffix))
    return data[:keep] + suffix, True, omitted


def _safe_process_output(data: Union[str, bytes, None], *, max_output_chars: int = DEFAULT_OUTPUT_CHARS) -> str:
    if data is None or data == "":
        return ""
    if isinstance(data, str):
        return _bounded_output(data, max_output_chars=max_output_chars)

    if not data:
        return ""
    if _looks_binary_output(data):
        note = (
            f"[binary output omitted: {len(data)} bytes. "
            "Use file or a format-aware reader such as openpyxl/scipy.io.loadmat for binary files.]"
        )
        if len(data) > max_output_chars:
            note += f"\n[output truncated before decoding: omitted {len(data) - max_output_chars} bytes]"
        return note

    bounded, _, _ = _truncate_output_bytes(data, max_output_chars=max_output_chars)
    try:
        decoded = bounded.decode("utf-8")
        prefix = ""
    except UnicodeDecodeError:
        decoded = bounded.decode("utf-8", errors="replace")
        prefix = "[non-UTF-8 bytes decoded with replacement characters]\n"
    return _bounded_output(prefix + decoded, max_output_chars=max_output_chars)


class Bash(ToolBase):
    name = "Bash"
    description = (
        "Run a local bash command and return stdout and stderr. This is the primary local execution tool for "
        "shell commands, path operations, ripgrep, git, temporary python3 heredoc scripts, parsing, validation, "
        "and local result transformation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Default is 30.",
            },
            "workdir": {
                "type": "string",
                "description": "Optional working directory for the command. Defaults to the current workspace root.",
            },
            "max_output_chars": {
                "type": "integer",
                "description": f"Maximum combined stdout/stderr characters returned after repeated-line compression. Default is {DEFAULT_OUTPUT_CHARS}.",
            },
        },
        "required": ["command"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[Bash] {exc}"
        base_root = kwargs.get("workspace_root")
        runtime_deadline = kwargs.get("runtime_deadline")

        command = str(params["command"])
        workdir = params.get("workdir")
        try:
            timeout = int(params.get("timeout", 30))
            max_output_chars = int(params.get("max_output_chars", DEFAULT_OUTPUT_CHARS))
        except (TypeError, ValueError):
            return "[Bash] timeout and max_output_chars must be integers."

        issue = command_safety_issue(str(command))
        if issue:
            return f"[Bash] Blocked by safety policy: {issue}"

        try:
            cwd = _resolve_cwd(workdir, base_root=base_root)
        except ValueError as exc:
            return f"[Bash] Invalid or blocked working directory: {exc}"
        if not cwd.exists():
            return f"[Bash] Working directory does not exist: {cwd}"
        if not cwd.is_dir():
            return f"[Bash] Working directory is not a directory: {cwd}"
        if timeout <= 0:
            return "[Bash] timeout must be > 0."
        if max_output_chars <= 0:
            return "[Bash] max_output_chars must be > 0."

        effective_timeout: float = float(timeout)
        if runtime_deadline is not None:
            remaining = float(runtime_deadline) - time.time()
            if remaining <= 0:
                return "[Bash] Agent runtime limit reached before command execution."
            effective_timeout = min(effective_timeout, max(remaining, 0.001))

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=False,
                timeout=effective_timeout,
                cwd=str(cwd),
                env=sanitized_subprocess_env(base_root=base_root),
                executable=shutil.which("bash") or "/bin/bash",
            )
        except subprocess.TimeoutExpired:
            return "[Bash] TimeoutError: Execution timed out."
        except (OSError, subprocess.SubprocessError) as exc:
            return f"[Bash] Error executing command: {exc}"

        parts = [f"exit_code: {proc.returncode}"]
        stdout = _safe_process_output(proc.stdout, max_output_chars=max_output_chars)
        stderr = _safe_process_output(proc.stderr, max_output_chars=max_output_chars)
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)

class TerminalSession:
    def __init__(self, cwd: Path, shell: str, rows: int, cols: int, *, base_root: Optional[Path] = None):
        self.cwd = cwd
        self.shell = shell
        self.rows = rows
        self.cols = cols
        self._buffer_limit = DEFAULT_BUFFER_LIMIT
        self._pending_output = ""
        self._dropped_output_chars = 0
        self._lock = threading.Lock()

        master_fd, slave_fd = pty.openpty()
        _set_terminal_size(slave_fd, rows, cols)
        _disable_echo(slave_fd)

        env = sanitized_subprocess_env(base_root=base_root)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("PS1", "")
        env.setdefault("PROMPT_COMMAND", "")

        self._proc = subprocess.Popen(
            [shell, "--noprofile", "--norc"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=env,
            text=False,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.poll()

    def _reader_loop(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self._master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break

            if not ready:
                if self._proc.poll() is not None:
                    break
                continue

            try:
                data = os.read(self._master_fd, 4096)
            except OSError:
                break

            if not data:
                if self._proc.poll() is not None:
                    break
                continue

            decoded = data.decode("utf-8", errors="replace")
            with self._lock:
                self._pending_output += decoded
                overflow = len(self._pending_output) - self._buffer_limit
                if overflow > 0:
                    self._pending_output = self._pending_output[overflow:]
                    self._dropped_output_chars += overflow

        try:
            os.close(self._master_fd)
        except OSError:
            pass

    def write(self, data: str) -> None:
        if not self.alive:
            raise RuntimeError("session is not running")
        os.write(self._master_fd, data.encode("utf-8", errors="replace"))

    def read(self, yield_time_ms: int = DEFAULT_YIELD_MS, max_output_chars: int = DEFAULT_OUTPUT_CHARS) -> dict:
        if yield_time_ms > 0:
            time.sleep(yield_time_ms / 1000.0)

        with self._lock:
            output = self._pending_output[:max_output_chars]
            self._pending_output = self._pending_output[max_output_chars:]
            remaining_output_chars = len(self._pending_output)
            dropped_output_chars = self._dropped_output_chars
            self._dropped_output_chars = 0

        return {
            "alive": self.alive,
            "returncode": self.returncode,
            "output": output,
            "remaining_output_chars": remaining_output_chars,
            "dropped_output_chars": dropped_output_chars,
            "truncated": remaining_output_chars > 0,
        }

    def interrupt(self, *, max_output_chars: int = DEFAULT_OUTPUT_CHARS) -> dict:
        if not self.alive:
            raise RuntimeError("session is not running")
        os.write(self._master_fd, b"\x03")
        return self.read(yield_time_ms=DEFAULT_YIELD_MS, max_output_chars=max_output_chars)

    def terminate(self, force: bool = False) -> Optional[int]:
        if self.alive:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                self._proc.kill() if force else self._proc.terminate()
            try:
                self._proc.wait(timeout=2 if not force else 1)
            except subprocess.TimeoutExpired:
                if not force:
                    return self.terminate(force=True)
        return self.returncode


class TerminalSessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._sessions: dict[str, TerminalSession] = {}

    def start(self, cwd: Path, shell: str, rows: int, cols: int, *, base_root: Optional[Path] = None) -> tuple[str, TerminalSession]:
        session = TerminalSession(cwd=cwd, shell=shell, rows=rows, cols=cols, base_root=base_root)
        session_id = f"term_{next(self._counter)}"
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def pop(self, session_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def cleanup(self) -> None:
        with self._lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
        for _, session in sessions:
            session.terminate(force=True)


SESSION_MANAGER = TerminalSessionManager()
atexit.register(SESSION_MANAGER.cleanup)


def _format_terminal_response(
    prefix: str,
    session_id: str,
    payload: dict,
    cwd: Optional[Path] = None,
    shell: Optional[str] = None,
    pid: Optional[int] = None,
) -> str:
    lines = [prefix, f"session_id: {session_id}"]
    if pid is not None:
        lines.append(f"pid: {pid}")
    if cwd is not None:
        lines.append(f"cwd: {cwd}")
    if shell is not None:
        lines.append(f"shell: {shell}")
    if "alive" in payload:
        lines.append(f"alive: {str(payload['alive']).lower()}")
    if "returncode" in payload:
        lines.append(f"returncode: {payload['returncode']}")
    if "truncated" in payload:
        lines.append(f"truncated: {str(payload['truncated']).lower()}")
    if "remaining_output_chars" in payload:
        lines.append(f"remaining_output_chars: {payload['remaining_output_chars']}")
    if "dropped_output_chars" in payload:
        lines.append(f"dropped_output_chars: {payload['dropped_output_chars']}")
    if "output" in payload:
        lines.append("output:")
        lines.append(payload["output"])
    return "\n".join(lines)


class TerminalStart(ToolBase):
    name = "TerminalStart"
    description = "Start a persistent local terminal session backed by a PTY shell."
    parameters = {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "description": "Optional working directory for the terminal session. Default is the current workspace root.",
            },
            "shell": {
                "type": "string",
                "description": "Optional shell executable path. Default is bash.",
            },
            "rows": {
                "type": "integer",
                "description": "Terminal row count. Default is 30.",
            },
            "cols": {
                "type": "integer",
                "description": "Terminal column count. Default is 120.",
            },
        },
        "required": [],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[TerminalStart] {exc}"
        base_root = kwargs.get("workspace_root")
        try:
            cwd = _resolve_cwd(params.get("cwd"), base_root=base_root)
            shell = params.get("shell") or _default_shell()
            rows = int(params.get("rows", 30))
            cols = int(params.get("cols", 120))
        except ValueError as exc:
            return f"[TerminalStart] {exc}"
        except (TypeError, OverflowError):
            return "[TerminalStart] rows and cols must be integers."

        if not cwd.exists():
            return f"[TerminalStart] Working directory does not exist: {cwd}"
        if not cwd.is_dir():
            return f"[TerminalStart] Working directory is not a directory: {cwd}"
        if not Path(shell).exists() and shutil.which(shell) is None:
            return f"[TerminalStart] Shell not found: {shell}"
        if rows <= 0 or cols <= 0:
            return "[TerminalStart] rows and cols must both be > 0."

        try:
            session_id, session = SESSION_MANAGER.start(cwd=cwd, shell=shell, rows=rows, cols=cols, base_root=base_root)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"[TerminalStart] Failed to start terminal session: {exc}"

        return _format_terminal_response(
            "[TerminalStart] Started terminal session.",
            session_id=session_id,
            payload={"alive": session.alive, "returncode": session.returncode},
            cwd=cwd,
            shell=shell,
            pid=session.pid,
        )


class TerminalWrite(ToolBase):
    name = "TerminalWrite"
    description = "Write input into an existing terminal session and read back newly produced output."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The terminal session ID returned by TerminalStart.",
            },
            "input": {
                "type": "string",
                "description": "The text to send to the terminal session.",
            },
            "append_newline": {
                "type": "boolean",
                "description": "Whether to append a newline after the provided input. Default is true.",
            },
            "yield_time_ms": {
                "type": "integer",
                "description": "Milliseconds to wait before reading output. Default is 200.",
            },
            "max_output_chars": {
                "type": "integer",
                "description": f"Maximum number of output characters to return. Default is {DEFAULT_OUTPUT_CHARS}.",
            },
        },
        "required": ["session_id", "input"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[TerminalWrite] {exc}"

        session_id = str(params["session_id"])
        input_text = str(params["input"])
        append_newline = bool(params.get("append_newline", True))
        try:
            yield_time_ms = int(params.get("yield_time_ms", DEFAULT_YIELD_MS))
            max_output_chars = int(params.get("max_output_chars", DEFAULT_OUTPUT_CHARS))
        except (TypeError, ValueError):
            return "[TerminalWrite] yield_time_ms and max_output_chars must be integers."

        issue = command_safety_issue(input_text)
        if issue:
            return f"[TerminalWrite] Blocked by safety policy: {issue}"

        session = SESSION_MANAGER.get(session_id)
        if session is None:
            return f"[TerminalWrite] Session not found: {session_id}"
        if max_output_chars <= 0:
            return "[TerminalWrite] max_output_chars must be > 0."
        if yield_time_ms < 0:
            return "[TerminalWrite] yield_time_ms must be >= 0."

        payload_input = input_text + ("\n" if append_newline else "")
        try:
            session.write(payload_input)
            payload = session.read(yield_time_ms=yield_time_ms, max_output_chars=max_output_chars)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"[TerminalWrite] Failed to write to session {session_id}: {exc}"

        return _format_terminal_response("[TerminalWrite] Session updated.", session_id=session_id, payload=payload)


class TerminalRead(ToolBase):
    name = "TerminalRead"
    description = "Read unread output from an existing terminal session."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The terminal session ID returned by TerminalStart.",
            },
            "yield_time_ms": {
                "type": "integer",
                "description": "Milliseconds to wait before reading output. Default is 200.",
            },
            "max_output_chars": {
                "type": "integer",
                "description": f"Maximum number of output characters to return. Default is {DEFAULT_OUTPUT_CHARS}.",
            },
        },
        "required": ["session_id"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[TerminalRead] {exc}"

        session_id = str(params["session_id"])
        try:
            yield_time_ms = int(params.get("yield_time_ms", DEFAULT_YIELD_MS))
            max_output_chars = int(params.get("max_output_chars", DEFAULT_OUTPUT_CHARS))
        except (TypeError, ValueError):
            return "[TerminalRead] yield_time_ms and max_output_chars must be integers."

        session = SESSION_MANAGER.get(session_id)
        if session is None:
            return f"[TerminalRead] Session not found: {session_id}"
        if max_output_chars <= 0:
            return "[TerminalRead] max_output_chars must be > 0."
        if yield_time_ms < 0:
            return "[TerminalRead] yield_time_ms must be >= 0."

        try:
            payload = session.read(yield_time_ms=yield_time_ms, max_output_chars=max_output_chars)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"[TerminalRead] Failed to read session {session_id}: {exc}"

        return _format_terminal_response("[TerminalRead] Session output fetched.", session_id=session_id, payload=payload)


class TerminalKill(ToolBase):
    name = "TerminalKill"
    description = "Terminate an existing terminal session and release its resources."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The terminal session ID returned by TerminalStart.",
            },
            "force": {
                "type": "boolean",
                "description": "Whether to force kill the terminal session immediately. Default is false.",
            },
        },
        "required": ["session_id"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[TerminalKill] {exc}"

        session_id = str(params["session_id"])
        force = bool(params.get("force", False))

        session = SESSION_MANAGER.pop(session_id)
        if session is None:
            return f"[TerminalKill] Session not found: {session_id}"

        try:
            returncode = session.terminate(force=force)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"[TerminalKill] Failed to terminate session {session_id}: {exc}"

        return _format_terminal_response(
            "[TerminalKill] Terminal session terminated.",
            session_id=session_id,
            payload={"alive": False, "returncode": returncode},
        )


class TerminalInterrupt(ToolBase):
    name = "TerminalInterrupt"
    description = "Send Ctrl-C to the foreground process in an existing terminal session while keeping the session alive."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The terminal session ID returned by TerminalStart.",
            },
            "max_output_chars": {
                "type": "integer",
                "description": f"Maximum number of output characters to return after the interrupt. Default is {DEFAULT_OUTPUT_CHARS}.",
            },
        },
        "required": ["session_id"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
        except ValueError as exc:
            return f"[TerminalInterrupt] {exc}"

        session_id = str(params["session_id"])
        try:
            max_output_chars = int(params.get("max_output_chars", DEFAULT_OUTPUT_CHARS))
        except (TypeError, ValueError):
            return "[TerminalInterrupt] max_output_chars must be an integer."

        session = SESSION_MANAGER.get(session_id)
        if session is None:
            return f"[TerminalInterrupt] Session not found: {session_id}"
        if max_output_chars <= 0:
            return "[TerminalInterrupt] max_output_chars must be > 0."

        try:
            payload = session.interrupt(max_output_chars=max_output_chars)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"[TerminalInterrupt] Failed to interrupt session {session_id}: {exc}"

        return _format_terminal_response(
            "[TerminalInterrupt] Sent Ctrl-C to terminal session.",
            session_id=session_id,
            payload=payload,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run runtime and terminal tools directly.")
    subparsers = parser.add_subparsers(dest="tool", required=True)

    bash_parser = subparsers.add_parser("bash", help="Run the Bash tool.")
    bash_parser.add_argument("command")
    bash_parser.add_argument("--timeout", type=int, default=30)
    bash_parser.add_argument("--workdir")

    terminal_parser = subparsers.add_parser("terminal", help="Run a minimal terminal session demo.")
    terminal_parser.add_argument("input", help="Input to send after starting the session.")
    terminal_parser.add_argument("--cwd")
    terminal_parser.add_argument("--yield-time-ms", type=int, default=200)

    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    workdir_root = Path(args.workdir).expanduser().resolve() if getattr(args, "workdir", None) else None

    if args.tool == "bash":
        result = Bash().call(
            {"command": args.command, "timeout": args.timeout, "workdir": args.workdir},
            workspace_root=workdir_root,
        )
        print(result)
        return 0

    terminal_root = Path(args.cwd).expanduser().resolve() if args.cwd else workspace_root()
    start_result = TerminalStart().call({"cwd": str(terminal_root)}, workspace_root=terminal_root)
    print(start_result)
    session_match = re.search(r"session_id: (term_\d+)", start_result)
    if not session_match:
        return 1
    session_id = session_match.group(1)
    write_result = TerminalWrite().call(
        {
            "session_id": session_id,
            "input": args.input,
            "yield_time_ms": args.yield_time_ms,
        },
        workspace_root=terminal_root,
    )
    print(write_result)
    print(TerminalKill().call({"session_id": session_id}, workspace_root=terminal_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
