#!/usr/bin/env python3
"""Offline self-test for the complete V6 runtime source tree.

This checker is deliberately fail-closed and does not deploy anything. It
validates source presence, UTF-8/corruption, Python compilation, key V6 safety
invariants, workflow markers, the sender's network-free self-test, and the
repository test suite when tests are present.

Run from the repository root:
    python3 self_test_all.py
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

RUNTIME_FILES = [
    "send_ads.py",
    "setup.py",
    "control_bot/__init__.py",
    "control_bot/__main__.py",
    "control_bot/alt_state.py",
    "control_bot/bot.py",
    "control_bot/config.py",
    "control_bot/dashboard.py",
    "control_bot/github_api.py",
    "control_bot/run.py",
    "control_bot/requirements.txt",
]

WORKFLOW_MARKERS = {
    ".github/workflows/bootstrap.yml": (
        "workflow_dispatch",
        "setup.py",
        "--non-interactive",
    ),
    ".github/workflows/control_bot.yml": (
        "workflow_dispatch",
        "control_bot",
        "timeout",
    ),
    ".github/workflows/self_check.yml": (
        "workflow_dispatch",
        "send_ads.py",
        "--self-test",
    ),
    ".github/workflows/send_ads.yml": (
        "workflow_dispatch",
        "curl-cffi",
        "WARP",
    ),
    ".github/workflows/sync_to_alts.yml": (
        "workflow_dispatch",
        "sync",
        "ALT_REPOS",
    ),
}

REQUIRED_MARKERS = {
    "send_ads.py": (
        "def main(",
        "def self_test(",
        "--self-test",
        "allowed_mentions",
        "DEAL_WEBHOOK_URL",
    ),
    "setup.py": (
        "class Bootstrap",
        "def preflight(",
        "def set_variable(",
        "def provision_github(",
        "def run_self_checks(",
        "--non-interactive",
        "--abort-on-failure",
    ),
    "control_bot/bot.py": (
        "OWNER_IDS",
        "def _is_owner(",
        "class RunStartView",
        "def _send_dm_wait_ack(",
        "@bot.tree.command(name=\"run\"",
    ),
    "control_bot/config.py": (
        "OWNER_IDS",
        "CONFIGURED_ALT_IDS",
        "ALT_REPOS",
        "ALT_DISCORD_IDS",
    ),
    "control_bot/alt_state.py": (
        "class AltState",
        "class AltStateManager",
        "def update_from_heartbeat(",
        "def mark_offline_stale(",
    ),
    "control_bot/dashboard.py": (
        "def build_summary_embed(",
        "def build_channels_embed(",
        "def build_alerts_embed(",
        "def build_single_alt_embed(",
    ),
    "control_bot/github_api.py": (
        "def dispatch_workflow(",
        "def cancel_run(",
        "def refresh_all_run_statuses(",
    ),
}

FORBIDDEN_MARKERS = {
    "ALT_ROLES": "ALT_ROLES must not be used; authorization is fail-closed via OWNER_IDS.",
}


def fail(label: str, detail: str, failures: list[str]) -> None:
    failures.append(f"{label}: {detail}")
    print(f"❌ {label}: {detail}")


def run_command(args: list[str], timeout: int = 240) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout[-12000:]


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    loaded: dict[str, str] = {}

    print("V6 complete offline self-test")
    print("=" * 72)
    print("No setup, deployment, workflow dispatch, or network provisioning is performed.")

    # Required runtime source and dependency files.
    for relative in RUNTIME_FILES:
        path = ROOT / relative
        if not path.is_file():
            fail("MISSING", relative, failures)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            fail("ENCODING", f"{relative}: {exc}", failures)
            continue
        loaded[relative] = text
        if "\ufffd" in text:
            fail("CORRUPTION", f"{relative} contains U+FFFD", failures)
        if relative.endswith(".py"):
            try:
                tree = ast.parse(text, filename=relative)
                compile(tree, relative, "exec")
            except (SyntaxError, ValueError, TypeError) as exc:
                fail("COMPILE", f"{relative}: {exc}", failures)

    if not failures:
        print(f"✅ Required runtime files present and UTF-8 decodable: {len(loaded)}")

    # Required V6 implementation markers.
    for relative, markers in REQUIRED_MARKERS.items():
        text = loaded.get(relative)
        if text is None:
            continue
        missing = [marker for marker in markers if marker not in text]
        if missing:
            fail("MARKERS", f"{relative} missing: {', '.join(missing)}", failures)
        else:
            print(f"✅ V6 markers: {relative}")

    # Authorization and safety invariants across runtime Python source.
    runtime_python = "\n".join(
        text for relative, text in loaded.items() if relative.endswith(".py")
    )
    for marker, explanation in FORBIDDEN_MARKERS.items():
        if marker in runtime_python:
            fail("SAFETY", explanation, failures)
        else:
            print(f"✅ Safety invariant: {marker} absent")

    bot_text = loaded.get("control_bot/bot.py", "")
    if "return bool(config.OWNER_IDS)" not in bot_text:
        fail("AUTH", "control_bot/bot.py does not visibly fail closed on OWNER_IDS", failures)
    else:
        print("✅ Control authorization visibly fails closed")

    if "allowed_mentions=discord.AllowedMentions.none()" not in bot_text:
        fail("MENTIONS", "control bot does not disable unsolicited mentions", failures)
    else:
        print("✅ Control messages disable unsolicited mentions")

    # Workflow presence and static markers. Do not execute workflows.
    for relative, markers in WORKFLOW_MARKERS.items():
        path = ROOT / relative
        if not path.is_file():
            fail("MISSING WORKFLOW", relative, failures)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            fail("WORKFLOW ENCODING", f"{relative}: {exc}", failures)
            continue
        if "\ufffd" in text:
            fail("WORKFLOW CORRUPTION", f"{relative} contains U+FFFD", failures)
        missing = [marker for marker in markers if marker not in text]
        if missing:
            fail("WORKFLOW MARKERS", f"{relative} missing: {', '.join(missing)}", failures)
        else:
            print(f"✅ Workflow markers: {relative}")

    gitignore = ROOT / ".gitignore"
    if gitignore.is_file():
        ignore_text = gitignore.read_text(encoding="utf-8")
        for marker in ("__pycache__/", "*.py[cod]", "adfarm-core-AI-v6-source.tar.gz"):
            if marker not in ignore_text:
                warnings.append(f".gitignore does not contain {marker}")
        print("✅ .gitignore present")
    else:
        warnings.append(".gitignore is missing")

    # The sender's built-in self-test is explicitly documented as network-free.
    sender = ROOT / "send_ads.py"
    if sender.is_file():
        print("\nRunning send_ads.py --self-test …")
        code, output = run_command([sys.executable, "send_ads.py", "--self-test"])
        if code != 0:
            fail("SENDER SELF-TEST", output, failures)
        elif "ALL SELF-TESTS PASSED" not in output:
            fail("SENDER SELF-TEST", "command exited successfully without its success marker", failures)
        else:
            print("✅ Sender self-test passed")

    tests_dir = ROOT / "tests"
    test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []
    if test_files:
        print(f"\nRunning pytest for {len(test_files)} test files …")
        code, output = run_command([sys.executable, "-m", "pytest", "-q", "tests"], timeout=300)
        if code != 0:
            fail("PYTEST", output, failures)
        else:
            print("✅ Pytest passed")
    else:
        warnings.append("No tests/test_*.py files found; pytest was not run")

    for warning in warnings:
        print(f"⚠️  {warning}")

    print("\n" + "=" * 72)
    if failures:
        print(f"RESULT: FAIL — {len(failures)} check(s) failed")
        return 1
    print("RESULT: PASS — complete V6 offline self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
