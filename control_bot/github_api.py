"""control_bot.github_api — thin wrapper around GitHub REST API for workflow control."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Optional

import requests  # uses requests; control bot doesn't need curl_cffi impersonation

from . import config


# Control-server HTTP timeout (seconds). Matches WEBHOOK_TIMEOUT=20 on the
# self-bot side (V6). Override via CONTROL_HTTP_TIMEOUT if needed.
_HTTP_TIMEOUT = config.CONTROL_HTTP_TIMEOUT


GH = "https://api.github.com"
GITHUB_API = GH


def _gist_filename(alt_id: int) -> str:
    return f"control_{int(alt_id)}.json"


def fetch_gist(gist_id: str) -> dict:
    """Fetch Gist contents and metadata by gist ID."""
    if not gist_id:
        return {}
    url = f"{GH}/gists/{gist_id}"
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        return {}


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
        existing = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        if existing.status_code == 200:
            files = existing.json().get("files") or {}
            prior = files.get(_gist_filename(alt_id), {})
            raw_prior = prior.get("content") or ""
            parsed_prior = json.loads(raw_prior) if raw_prior else {}
            if isinstance(parsed_prior, dict):
                payload.update({k: v for k, v in parsed_prior.items() if k in {"deal_keywords", "deal_scan_enabled", "deal_alert_delta"}})
    except Exception:
        # The PATCH below remains authoritative; inability to read an old
        # optional setting must not prevent a command from being queued.
        pass
    payload.update({
        "alt_id": int(alt_id),
        "command_id": uuid.uuid4().hex,
        "command": command,
        "args": args.strip()[:1900],
        "issued_at": time.time(),
        "transport": "control_gist",
    })
    if command == "setdealkeywords":
        payload["deal_keywords"] = [item.strip()[:60] for item in args.split(",") if item.strip()][:20]
    elif command == "setdealscan":
        payload["deal_scan_enabled"] = args.casefold().strip() in {"on", "true", "1", "enable", "enabled"}
    elif command == "setdealdelta":
        try:
            value = float(args.strip())
            if 0 <= value <= 5:
                payload["deal_alert_delta"] = value
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        response = requests.patch(
            url,
            headers=_auth_headers(),
            json={"files": {_gist_filename(alt_id): {"content": json.dumps(payload, ensure_ascii=False)}}},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
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


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
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
            headers=_auth_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
        return False, f"Repository lookup failed: {type(exc).__name__}: {exc}"
    if response.status_code == 200:
        return True, "Repository exists."
    if response.status_code == 404:
        return False, "Repository was not found or is not accessible to GH_TOKEN."
    return False, f"Repository lookup returned HTTP {response.status_code}."


def _run_gh_secret(args: list[str], value: str | None = None) -> tuple[bool, str]:
    """Run gh secret operations without putting secret values in argv/logs."""
    if not config.GITHUB_TOKEN:
        return False, "GH_TOKEN is missing."
    if not shutil.which("gh"):
        return False, "GitHub CLI (gh) is not installed on the control runner."
    env = os.environ.copy()
    env["GH_TOKEN"] = config.GITHUB_TOKEN
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
    return _run_gh_secret(["secret", "set", name, "--repo", _repo_slug(repo)], value)


def delete_repository_secret(repo: str, name: str) -> tuple[bool, str]:
    """Delete one repository secret; callers must already have owner auth."""
    if not repo or not name:
        return False, "Repository and secret name are required."
    return _run_gh_secret(["secret", "delete", name, "--repo", _repo_slug(repo), "--yes"])


def delete_repository(repo: str) -> tuple[bool, str]:
    """Delete a repository only when an explicit owner command requested it."""
    if not repo:
        return False, "Repository is empty."
    return _run_gh_secret(["repo", "delete", _repo_slug(repo), "--yes"])


def dispatch_named_workflow(alt_id: int, workflow: str, inputs: dict | None = None) -> tuple[bool, str]:
    """Trigger a named workflow without requiring an alt DM."""
    repo = _repo_for(alt_id)
    if not repo:
        return False, f"Alt {alt_id} has no repository mapped in ALT_REPOS."
    workflow = str(workflow or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow):
        return False, "Workflow filename is invalid."
    url = f"{GH}/repos/{_repo_slug(repo)}/actions/workflows/{workflow}/dispatches"
    payload = {"ref": "main", "inputs": inputs or {}}
    try:
        response = requests.post(url, headers=_auth_headers(), json=payload, timeout=_HTTP_TIMEOUT)
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
    url = f"{GH}/repos/{_repo_slug(repo)}/actions/workflows/{config.WORKFLOW_FILE}/dispatches"
    payload = {"ref": "main", "inputs": inputs}
    try:
        r = requests.post(url, headers=_auth_headers(), json=payload, timeout=_HTTP_TIMEOUT)
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
    url = f"{GH}/repos/{_repo_slug(repo)}/actions/runs?per_page=1"
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            runs = r.json().get("workflow_runs") or []
            if runs:
                return runs[0].get("id")
    except Exception as e:
        print(f"[GH_API] fetch latest run error for {repo}: {e}")
    return None


def cancel_run(alt_id: int) -> tuple[bool, str]:
    """Cancel the most recent run for an alt's repo."""
    repo = _repo_for(alt_id)
    if not repo:
        return False, f"Alt {alt_id} has no repository mapped."
    run_id = _fetch_latest_run_id(repo)
    if not run_id:
        return False, "Could not find an active run to cancel."
    url = f"{GH}/repos/{_repo_slug(repo)}/actions/runs/{run_id}/cancel"
    try:
        r = requests.post(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code in (202, 204):
        return True, f"Sent cancel for run `{run_id}` in `{repo}`."
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def list_runs(alt_id: int, limit: int = 5) -> list[dict]:
    repo = _repo_for(alt_id)
    if not repo:
        return []
    url = f"{GH}/repos/{_repo_slug(repo)}/actions/runs?per_page={limit}"
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("workflow_runs") or []
    except Exception as e:
        print(f"[GH_API] list runs error for {repo}: {e}")
    return []


def upload_repository_file(repo: str, file_path: str, content_bytes: bytes, message: str = "Update file from control bot") -> tuple[bool, str]:
    """Upload or overwrite a file in a GitHub repository."""
    if not repo or not file_path:
        return False, "Repository and file path are required."
    slug = _repo_slug(repo)
    clean_path = file_path.lstrip("/")
    url = f"{GH}/repos/{slug}/contents/{clean_path}"

    sha = None
    try:
        r_get = requests.get(url, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
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
        r_put = requests.put(url, headers=_auth_headers(), json=payload, timeout=_HTTP_TIMEOUT)
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
