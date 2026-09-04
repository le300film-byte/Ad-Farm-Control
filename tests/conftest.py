"""tests/conftest.py — V8 manager cleanup: hermetic, non-destructive test runs.

Guarantees enforced for EVERY test (the plan's "isolated tests, leave no
trace" checklist item):

1. **No production state can be touched.** Ambient secrets/config from the
   developer shell or a CI runner are stripped BEFORE any module under test
   is imported, so a test run can never write to the real backup Gist, a
   real GitHub repo, or a real Discord guild even if a mock leaks. Tests
   that need those knobs set env vars explicitly with ``mock.patch.dict``
   (still honored — the patch happens per-test, after this scrub).
2. **Files land in a temp dir.** customers.db, the control/channel state
   JSON files and the dashboard message-id file are redirected outside the
   repository; the repo working tree stays clean no matter what a test does.
3. **No leakage between tests.** os.environ (and security.OWNER_IDS, which
   some tests reload from the environment) is snapshotted around each test
   and restored afterwards.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 1. per-session scratch directory (auto-removed at interpreter exit) ──────
_TMP = tempfile.mkdtemp(prefix="adfarm-tests-")


def _cleanup_tmp() -> None:
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


import atexit

atexit.register(_cleanup_tmp)

for _var, _fname in (
    ("CUSTOMERS_DB", "customers.db"),
    ("CONTROL_STATE_FILE", "control_state.json"),
    ("CHANNEL_STATE_FILE", "channel_registry.json"),
    ("DASHBOARD_MSG_ID_FILE", "dash_msg_id.txt"),
    ("SHUTDOWN_FLAG_FILE", "shutdown.flag"),
    ("BACKUP_DIR", "backups"),
):
    os.environ.setdefault(_var, os.path.join(_TMP, _fname))

# ── 2. strip ambient credentials / installations before imports ──────────────
#: Names whose real values MUST NOT leak into a test process (secrets and
#: deployment coordinates). Anything a test needs it can set explicitly.
AMBIENT_BLOCKLIST = (
    # Discord
    "BOT_TOKEN", "GUILD_ID", "OWNER_IDS", "OWNER_ID", "BOT_USER_ID",
    "CONTROL_CH_ID", "DASHBOARD_CH_ID", "LOG_CH_ID", "DEALS_CH_ID",
    "DM_INBOX_CH_ID", "OPEN_TICKET_CH_ID", "TICKET_CH_ID",
    "AUDIT_LOG_CH_ID", "ADMIN_ALERTS_CH_ID", "PROOFS_CH_ID",
    # GitHub
    "GH_TOKEN", "GITHUB_PAT", "GH_ADMIN_TOKEN", "GITHUB_TOKEN",
    "REPO_OWNER", "GITHUB_OWNER", "CORE_REPO", "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID", "GITHUB_REPOSITORY_OWNER",
    "WORKER_TOKENS", "WORKER_GITHUB_OWNERS", "WORKER_TOKENS_LIST",
    "WORKER_1_USER", "WORKER_1_TOKEN", "WORKER_2_USER", "WORKER_2_TOKEN",
    "WORKER_3_USER", "WORKER_3_TOKEN",
    # Fleet alt mappings (operator installations keep these as secrets)
    "ALT_REPOS", "ALT_DISCORD_IDS", "ALT_NAMES", "CHANNEL_IDS",
    # Gist backup / state
    "GIST_TOKEN", "CUSTOMERS_GIST_ID", "CONTROL_GIST_ID",
    "CHANNEL_STATE_GIST_ID",
    # Tuning + misc deployment coordinates
    "TUNING_JSON", "TOKEN_VAULT_KEY", "STORE_ALT_TOKENS_IN_DB",
    "WARP_LICENSE_KEY", "WARP_PRIVATE_KEY", "WARP_ENROLL_TOKEN",
    "MAIN_WARP_PRIVATE_KEY",
)
for _name in AMBIENT_BLOCKLIST:
    os.environ.pop(_name, None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_environment():
    """Snapshot/restore os.environ + reloaded globals around every test."""
    env_before = copy.deepcopy(os.environ)
    owner_ids_before = None
    security_before = None
    try:
        import security as _sec
        security_before = _sec
        owner_ids_before = set(_sec.OWNER_IDS)
    except Exception as exc:  # security module expected to import cleanly
        print(f"[CONFTEST] security snapshot skipped: {type(exc).__name__}: {exc}")
    db_before = None
    try:
        import customer_manager as _cm
        db_before = _cm.DB_PATH
    except Exception as exc:
        print(f"[CONFTEST] customer_manager snapshot skipped: {type(exc).__name__}: {exc}")
    # Mutable module-level dicts some tests patch/replace IN PLACE (fleet alt
    # registry). Without this, one suite's leftovers leak into another's view
    # of "the global config" and only fail in full-suite ordering.
    mirror_before = []
    try:
        import config as _cfg
        for _name in ("ALT_REPOS", "ALT_DISCORD_IDS", "ALT_NAMES"):
            mirror_before.append((_cfg, _name, dict(getattr(_cfg, _name, {}) or {})))
    except Exception as exc:
        print(f"[CONFTEST] config mirror snapshot skipped: {type(exc).__name__}: {exc}")
    if security_before is not None:
        mirror_before.append(
            (security_before, "CHANNEL_RULES",
             {k: set(v) for k, v in security_before.CHANNEL_RULES.items()}))
    yield
    os.environ.clear()
    os.environ.update(env_before)
    if security_before is not None and owner_ids_before is not None:
        security_before.OWNER_IDS = owner_ids_before
    if db_before is not None:
        try:
            import customer_manager as _cm
            _cm.DB_PATH = db_before
        except Exception as exc:
            print(f"[CONFTEST] customer_manager restore skipped: {type(exc).__name__}: {exc}")
    for _obj, _name, _value in mirror_before:
        try:
            _current = getattr(_obj, _name)
            if isinstance(_current, dict) and isinstance(_value, dict):
                _current.clear()
                _current.update(_value)
            else:
                setattr(_obj, _name, _value)
        except Exception as exc:  # pragma: no cover - best-effort restore
            print(f"[CONFTEST] mirror restore skipped for {_name}: {type(exc).__name__}: {exc}")


@pytest.fixture()
def tmp_db_path(tmp_path):
    """A ready-to-use temporary customers.db for direct DB tests."""
    return str(tmp_path / "customers-test.db")
