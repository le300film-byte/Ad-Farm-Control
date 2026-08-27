"""control_bot.github_api — thin wrapper around GitHub REST API for workflow control."""
from __future__ import annotations

import os
import time
from typing import Optional

import requests  # uses requests; control bot doesn't need curl_cffi impersonation

from . import config


# Control-server HTTP timeout (seconds). Matches WEBHOOK_TIMEOUT=20 on the
# self-bot side (v5.5.1). Override via CONTROL_HTTP_TIMEOUT if needed.
_HTTP_TIMEOUT = config.CONTROL_HTTP_TIMEOUT


GH = "https://api.github.com"


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


def dispatch_workflow(alt_id: int, inputs: dict) -> tuple[bool, str]:
    """Trigger workflow_dispatch for an alt's repo. Returns (ok, message)."""
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
    except Exception:
        pass
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
    except Exception:
        pass
    return []


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
