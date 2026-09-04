"""control_bot.github_api — thin wrapper around GitHub REST API for workflow control."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

import requests  # uses requests; control bot doesn't need curl_cffi impersonation

from . import config


# Control-server HTTP timeout (seconds). Matches WEBHOOK_TIMEOUT=20 on the
# self-bot side (V6). Override via CONTROL_HTTP_TIMEOUT if needed.
_HTTP_TIMEOUT = config.CONTROL_HTTP_TIMEOUT

# ── Gist queue monitoring counters (TODO 2.2) ───────────────────────────────
_gist_calls: deque[tuple[float, int, float]] = deque(maxlen=10000)  # (ts, status, ms)
_gist_stats_lock = threading.Lock()


def _gist_record(status: int, started: float) -> None:
    with _gist_stats_lock:
        _gist_calls.append((time.time(), int(status), (time.perf_counter() - started) * 1000))


def gist_usage_stats() -> dict[str, int | float]:
    """Aggregate Gist API usage: requests/hour, 429 count, average latency."""
    now = time.time()
    with _gist_stats_lock:
        recent = [c for c in _gist_calls if now - c[0] <= 3600]
    return {
        "requests_last_hour": len(recent),
        "429_count": sum(1 for _ts, status, _ms in recent if status == 429),
        "avg_response_ms": round(sum(c[2] for c in recent) / len(recent), 1) if recent else 0.0,
    }


GH = "https://api.github.com"
GITHUB_API = GH


def _gist_filename(alt_id: int) -> str:
    return f"control_{int(alt_id)}.json"


def fetch_gist(gist_id: str) -> dict:
    """Fetch Gist contents and metadata by gist ID."""
    if not gist_id:
        return {}
    url = f"{GH}/gists/{gist_id}"
    started = time.perf_counter()
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        _gist_record(r.status_code, started)
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        _gist_record(0, started)
        return {}


def fetch_channel_registry_snapshot(alt_id: int) -> dict:
    """Read one per-alt channel registry file from the shared control Gist."""
    gist = fetch_gist(config.CHANNEL_STATE_GIST_ID)
    files = gist.get("files") if isinstance(gist, dict) else None
    raw_file = files.get(f"channel_state_{int(alt_id)}.json", {}) if isinstance(files, dict) else {}
    content = raw_file.get("content", "") if isinstance(raw_file, dict) else ""
    try:
        payload = json.loads(content) if content else {}
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_channel_registry_snapshot(alt_id: int, snapshot: dict) -> tuple[bool, str]:
    """Write only one registry file, preserving command files and other alts."""
    if not config.CHANNEL_STATE_GIST_ID:
        return False, "CHANNEL_STATE_GIST_ID/CONTROL_GIST_ID is not configured."
    if not isinstance(snapshot, dict):
        return False, "Channel registry snapshot must be an object."
    started = time.perf_counter()
    try:
        response = requests.patch(
            f"{GH}/gists/{config.CHANNEL_STATE_GIST_ID}",
            headers=_auth_headers(),
            json={"files": {f"channel_state_{int(alt_id)}.json": {"content": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))}}},
            timeout=_HTTP_TIMEOUT,
        )
        _gist_record(response.status_code, started)
    except Exception as exc:
        _gist_record(0, started)
        return False, f"Channel registry Gist network error: {type(exc).__name__}: {exc}"
    if response.status_code == 200:
        return True, "Channel registry snapshot saved."
    try:
        detail = response.json().get("message", response.text[:200])
    except Exception:
        detail = response.text[:200]
    return False, f"Channel registry Gist HTTP {response.status_code}: {detail}"


def queue_control_command(alt_id: int, text: str) -> tuple[bool, str]:
    """Queue one validated control command in the shared private Gist.

    This is the transport used when the alt must not join the control server.
    A separate file per alt prevents commands for different alts from
    overwriting one another. The alt-side sender polls this file using its
    existing GIST_TOKEN and applies the command exactly once per run.
    """
    if not config.CONTROL_GIST_ID:
        return False, "CONTROL_GIST_ID is not configured."
    raw = str(text or "").strip()
    command, _, args = raw.lstrip("!/").partition(" ")
    command = command.strip().lower()
    if not command or len(command) > 40:
        return False, "Command is empty or invalid."
    url = f"{GH}/gists/{config.CONTROL_GIST_ID}"
    # Preserve durable per-alt settings (especially deal keywords) when a new
    # one-shot command replaces the target file.
    payload = {}
    try:
        _gstart = time.perf_counter()
        existing = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        _gist_record(existing.status_code, _gstart)
        if existing.status_code == 200:
            files = existing.json().get("files") or {}
            prior = files.get(_gist_filename(alt_id), {})
            raw_prior = prior.get("content") or ""
            parsed_prior = json.loads(raw_prior) if raw_prior else {}
            if isinstance(parsed_prior, dict):
                payload.update({k: v for k, v in parsed_prior.items() if k in {
                    "deal_keywords", "deal_scan_enabled", "deal_alert_delta",
                    "paused", "rate", "interval_min", "policy_template", "ad_type", "message"
                }})
    except Exception as _ignored_exc:
        # The PATCH below remains authoritative; inability to read an old
        # optional setting must not prevent a command from being queued.
        print(f"[GH-API] queue_control_command: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    payload.update({
        "alt_id": int(alt_id),
        "command_id": uuid.uuid4().hex,
        "command": command,
        "args": args.strip()[:1900],
        "issued_at": time.time(),
        "transport": "control_gist",
    })
    if command in ("pause",):
        payload["paused"] = True
    elif command in ("resume",):
        payload["paused"] = False
    elif command in ("setprice", "price", "rate"):
        try:
            payload["rate"] = float(args.strip())
        except Exception as _ignored_exc:
            print(f"[GH-API] queue_control_command: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    elif command in ("setmode", "mode"):
        if args.lower().strip() in {"sell", "buy"}:
            payload["ad_type"] = args.lower().strip()
    elif command in ("setmessage", "message"):
        if args.strip():
            payload["message"] = args.strip()[:1900]
    elif command in ("setinterval", "interval"):
        try:
            val = int(args.strip())
            if val in (3, 5):
                payload["interval_min"] = val
        except Exception as _ignored_exc:
            print(f"[GH-API] queue_control_command: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    elif command in ("setruntime", "runtime"):
        try:
            val = int(args.strip())
            if val in (6, 12, 18, 24, 48):
                payload["runtime_hours"] = val
        except Exception as _ignored_exc:
            print(f"[GH-API] queue_control_command: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    elif command == "policy":
        if args.lower().strip() in {"stealth", "aggressive", "peak_hour", "balanced"}:
            payload["policy_template"] = args.lower().strip()
            if args.lower().strip() == "stealth":
                payload["interval_min"] = 5
                payload["deal_scan_enabled"] = False
            elif args.lower().strip() == "aggressive":
                payload["interval_min"] = 3
                payload["deal_scan_enabled"] = True
                payload["deal_alert_delta"] = 0.05
            elif args.lower().strip() == "peak_hour":
                payload["interval_min"] = 3
                payload["deal_scan_enabled"] = True
                payload["deal_alert_delta"] = 0.03
            elif args.lower().strip() == "balanced":
                payload["interval_min"] = 5
                payload["deal_scan_enabled"] = True
                payload["deal_alert_delta"] = 0.05
    elif command in ("setdealkeywords", "dealkeywords"):
        payload["deal_keywords"] = [item.strip()[:60] for item in args.split(",") if item.strip()][:20]
    elif command in ("setdealscan", "dealscan"):
        payload["deal_scan_enabled"] = args.casefold().strip() in {"on", "true", "1", "enable", "enabled"}
    elif command in ("setdealdelta", "dealdelta"):
        try:
            value = float(args.strip())
            if 0 <= value <= 5:
                payload["deal_alert_delta"] = value
        except (TypeError, ValueError, OverflowError) as _ignored_exc:
            print(f"[GH-API] queue_control_command: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    started = time.perf_counter()
    try:
        response = requests.patch(
            url,
            headers=_auth_headers(),
            json={"files": {_gist_filename(alt_id): {"content": json.dumps(payload, ensure_ascii=False)}}},
            timeout=_HTTP_TIMEOUT,
        )
        _gist_record(response.status_code, started)
    except Exception as exc:
        _gist_record(0, started)
        return False, f"Control Gist network error: {type(exc).__name__}: {exc}"
    if response.status_code == 200:
        return True, payload["command_id"]
    try:
        message = response.json().get("message", response.text[:200])
    except Exception:
        message = response.text[:200]
    if response.status_code in {401, 403}:
        return False, f"Control Gist authorization failed (HTTP {response.status_code}): {message}"
    if response.status_code == 404:
        return False, "Control Gist was not found or GH_TOKEN cannot access it."
    return False, f"Control Gist HTTP {response.status_code}: {message}"


def _token_for_repo(repo: str) -> Optional[str]:
    """Best token for a repo slug's OWNER (V8 bug-fix, plan #1).

    Customer/alt repositories live in the WORKER GitHub accounts. The shared
    control token (GH_TOKEN/GH_ADMIN_TOKEN — the main account) cannot dispatch
    workflows, read runs, or manage secrets there, which produced the
    ``GET /repos/<worker>/<repo>/actions/runs → HTTP 404`` failures. When the
    slug's owner matches a configured worker account, that worker's
    fine-grained PAT is used instead. Returns None when no worker matches
    (callers fall back to ``config.GITHUB_TOKEN``).
    """
    slug = str(repo or "").strip()
    if "/" not in slug:
        return None
    owner = slug.split("/")[0].strip().strip("/").lower()
    if not owner:
        return None
    try:
        # WORKER_TOKENS=org1:token1,org2:token2
        raw = os.environ.get("WORKER_TOKENS", "").strip()
        if raw:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" not in pair:
                    continue
                org, tok = pair.split(":", 1)
                if org.strip().lower() == owner and tok.strip():
                    return tok.strip()
        # WORKER_GITHUB_OWNERS + positional WORKER_TOKENS_LIST
        owners = [x.strip() for x in os.environ.get("WORKER_GITHUB_OWNERS", "").split(",") if x.strip()]
        tokens = [x.strip() for x in os.environ.get("WORKER_TOKENS_LIST", "").split(",") if x.strip()]
        for idx, org in enumerate(owners):
            if org.lower() == owner and idx < len(tokens) and tokens[idx]:
                return tokens[idx]
        # WORKER_N_USER / WORKER_N_TOKEN
        for i in range(1, 4):
            user = os.environ.get(f"WORKER_{i}_USER", "").strip()
            tok = os.environ.get(f"WORKER_{i}_TOKEN", "").strip()
            if user and tok and user.lower() == owner:
                return tok
    except Exception:
        return None
    return None


def _auth_headers(repo: str = "") -> dict:
    """Auth headers; worker-owned repo slugs use their worker PAT (plan #1)."""
    token = (_token_for_repo(repo) if repo else None) or config.GITHUB_TOKEN
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "discord-control-bot",
    }


def _repo_for(alt_id: int) -> Optional[str]:
    return config.ALT_REPOS.get(alt_id)


def _repo_slug(repo: str) -> str:
    """Accept either a repo name or an owner/repo slug."""
    return repo if "/" in repo else f"{config.GITHUB_OWNER}/{repo}"


def repository_exists(repo: str) -> tuple[bool, str]:
    """Check that a repository exists without exposing any secret value."""
    if not repo:
        return False, "Repository is empty."
    try:
        response = requests.get(
            f"{GH}/repos/{_repo_slug(repo)}",
            headers=_auth_headers(_repo_slug(repo)),
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
        return False, f"Repository lookup failed: {type(exc).__name__}: {exc}"
    if response.status_code == 200:
        return True, "Repository exists."
    if response.status_code == 404:
        return False, "Repository was not found or is not accessible to GH_TOKEN."
    return False, f"Repository lookup returned HTTP {response.status_code}."


def _run_gh_secret(args: list[str], value: str | None = None, repo: str = "") -> tuple[bool, str]:
    """Run gh secret operations without putting secret values in argv/logs.

    ``repo`` selects the credential: worker-owned slugs authenticate with the
    worker PAT so secrets/deletes work on customer repos (plan #1).
    """
    token = (_token_for_repo(repo) if repo else None) or config.GITHUB_TOKEN
    if not token:
        return False, "GH_TOKEN is missing."
    if not shutil.which("gh"):
        return False, "GitHub CLI (gh) is not installed on the control runner."
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", *args],
            input=(value + "\n") if value is not None else None,
            text=True,
            capture_output=True,
            timeout=_HTTP_TIMEOUT,
            env=env,
            check=False,
        )
    except Exception as exc:
        return False, f"GitHub CLI failed: {type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return True, "GitHub secret operation completed."
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:300]
    return False, detail or f"GitHub CLI exited with {result.returncode}."


def set_repository_secret(repo: str, name: str, value: str) -> tuple[bool, str]:
    """Set a repository secret via stdin so the value is never in argv."""
    if not repo or not name or value is None:
        return False, "Repository, secret name, and value are required."
    slug = _repo_slug(repo)
    return _run_gh_secret(["secret", "set", name, "--repo", slug], value, repo=slug)


def delete_repository_secret(repo: str, name: str) -> tuple[bool, str]:
    """Delete one repository secret; callers must already have owner auth."""
    if not repo or not name:
        return False, "Repository and secret name are required."
    slug = _repo_slug(repo)
    # Try GitHub REST API first
    try:
        url = f"{GH}/repos/{slug}/actions/secrets/{name}"
        r = requests.delete(url, headers=_auth_headers(slug), timeout=_HTTP_TIMEOUT)
        if r.status_code in (204, 200, 404):
            return True, "Secret deleted."
    except Exception as _ignored_exc:
        print(f"[GH-API] delete_repository_secret: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # Fallback to gh CLI (note: gh secret delete does not take --yes)
    return _run_gh_secret(["secret", "delete", name, "--repo", slug], repo=slug)


def delete_repository(repo: str) -> tuple[bool, str]:
    """Delete a repository only when an explicit owner command requested it."""
    if not repo:
        return False, "Repository is empty."
    slug = _repo_slug(repo)
    return _run_gh_secret(["repo", "delete", slug, "--yes"], repo=slug)


def dispatch_named_workflow(alt_id: int, workflow: str, inputs: dict | None = None) -> tuple[bool, str]:
    """Trigger a named workflow without requiring an alt DM."""
    repo = _repo_for(alt_id)
    if not repo:
        return False, f"Alt {alt_id} has no repository mapped in ALT_REPOS."
    workflow = str(workflow or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow):
        return False, "Workflow filename is invalid."
    slug = _repo_slug(repo)
    url = f"{GH}/repos/{slug}/actions/workflows/{workflow}/dispatches"
    payload = {"ref": "main", "inputs": inputs or {}}
    try:
        response = requests.post(url, headers=_auth_headers(slug), json=payload, timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        return False, f"Network error: {type(exc).__name__}: {exc}"
    if response.status_code == 204:
        return True, f"Dispatched `{workflow}` in `{repo}`."
    try:
        message = response.json().get("message", response.text[:200])
    except Exception:
        message = response.text[:200]
    return False, f"HTTP {response.status_code}: {message}"


def dispatch_workflow(alt_id: int, inputs: dict) -> tuple[bool, str]:
    """Trigger the configured sender workflow. Returns (ok, message)."""
    repo = _repo_for(alt_id)
    if not repo:
        return False, f"Alt {alt_id} has no repository mapped in ALT_REPOS."
    slug = _repo_slug(repo)
    url = f"{GH}/repos/{slug}/actions/workflows/{config.WORKFLOW_FILE}/dispatches"
    payload = {"ref": "main", "inputs": inputs}
    try:
        r = requests.post(url, headers=_auth_headers(slug), json=payload, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return False, f"Network error: {type(e).__name__}: {e}"
    if r.status_code == 204:
        # Give GitHub a moment to register the run, then fetch run id
        time.sleep(3)
        run_id = _fetch_latest_run_id(repo)
        return True, f"Dispatched workflow in `{repo}`. Run id: `{run_id or '?'}`."
    # Common failure hints
    try:
        msg = r.json().get("message", r.text[:200])
    except Exception:
        msg = r.text[:200]
    if r.status_code == 404:
        return False, f"404 — check GH_TOKEN has workflow access and that `{repo}/.github/workflows/{config.WORKFLOW_FILE}` exists on branch `main`."
    if r.status_code == 422:
        return False, f"422 — input validation failed: {msg}"
    if r.status_code == 401:
        return False, "401 — GH_TOKEN invalid or missing."
    return False, f"HTTP {r.status_code}: {msg}"


def _fetch_latest_run_id(repo: str) -> Optional[int]:
    slug = _repo_slug(repo)
    url = f"{GH}/repos/{slug}/actions/runs?per_page=1"
    try:
        r = requests.get(url, headers=_auth_headers(slug), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            runs = r.json().get("workflow_runs") or []
            if runs:
                return runs[0].get("id")
    except Exception as e:
        print(f"[GH_API] fetch latest run error for {repo}: {e}")
    return None


def cancel_workflow_run_by_id(run_id: int, repo: str | None = None) -> tuple[bool, str]:
    """Cancel an explicit GitHub Actions workflow run by numeric run ID.

    Used by /shutdown to stop the control-bot's own core workflow after it has
    gracefully stopped every alt.
    """
    if not run_id:
        return False, "No workflow run ID provided."
    slug = repo or config.CORE_REPO
    if not slug:
        return False, "CORE_REPO is not configured."
    url = f"{GH}/repos/{slug}/actions/runs/{int(run_id)}/cancel"
    try:
        r = requests.post(url, headers=_auth_headers(slug), timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code in (202, 204):
        return True, f"Sent cancel for core workflow run `{run_id}` in `{slug}`."
    if r.status_code == 409:
        return True, f"Workflow run `{run_id}` in `{slug}` was already completed."
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def cancel_run(alt_id: int) -> tuple[bool, str]:
    """Cancel the most recent run for an alt's repo."""
    repo = _repo_for(alt_id)
    if not repo:
        return False, f"Alt {alt_id} has no repository mapped."
    run_id = _fetch_latest_run_id(repo)
    if not run_id:
        return True, "No active workflow run found to cancel."
    slug = _repo_slug(repo)
    url = f"{GH}/repos/{slug}/actions/runs/{run_id}/cancel"
    try:
        r = requests.post(url, headers=_auth_headers(slug), timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code in (202, 204):
        return True, f"Sent cancel for run `{run_id}` in `{repo}`."
    if r.status_code == 409:
        return True, f"Workflow run `{run_id}` was already completed."
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def list_runs(alt_id: int, limit: int = 5) -> list[dict]:
    repo = _repo_for(alt_id)
    if not repo:
        return []
    slug = _repo_slug(repo)
    url = f"{GH}/repos/{slug}/actions/runs?per_page={limit}"
    try:
        r = requests.get(url, headers=_auth_headers(slug), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("workflow_runs") or []
    except Exception as e:
        print(f"[GH_API] list runs error for {repo}: {e}")
    return []


def fetch_discord_user_profile(user_token: str) -> tuple[bool, dict]:
    """Validate token and fetch Discord user profile (@me)."""
    if not user_token:
        return False, {"error": "Token is empty."}
    try:
        r = requests.get(
            "https://discord.com/api/v9/users/@me",
            headers={"Authorization": user_token.strip(), "User-Agent": "Mozilla/5.0"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return True, r.json()
        return False, {"error": f"Discord returned HTTP {r.status_code}"}
    except Exception as exc:
        return False, {"error": str(exc)}


def create_alt_repository(repo_slug_or_name: str, private: bool = False) -> tuple[bool, str]:
    """Ensure an alt repository exists on GitHub, creating it if necessary (defaults to public)."""
    slug = _repo_slug(repo_slug_or_name)
    exists, _ = repository_exists(slug)
    if exists:
        return True, slug
    parts = slug.split("/")
    repo_name = parts[-1] if len(parts) > 1 else slug
    owner = parts[0] if len(parts) > 1 else config.GITHUB_OWNER

    if os.environ.get("ALT_REPO_PRIVATE", "").lower() in ("1", "true"):
        private = True

    # V8 bug-fix (plan #1): worker-owned slugs authenticate with the worker
    # PAT — otherwise the org create fails and the fallback silently creates
    # the repo in the MAIN account via /user/repos.
    headers = _auth_headers(slug)

    # Try creating under org or user via REST API
    payload = {"name": repo_name, "private": private, "auto_init": True, "description": f"Ad Farm alt {repo_name}"}
    for url in (f"{GH}/orgs/{owner}/repos" if (owner and owner != config.GITHUB_OWNER) else f"{GH}/user/repos", f"{GH}/user/repos"):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT)
            if r.status_code in (200, 201):
                return True, slug
        except Exception as _ignored_exc:
            print(f"[GH-API] create_alt_repository: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    gh_token = _token_for_repo(slug) or config.GITHUB_TOKEN
    if shutil.which("gh") and gh_token:
        env = os.environ.copy()
        env["GH_TOKEN"] = gh_token
        visibility_flag = "--private" if private else "--public"
        res = subprocess.run(
            ["gh", "repo", "create", slug, visibility_flag, "--add-readme"],
            capture_output=True, text=True, timeout=_HTTP_TIMEOUT, env=env, check=False,
        )
        if res.returncode == 0 or "already exists" in (res.stderr or "").lower():
            return True, slug
    return False, f"Could not create repository `{slug}` on GitHub."


def provision_alt_repository_files_and_secrets(repo: str, user_token: str, channel_ids: str = "") -> tuple[bool, str]:
    """Upload canonical workflows and sender script, and populate required secrets."""
    slug = _repo_slug(repo)
    ok_repo, msg = create_alt_repository(slug)
    if not ok_repo:
        return False, msg

    # Files to sync from local workspace or core repo
    files_to_sync = [
        "send_ads.py",
        ".github/workflows/send_ads.yml",
        ".github/workflows/self_check.yml",
    ]
    repo_root = Path(__file__).resolve().parent.parent
    for rel_path in files_to_sync:
        file_p = repo_root / rel_path
        if not file_p.is_file() and os.path.isfile(rel_path):
            file_p = Path(rel_path)
        content = None
        if file_p.is_file():
            content = file_p.read_bytes()
        elif config.CORE_REPO and config.GITHUB_TOKEN:
            try:
                raw_url = f"https://raw.githubusercontent.com/{config.CORE_REPO}/main/{rel_path}"
                r_raw = requests.get(raw_url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
                if r_raw.status_code == 200:
                    content = r_raw.content
            except Exception as _ignored_exc:
                print(f"[GH-API] provision_alt_repository_files_and_secrets: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        if content:
            upload_repository_file(slug, rel_path, content, message=f"bootstrap: install {rel_path}")

    # Set USER_TOKEN secret
    set_repository_secret(slug, "USER_TOKEN", user_token)

    # Resolve default advertising channels from input, env, or existing fleet state
    resolved_channels = (channel_ids or "").strip() or os.environ.get("CHANNEL_IDS", "").strip() or config._raw("CHANNEL_IDS")
    if not resolved_channels:
        try:
            from control_bot.alt_state import state
            for aid in state.alt_ids:
                a_obj = state.get(aid)
                if a_obj and a_obj.channels:
                    cids = [str(c) for c in a_obj.channels.keys() if str(c).isdigit()]
                    if cids:
                        resolved_channels = ",".join(cids)
                        break
        except Exception as _ignored_exc:
            print(f"[GH-API] provision_alt_repository_files_and_secrets: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    # Clone common secrets from environment or config
    common_secrets = [
        ("DM_WEBHOOK_URL", os.environ.get("DM_WEBHOOK_URL") or config._raw("DM_WEBHOOK_URL")),
        ("LOG_WEBHOOK_URL", os.environ.get("LOG_WEBHOOK_URL") or config._raw("LOG_WEBHOOK_URL")),
        ("DASHBOARD_WEBHOOK_URL", os.environ.get("DASHBOARD_WEBHOOK_URL") or config._raw("DASHBOARD_WEBHOOK_URL")),
        ("DEAL_WEBHOOK_URL", os.environ.get("DEAL_WEBHOOK_URL") or config._raw("DEAL_WEBHOOK_URL")),
        ("GIST_ID", os.environ.get("GIST_ID") or config._raw("GIST_ID")),
        ("GIST_TOKEN", os.environ.get("GIST_TOKEN") or config._raw("GIST_TOKEN") or config.GITHUB_TOKEN),
        ("CONTROL_GIST_ID", config.CONTROL_GIST_ID or os.environ.get("CONTROL_GIST_ID") or ""),
        ("CONTROLLER_USER_IDS", ",".join(str(x) for x in config.OWNER_IDS)),
        ("CHANNEL_IDS", resolved_channels),
    ]
    for sec_name, sec_val in common_secrets:
        if sec_val:
            set_repository_secret(slug, sec_name, str(sec_val))

    return True, f"Repository `{slug}` auto-provisioned successfully with canonical files and secrets."


def upload_repository_file(repo: str, file_path: str, content_bytes: bytes, message: str = "Update file from control bot") -> tuple[bool, str]:
    """Upload or overwrite a file in a GitHub repository."""
    if not repo or not file_path:
        return False, "Repository and file path are required."
    slug = _repo_slug(repo)
    clean_path = file_path.lstrip("/")
    url = f"{GH}/repos/{slug}/contents/{clean_path}"
    headers = _auth_headers(slug)

    sha = None
    try:
        r_get = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        if r_get.status_code == 200:
            sha = r_get.json().get("sha")
    except Exception as e:
        print(f"[GH_API] get file info error for {repo}/{clean_path}: {e}")

    payload = {
        "message": message[:200],
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    try:
        r_put = requests.put(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT)
        if r_put.status_code in (200, 201):
            return True, f"File `{clean_path}` committed to `{slug}`."
        return False, f"HTTP {r_put.status_code}: {r_put.text[:200]}"
    except Exception as e:
        return False, f"Network error: {e}"


def get_authenticated_user() -> dict:
    """Fetch the authenticated GitHub user profile."""
    url = f"{GITHUB_API}/user"
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        return {}


def check_github_api_health() -> tuple[bool, float]:
    """Check GitHub REST API status and latency in milliseconds."""
    t0 = time.perf_counter()
    headers = _auth_headers()
    try:
        r = requests.get(f"{GITHUB_API}/user", headers=headers, timeout=_HTTP_TIMEOUT)
        latency = (time.perf_counter() - t0) * 1000
        return (r.status_code == 200, latency)
    except Exception:
        latency = (time.perf_counter() - t0) * 1000
        return (False, latency)


def refresh_all_run_statuses(state_manager) -> None:
    """Poll GitHub once per alt and update state.workflow_*. Best-effort."""
    for alt_id in state_manager.alt_ids:
        repo = _repo_for(alt_id)
        if not repo:
            continue
        try:
            runs = list_runs(alt_id, limit=1)
        except Exception:
            continue
        if not runs:
            continue
        run = runs[0]
        state_manager.set_workflow(
            alt_id,
            run_id=run.get("id"),
            status=run.get("status", ""),
            conclusion=run.get("conclusion") or "",
        )
