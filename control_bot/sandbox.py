"""Resource-limited script sandbox for `/script simulate` and `/script run`.

Both actions execute in a separate, temporary Python process. ``simulate`` is
intended for a safe, unfiltered diagnostic run; ``run`` uses the same boundary
for the operator's approved script. The control bot never imports or executes
operator source in its own process.
"""
from __future__ import annotations

import ast
import os
import resource
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any


_BLOCKED_MODULES = {
    "os", "posix", "nt", "subprocess", "multiprocessing", "threading",
    "socket", "ssl", "ctypes", "cffi", "resource", "signal", "pty",
    "asyncio", "selectors", "shutil", "pathlib", "http", "urllib",
    "requests", "curl_cffi", "websocket", "ftplib", "telnetlib",
}
_BLOCKED_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "breakpoint", "system", "popen", "fork", "forkpty", "spawn",
}


def _limits_for(memory_mb: int, cpu_sec: int) -> dict[str, Any]:
    """Return resource::setrlimit keyword values (best-effort)."""
    limits: dict[str, Any] = {}
    mem_bytes = max(16, int(memory_mb or 256)) * 1024 * 1024
    cpu_sec = max(2, int(cpu_sec or 10))
    for name, value in (
        ("RLIMIT_AS", mem_bytes),
        ("RLIMIT_DATA", mem_bytes),
        ("RLIMIT_CPU", cpu_sec),
        ("RLIMIT_NPROC", 32),
        ("RLIMIT_NOFILE", 64),
    ):
        limit = getattr(resource, name, None)
        if limit is not None:
            limits[limit] = (value, value)
    return limits


def _pre_exec(limits: dict[str, Any]):
    def _apply() -> None:
        # ``start_new_session=True`` creates the process group on supported
        # platforms; do not call setsid twice when the pre-exec hook is used.
        os.umask(0o077)
        for limit, value in limits.items():
            try:
                resource.setrlimit(limit, value)
            except (OSError, ValueError, resource.error):
                pass
    return _apply


def _sanitized_env(network: bool = False) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    # Network is opt-in, but process spawning and access to the host are never
    # opt-in. With networking disabled, remove proxy/credential variables and
    # leave only loopback in NO_PROXY as a clear diagnostic environment.
    if not network:
        env.update({
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
    return env


def _static_safety_check(source: str) -> str | None:
    """Reject obvious host/network escape primitives before starting Python."""
    try:
        tree = ast.parse(source, filename="operator_script.py", mode="exec")
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".", 1)[0].casefold()
                if module in _BLOCKED_MODULES:
                    return f"Import of `{alias.name}` is not allowed in the sandbox."
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0].casefold()
            if module in _BLOCKED_MODULES:
                return f"Import from `{node.module}` is not allowed in the sandbox."
        elif isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id.casefold()
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr.casefold()
            if name in _BLOCKED_CALLS:
                return f"Call to `{name}` is not allowed in the sandbox."
        elif isinstance(node, ast.Attribute) and node.attr.casefold() in _BLOCKED_CALLS:
            return f"Access to `{node.attr}` is not allowed in the sandbox."
    return None


def validate_script(text: str, max_chars: int = 20000) -> tuple[bool, str]:
    """Validate size, syntax, and host-escape primitives before execution."""
    if not text or not str(text).strip():
        return False, "Script is empty."
    source = str(text)
    if len(source) > int(max_chars or 20000):
        return False, f"Script is too long ({len(source)} chars; max {max_chars})."
    safety_error = _static_safety_check(source)
    if safety_error:
        return False, safety_error
    return True, "OK"


def _decode_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate the launcher and children, then hard-kill anything remaining."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass


def run_script(
    script: str,
    *,
    timeout_sec: float = 20,
    memory_mb: int = 256,
    cpu_sec: int = 10,
    label: str = "operator-script",
    network: bool = False,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Run source in a restricted process and return unfiltered diagnostics."""
    script = str(script or "")
    ok, msg = validate_script(script, max_chars=max_chars)
    if not ok:
        return {
            "code": 2,
            "stdout": "",
            "stderr": msg,
            "timed_out": False,
            "elapsed": 0.0,
            "error": msg,
        }

    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label or "script")[:40]) or "script"
    limits = _limits_for(memory_mb, cpu_sec)
    env = _sanitized_env(network=network)
    started = time.time()
    proc: subprocess.Popen | None = None

    try:
        with tempfile.TemporaryDirectory(prefix=f"adfarm-{safe_label}-") as tmp:
            user_file = Path(tmp) / "user_script.py"
            user_file.write_text(script, encoding="utf-8")
            launcher = Path(tmp) / "launcher.py"
            launcher.write_text(
                textwrap.dedent(
                    """
                    import runpy, sys, os
                    sys.argv = ["adfarm-script"]
                    os.chdir(os.environ.get("ADFARM_WORKDIR", "."))
                    runpy.run_path(os.environ.get("ADFARM_SCRIPT", "user_script.py"), run_name="__main__")
                    """
                ).strip(),
                encoding="utf-8",
            )
            child_env = dict(env)
            child_env["ADFARM_WORKDIR"] = tmp
            child_env["ADFARM_SCRIPT"] = str(user_file)
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", str(launcher)],
                cwd=tmp,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name != "nt"),
                preexec_fn=_pre_exec(limits) if os.name != "nt" and hasattr(resource, "setrlimit") else None,
            )
            try:
                out, err = proc.communicate(timeout=max(1.0, float(timeout_sec or 20)))
                code = int(proc.returncode or 0)
                timed_out = False
                error = ""
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(proc)
                out, err = exc.output, exc.stderr
                code = 124
                timed_out = True
                error = f"Script exceeded {timeout_sec:g}s wall-clock limit."
            out_text = _decode_output(out)[-20000:]
            err_text = _decode_output(err)[-20000:]
            if not timed_out and code < 0:
                timed_out = abs(code) in {signal.SIGXCPU, signal.SIGKILL, signal.SIGTERM}
                if timed_out:
                    error = f"Script was terminated by a resource/signal limit (exit code {code})."
            return {
                "code": code,
                "stdout": out_text,
                "stderr": err_text,
                "timed_out": timed_out,
                "elapsed": time.time() - started,
                "error": error,
            }
    except (SystemError, OSError) as exc:
        return {
            "code": 125,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "elapsed": time.time() - started,
            "error": f"Sandbox could not start: {exc}",
        }
    except Exception as exc:  # pragma: no cover - defensive host failure
        return {
            "code": 126,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "timed_out": False,
            "elapsed": time.time() - started,
            "error": f"Sandbox host error: {type(exc).__name__}: {exc}",
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(run_script("print('hello world')"))
