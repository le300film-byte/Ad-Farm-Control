"""github_dispatch.py – V8 Multi-Account GitHub Workflow Dispatcher.

Dispatches GitHub Actions workflows to customer repos across multiple
GitHub accounts using the GH_ADMIN_TOKEN stored as a repository secret.

All secrets are handled via the GitHub REST API:
  PUT /repos/{owner}/{repo}/actions/secrets/{name}

Token security: GH_ADMIN_TOKEN is read from the environment and NEVER
hardcoded or logged.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"

# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _admin_token() -> str:
    tok = os.environ.get("GH_ADMIN_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("No GitHub token configured: set GH_ADMIN_TOKEN or WORKER_TOKENS.")
    return tok


def _worker_tokens() -> dict[str, str]:
    """Parse WORKER_TOKENS=org1:token1,org2:token2 (fine-grained org PATs).

    Returns {org: token}. Falls back to GH_ADMIN_TOKEN as a single default.
    """
    result: dict[str, str] = {}
    raw = os.environ.get("WORKER_TOKENS", "").strip()
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            org, tok = pair.split(":", 1)
            org = org.strip()
            tok = tok.strip()
            if org and tok:
                result[org] = tok
    return result


def token_for_owner(owner: str = "") -> str:
    """Return the best token for *owner*: per-org fine-grained PAT first."""
    workers = _worker_tokens()
    if owner and owner in workers:
        return workers[owner]
    # Also accept org names list matching WORKER_GITHUB_OWNERS order.
    owners = [x.strip() for x in os.environ.get("WORKER_GITHUB_OWNERS", "").split(",") if x.strip()]
    if owner and owners:
        try:
            idx = owners.index(owner)
            tokens = [x.strip() for x in os.environ.get("WORKER_TOKENS_LIST", "").split(",") if x.strip()]
            if idx < len(tokens) and tokens[idx]:
                return tokens[idx]
        except ValueError:
            pass
    return _admin_token()


def get_workers() -> list[tuple[str, str]]:
    """Return list of (username, token) pairs for all worker accounts."""
    result = []
    raw = os.environ.get("WORKER_TOKENS", "").strip()
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                user, tok = pair.split(":", 1)
                user = user.strip()
                tok = tok.strip()
                if user and tok:
                    result.append((user, tok))
    # Fallback to individual WORKER_N_USER/TOKEN secrets
    if not result:
        for i in range(1, 4):
            user = os.environ.get(f"WORKER_{i}_USER", "").strip()
            tok = os.environ.get(f"WORKER_{i}_TOKEN", "").strip()
            if user and tok:
                result.append((user, tok))
    return result


_worker_index = 0

# Cache of {token: login} so resolving the "main account" owner for an empty
# owner string costs at most one GET /user per token.
_LOGIN_CACHE: dict[str, str] = {}


def _norm_owner(owner: str) -> str:
    """Return *owner* stripped of whitespace and surrounding slashes.

    V8 bug-fix C: owners coming from env vars, DB records or CLI input often
    carry trailing/leading slashes (``darkkitty_alt1/``) or whitespace; URL
    paths built from them then contain ``//`` or an empty segment
    (``/repos//reponame`` → HTTP 404).  Normalising here guarantees repo paths
    never contain double slashes.
    """
    return (owner or "").strip().strip("/").strip()


def _norm_repo(repo: str) -> str:
    """Return *repo* stripped of whitespace and surrounding slashes."""
    return (repo or "").strip().strip("/").strip()


def _resolve_owner(owner: str, token: Optional[str] = None) -> str:
    """Return a non-empty URL-safe owner login.

    When *owner* is empty (the "main account" fallback), the owner is resolved
    to the authenticated token's login so URLs like ``/repos//name`` (double
    slash / empty segment, V8 bug-fix C) can never be constructed.
    """
    owner = _norm_owner(owner)
    if owner:
        return owner
    token = token or _admin_token()
    cached = _LOGIN_CACHE.get(token)
    if cached:
        return cached
    me = _request("GET", "/user", token=token)
    login = str((me or {}).get("login") or "").strip()
    if not login:
        raise RuntimeError(
            "Cannot resolve the GitHub owner: the token's /user response has "
            "no login. Refusing to build a repo URL with an empty owner."
        )
    _LOGIN_CACHE[token] = login
    return login


def pick_worker() -> tuple[str, str]:
    """Pick the next worker account in round-robin order.
    
    Returns (username, token) for the next worker. Falls back to main
    account if no workers configured.
    """
    global _worker_index
    workers = get_workers()
    if not workers:
        # Fallback to main account
        tok = _admin_token()
        return ("", tok)
    idx = _worker_index % len(workers)
    _worker_index += 1
    return workers[idx]


def _headers(token: Optional[str] = None) -> dict[str, str]:
    tok = token or _admin_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    expected_statuses: tuple[int, ...] = (200, 201, 204),
) -> Optional[dict]:
    url = f"{GITHUB_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=_headers(token), method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if raw:
                return json.loads(raw)
            return {}
    except HTTPError as exc:
        try:
            raw_body = exc.read()
            if isinstance(raw_body, bytes):
                body_txt = raw_body.decode(errors="replace")[:500]
            elif isinstance(raw_body, str):
                body_txt = raw_body[:500]
            else:
                body_txt = str(exc.reason)[:500]
        except Exception:
            body_txt = str(exc.reason)[:500]
        raise RuntimeError(
            f"GitHub API {method} {path} → HTTP {exc.code}: {body_txt}"
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Repository management
# ──────────────────────────────────────────────────────────────────────────────

def repo_exists(owner: str, repo: str, token: Optional[str] = None) -> bool:
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    tok = token or token_for_owner(owner)
    try:
        owner = _resolve_owner(owner, tok)
        _request("GET", f"/repos/{quote(owner)}/{quote(repo)}", token=tok)
        return True
    except RuntimeError:
        return False


def create_repo(
    owner: str,
    repo: str,
    private: bool = False,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Create a GitHub repository under *owner* (idempotent).

    Phase 0.2 (TODO): customer repos are PUBLIC (``private=False``) — public
    repositories unlock free GitHub Actions minutes.  Pass ``private=True``
    explicitly only when a founder opts a customer into private repos.

    V8 bug-fix D: the repo is first checked with ``GET /repos/{owner}/{repo}``
    and simply *reused* when it already exists — provisioning never fails with
    ``422: name already exists on this account`` and never creates duplicates.
    """
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    # Reuse: an existing repo (e.g. left over from a previous activation or a
    # concurrent provisioning) is returned instead of raising 422.  A GET
    # response only counts as "already exists" when it carries repository
    # fields (id/full_name/name) — this keeps unrelated GET mocks honest.
    try:
        existing = _request("GET", f"/repos/{quote(owner)}/{quote(repo)}", token=token)
        if isinstance(existing, dict) and existing and (
            "id" in existing or "full_name" in existing or "name" in existing
        ):
            print(f"[DISPATCH] Repo {owner}/{repo} already exists — reusing it.")
            return {**existing, "reused": True}
    except RuntimeError:
        pass  # repo does not exist yet → create below
    # Organisation vs. personal account
    try:
        _request("GET", f"/orgs/{quote(owner)}", token=token)
        path = f"/orgs/{quote(owner)}/repos"
    except RuntimeError:
        path = "/user/repos"

    return _request(
        "POST",
        path,
        body={
            "name": repo,
            "private": private,
            "auto_init": True,
            "description": "AdFarm V8 customer alt repository",
        },
        token=token,
    ) or {}


def discover_repo_owner(repo: str) -> str:
    """Find which configured account actually holds *repo* (V8 bug-fix, plan #1).

    Legacy customer records could store an empty ``github_account`` (the old
    silent main-account fallback), which broke every later owner-scoped call
    (``/repos//name`` → HTTP 404 on run cancels, syncs, deletes).  This probes
    every configured worker account first, then the main account, and returns
    the resolved owner login (``""`` when the repo is nowhere reachable).
    """
    repo = _norm_repo(repo)
    if not repo:
        return ""
    for user, tok in get_workers():
        owner = _norm_owner(user)
        if not owner:
            continue
        try:
            if repo_exists(owner, repo, tok):
                return owner
        except Exception:
            continue
    # Main account as the last resort (repos created before workers existed).
    try:
        main_owner = _resolve_owner("")
        if repo_exists(main_owner, repo):
            return main_owner
    except Exception:
        pass
    return ""


def soft_delete_repo(owner: str, repo: str, token: Optional[str] = None) -> dict[str, Any]:
    """Phase 3.2: soft-delete a repo — rename into a 24h quarantine window.

    Hard deletes are irreversible; this renames the repo to
    ``<repo>_DELETED_<timestamp>`` and disables the main workflow so the
    customer cannot keep running while the 24h undo window is open.  Call
    :func:`delete_repo` only after the window has elapsed.
    """
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    new_name = f"{repo}_DELETED_{int(time.time())}"
    _request(
        "PATCH",
        f"/repos/{quote(owner)}/{quote(repo)}",
        body={"name": new_name, "description": "PENDING DELETE — 24h undo window (Phase 3.2)"},
        token=token,
    )
    # Disable the sender workflow so nothing keeps posting during the window.
    try:
        disable_workflow(owner, repo, "send_ads.yml", token=token)
    except Exception as exc:
        print(f"[DISPATCH] Warning: could not disable workflow for {owner}/{repo}: {exc}")
    return {"ok": True, "quarantined_as": new_name, "undo_window_hours": 24}


def disable_workflow(owner: str, repo: str, workflow_file: str, token: Optional[str] = None) -> None:
    """Disable a workflow file (avoids the repo being active during quarantine)."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    _request(
        "PUT",
        f"/repos/{quote(owner)}/{quote(repo)}/actions/workflows/{quote(workflow_file)}/disable",
        token=token,
        expected_statuses=(204,),
    )


def delete_repo(owner: str, repo: str, token: Optional[str] = None) -> bool:
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    try:
        owner = _resolve_owner(owner, token)
        _request("DELETE", f"/repos/{quote(owner)}/{quote(repo)}", token=token)
        return True
    except RuntimeError:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0.2/0.3 — secret scanning, push protection and token verification
# ──────────────────────────────────────────────────────────────────────────────

def enable_repo_secret_protection(owner: str, repo: str, token: Optional[str] = None) -> dict[str, Any]:
    """Enable secret scanning + push protection on one repository.

    Primary endpoint: PUT /repos/{owner}/{repo}/secret-scanning/push-protection
    (recognised for public and private repos; free on public repos).
    Fallback: PATCH /repos/{owner}/{repo} ``security_and_analysis`` for
    installations where the dedicated endpoint is not available.  Both are
    best-effort — a 404/403 means the token lacks admin scope and the runbook
    tells operators to enable it org-wide (V8_RUNBOOKS.md §2.2).
    """
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    res: dict[str, Any] = {"ok": False, "attempts": []}
    try:
        _request(
            "PUT",
            f"/repos/{quote(owner)}/{quote(repo)}/secret-scanning/push-protection",
            token=token,
            expected_statuses=(200, 204),
        )
        res["ok"] = True
        res["attempts"].append("push-protection-endpoint")
    except RuntimeError as exc:
        res["attempts"].append(f"push-protection-endpoint: {exc}")
        try:
            _request(
                "PATCH",
                f"/repos/{quote(owner)}/{quote(repo)}",
                body={
                    "security_and_analysis": {
                        "secret_scanning": {"status": "enabled"},
                        "secret_scanning_push_protection": {"status": "enabled"},
                    }
                },
                token=token,
            )
            res["ok"] = True
            res["attempts"].append("security_and_analysis")
        except RuntimeError as exc2:
            res["attempts"].append(f"security_and_analysis: {exc2}")
    return res


def enable_org_secret_protection(owner: str, token: Optional[str] = None) -> dict[str, Any]:
    """Enable secret scanning + push protection org-wide (best-effort)."""
    owner = _norm_owner(owner)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    res: dict[str, Any] = {"ok": False}
    try:
        _request(
            "PATCH",
            f"/orgs/{quote(owner)}",
            body={
                "security_and_analysis": {
                    "secret_scanning": {"status": "enabled"},
                    "secret_scanning_push_protection": {"status": "enabled"},
                }
            },
            token=token,
        )
        res["ok"] = True
    except RuntimeError as exc:
        res["error"] = str(exc)
    return res


def verify_github_token(owner: str, token: Optional[str] = None) -> dict[str, Any]:
    """Prove WRITE access by creating and deleting a scratch repository.

    Replaces the old `GET /users/{account}` existence check (0.3): existence
    does not imply repo/workflow authority.  A fine-grained PAT scoped to the
    worker org can create a repo under that org, so the scratch repo is created
    through the org endpoint when the owner resolves to an organization.
    """
    token = token or token_for_owner(owner)
    scratch = f"adfarm-token-check-{int(time.time())}-{random.randint(1000, 9999)}"
    result: dict[str, Any] = {"ok": False, "owner": owner, "scratch": scratch}
    # 1. Identity + expiry
    try:
        me = _request("GET", "/user", token=token)
        result["login"] = me.get("login", "")
        result["expires_at"] = me.get("expires_at", "")
        result["plan"] = (me.get("plan") or {}).get("name", "")
        # V8 bug-fix C: never build /repos//… paths — an empty *owner* resolves
        # to the authenticated login for the cleanup DELETE below.
        owner = _norm_owner(owner) or str(result.get("login") or "")
    except RuntimeError as exc:
        result["error"] = f"identity: {exc}"
        return result
    # 2. Create scratch repo (org-scoped when possible)
    created_path = ""
    try:
        _request("GET", f"/orgs/{quote(owner)}", token=token)
        path = f"/orgs/{quote(owner)}/repos"
    except RuntimeError:
        path = "/user/repos"
    try:
        _request(
            "POST", path,
            body={"name": scratch, "private": True, "auto_init": False,
                  "description": "adfarm token write-access verification (auto-deleted)"},
            token=token,
            expected_statuses=(201,),
        )
        if owner:
            created_path = f"/repos/{quote(owner)}/{quote(scratch)}"
        result["created"] = True
        if not created_path:
            result["error"] = "delete: owner login could not be resolved for the cleanup path"
            return result
    except RuntimeError as exc:
        result["error"] = f"create: {exc}"
        return result
    # 3. Delete scratch repo (proof of administration + cleanup)
    try:
        _request("DELETE", created_path, token=token, expected_statuses=(204,))
        result["deleted"] = True
        result["ok"] = True
        result["error"] = ""
    except RuntimeError as exc:
        result["error"] = f"delete: {exc} — scratch repo may need manual cleanup: {scratch}"
    return result


def check_token_status(token: Optional[str] = None, owner: str = "") -> dict[str, Any]:
    """PAT health check: identity, expiry, 401 detection (0.3 health checks).

    Returns ``{"ok": bool, "login": str, "expires_at": str, "days_left": float|None,
    "error": str|None}``.  ``ok=False`` means 401/unauthorized (critical).
    """
    token = token or token_for_owner(owner)
    try:
        me = _request("GET", "/user", token=token)
        expires = str(me.get("expires_at") or "").strip()
        days_left: Optional[float] = None
        if expires and expires not in ("null", "None"):
            try:
                from datetime import datetime, timezone
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
            except Exception:
                days_left = None
        return {
            "ok": True, "login": str(me.get("login", "")), "expires_at": expires,
            "days_left": days_left, "error": None,
        }
    except RuntimeError as exc:
        return {"ok": False, "login": "", "expires_at": "", "days_left": None, "error": str(exc)}


def list_worker_tokens() -> list[dict[str, str]]:
    """Enumerate configured worker tokens for health checks."""
    workers = _worker_tokens()
    out = [{"owner": owner, "token": tok} for owner, tok in workers.items()]
    if not out:
        out = [{"owner": "", "token": os.environ.get("GH_ADMIN_TOKEN", "").strip()}]
    return [item for item in out if item["token"]]


# ──────────────────────────────────────────────────────────────────────────────
# Secret management (uses libsodium-encrypted secrets via GitHub API)
# ──────────────────────────────────────────────────────────────────────────────

def _get_public_key(owner: str, repo: str, token: Optional[str] = None) -> tuple[str, str]:
    """Return (key_id, base64_public_key) for encrypting repository secrets."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    data = _request(
        "GET",
        f"/repos/{quote(owner)}/{quote(repo)}/actions/secrets/public-key",
        token=token,
    )
    return data["key_id"], data["key"]


def _encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Encrypt *secret_value* with the repo's NaCl public key.

    Security hardening (0.8): silently sending a base64 "encrypted" secret
    would look like encryption but is not.  Without PyNaCl we fail loudly and
    tell the operator exactly what to install, unless
    ``ALLOW_BASE64_SECRET_FALLBACK=1`` is explicitly set (offline tests only).
    """
    try:
        from nacl import encoding, public  # type: ignore

        pub_key = public.PublicKey(public_key_b64, encoding.Base64Encoder)
        box = public.SealedBox(pub_key)
        encrypted = box.encrypt(secret_value.encode())
        return base64.b64encode(encrypted).decode()
    except ImportError:
        if os.environ.get("ALLOW_BASE64_SECRET_FALLBACK", "").strip() in {"1", "true", "yes", "on"}:
            return base64.b64encode(secret_value.encode()).decode()
        raise RuntimeError(
            "PyNaCl is required to encrypt GitHub repository secrets "
            "(libsodium sealed boxes). Install it with: pip install pynacl"
        )


def set_repo_secret(
    owner: str,
    repo: str,
    secret_name: str,
    secret_value: str,
    token: Optional[str] = None,
) -> None:
    """Create or update a GitHub Actions secret in a customer repo."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    key_id, pub_key = _get_public_key(owner, repo, token)
    encrypted = _encrypt_secret(pub_key, secret_value)
    _request(
        "PUT",
        f"/repos/{quote(owner)}/{quote(repo)}/actions/secrets/{quote(secret_name)}",
        body={"encrypted_value": encrypted, "key_id": key_id},
        token=token,
        expected_statuses=(201, 204),
    )


# ──────────────────────────────────────────────────────────────────────────────
# File uploads to repos (for send_ads.py, workflows)
# ──────────────────────────────────────────────────────────────────────────────

def upload_file(
    owner: str,
    repo: str,
    path_in_repo: str,
    content: str,
    commit_message: str = "chore: upload file",
    branch: str = "main",
    token: Optional[str] = None,
) -> None:
    """Create or update a file in a GitHub repo via the Contents API."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    encoded = base64.b64encode(content.encode()).decode()
    # Check if the file already exists (to get its sha for update)
    sha: Optional[str] = None
    try:
        existing = _request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/contents/{path_in_repo}",
            token=token,
        )
        sha = existing.get("sha")
    except RuntimeError:
        pass

    body: dict[str, Any] = {
        "message": commit_message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    _request(
        "PUT",
        f"/repos/{quote(owner)}/{quote(repo)}/contents/{path_in_repo}",
        body=body,
        token=token,
        expected_statuses=(200, 201),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Workflow dispatch
# ──────────────────────────────────────────────────────────────────────────────

def dispatch_workflow(
    owner: str,
    repo: str,
    workflow_file: str,
    ref: str = "main",
    inputs: Optional[dict] = None,
    token: Optional[str] = None,
) -> None:
    """Trigger a workflow_dispatch event on a customer repository."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    _request(
        "POST",
        f"/repos/{quote(owner)}/{quote(repo)}/actions/workflows/{quote(workflow_file)}/dispatches",
        body={"ref": ref, "inputs": inputs or {}},
        token=token,
        expected_statuses=(204,),
    )


def cancel_workflow_runs(
    owner: str,
    repo: str,
    token: Optional[str] = None,
) -> int:
    """Cancel all in-progress workflow runs on a customer repository."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    data = _request(
        "GET",
        f"/repos/{quote(owner)}/{quote(repo)}/actions/runs?status=in_progress&per_page=20",
        token=token,
    )
    cancelled = 0
    for run in (data or {}).get("workflow_runs", []):
        try:
            _request(
                "POST",
                f"/repos/{quote(owner)}/{quote(repo)}/actions/runs/{run['id']}/cancel",
                token=token,
                expected_statuses=(202,),
            )
            cancelled += 1
        except RuntimeError:
            pass
    return cancelled


# ──────────────────────────────────────────────────────────────────────────────
# High-level: provision a complete customer alt repository
# ──────────────────────────────────────────────────────────────────────────────

def _read_local(rel_path: str) -> str:
    import pathlib
    p = pathlib.Path(__file__).resolve().parent / rel_path
    return p.read_text(encoding="utf-8")


def provision_alt_repo(
    owner: str,
    repo: str,
    token: Optional[str] = None,
    private: bool = False,
) -> str:
    """Create a customer alt repo and upload core workflow/sender files.

    Phase 0.2: repos are created PUBLIC (free GitHub Actions minutes).
    Phase 0.2/0.3: after upload the repo gets secret scanning + push
    protection enabled (best-effort).

    Returns the HTML URL of the created repo.
    """
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token or token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    # 1. Create the repo (idempotent – skip if it already exists)
    if not repo_exists(owner, repo, token):
        result = create_repo(owner, repo, private=private, token=token)
        html_url = result.get("html_url", f"https://github.com/{owner}/{repo}")
    else:
        html_url = f"https://github.com/{owner}/{repo}"

    # Brief pause after repo creation before uploading files
    time.sleep(2)

    # 2. Upload send_ads.py
    try:
        sender_code = _read_local("send_ads.py")
        upload_file(owner, repo, "send_ads.py", sender_code,
                    "chore: upload send_ads.py", token=token)
    except Exception as exc:
        print(f"[DISPATCH] Warning: could not upload send_ads.py: {exc}")

    # 3. Upload send_ads.yml workflow
    try:
        workflow_code = _read_local(".github/workflows/send_ads.yml")
        upload_file(owner, repo, ".github/workflows/send_ads.yml",
                    workflow_code, "chore: upload send_ads.yml", token=token)
    except Exception as exc:
        print(f"[DISPATCH] Warning: could not upload send_ads.yml: {exc}")

    # 4. Upload self_check.yml workflow
    try:
        self_check_code = _read_local(".github/workflows/self_check.yml")
        upload_file(owner, repo, ".github/workflows/self_check.yml",
                    self_check_code, "chore: upload self_check.yml", token=token)
    except Exception as exc:
        print(f"[DISPATCH] Warning: could not upload self_check.yml: {exc}")

    # 5. Secret hygiene: push protection + secret scanning (best-effort)
    try:
        prot = enable_repo_secret_protection(owner, repo, token)
        if not prot.get("ok"):
            print(f"[DISPATCH] Warning: secret protection not enabled on {owner}/{repo} ({prot.get('attempts')})")
    except Exception as exc:
        print(f"[DISPATCH] Warning: secret protection enable failed: {exc}")

    return html_url


def rename_banned_repo(owner: str, repo: str) -> str:
    """Rename an alt repo to ``<repo>_BANNED_<timestamp>`` (TODO 1.2)."""
    owner = _norm_owner(owner)
    repo = _norm_repo(repo)
    token = token_for_owner(owner)
    owner = _resolve_owner(owner, token)
    new_name = f"{repo}_BANNED_{int(time.time())}"
    _request(
        "PATCH",
        f"/repos/{quote(owner)}/{quote(repo)}",
        body={"name": new_name, "description": "BANNED alt — kept for evidence only"},
        token=token,
    )
    return new_name


def create_replacement_alt_repo(owner: str, repo: str, alt_index: int) -> str:
    """Create a FRESH repo for a replacement alt and re-upload everything."""
    html_url = provision_alt_repo(owner, repo, private=False)
    # A fresh repo must never carry over secrets from the banned repo.
    return html_url


def sync_sender_to_all_repos(
    customers: list[dict],
    token: Optional[str] = None,
) -> dict[str, str]:
    """Push the latest send_ads.py to every customer alt repo.

    Returns a dict of {repo_name: "ok"/"error: ..."}.
    """
    try:
        sender_code = _read_local("send_ads.py")
    except Exception as exc:
        return {"_": f"error: could not read send_ads.py – {exc}"}

    results: dict[str, str] = {}
    for c in customers:
        owner = _norm_owner(c.get("github_account", ""))
        repos = c.get("repos", [])
        for repo in repos:
            repo = _norm_repo(repo)
            key = f"{owner or '?'}/{repo}"
            try:
                upload_file(owner, repo, "send_ads.py", sender_code,
                            "chore: sync send_ads.py", token=token)
                results[key] = "ok"
            except Exception as exc:
                results[key] = f"error: {exc}"
    return results


# ──────────────────────────────────────────────────────────────────────────────
# V8 bug-fix K/L — admin repo inventory + direct deletion from Discord
# ──────────────────────────────────────────────────────────────────────────────

def _paginated_repos(owner: str, token: str) -> list[dict[str, Any]]:
    """List every repository of one account (org-scoped when possible)."""
    owner = _norm_owner(owner)
    try:
        owner = _resolve_owner(owner, token)
    except RuntimeError:
        return []
    try:
        _request("GET", f"/orgs/{quote(owner)}", token=token)
        base = f"/orgs/{quote(owner)}/repos"
    except RuntimeError:
        base = "/user/repos"
    repos: list[dict[str, Any]] = []
    page = 1
    while page <= 10:
        try:
            data = _request("GET", f"{base}?per_page=100&page={page}&type=all", token=token)
        except RuntimeError:
            break
        if not isinstance(data, list) or not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos


def _repo_status_flags(name: str) -> tuple[str, str]:
    """Return (marker, status) for a repo name, e.g. ('_BANNED_', 'banned')."""
    if "_BANNED_" in name:
        return "_BANNED_", "banned"
    if "_DELETED_" in name:
        return "_DELETED_", "quarantined"
    return "", "active"


def list_all_repos(customers: Optional[list[dict]] = None) -> list[dict[str, Any]]:
    """Enumerate every customer alt repo across ALL worker accounts.

    V8 bug-fix K — powers ``/admin repos``.  Each returned entry contains:
    ``owner`` (account login), ``repo`` (name), ``customer`` (Discord username
    or ""), ``customer_id``, ``alt`` (alt index parsed from the name or 0),
    ``status`` (active / banned / quarantined / orphan / listing_error) and
    ``html_url``.  The main account is included as a fallback listing when no
    worker tokens are configured; main-account noise is filtered to
    customer-looking repo names.
    """
    if customers is None:
        try:
            import customer_manager as cm  # lazy import keeps this module DB-free
            customers = cm.list_customers(active_only=False)
        except Exception:
            customers = []
    customers = customers or []

    # repo name → owning customer record (original, pre-rename names)
    owner_for_customer: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for c in customers:
        for repo in c.get("repos") or []:
            repo = _norm_repo(repo)
            by_name.setdefault(repo, c)
        account = _norm_owner(c.get("github_account") or "")
        if account:
            owner_for_customer.setdefault(account, c)

    accounts = get_workers()  # [(username, token)]
    listing_errors: list[str] = []
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _looks_customerish(name: str) -> bool:
        return bool(re.search(r"_alt\d+(_BANNED_\d+)?(_DELETED_\d+)?$", name)
                    or "_BANNED_" in name or "_DELETED_" in name)

    for user, tok in accounts:
        owner = _norm_owner(user)
        try:
            repos = _paginated_repos(owner, tok)
        except RuntimeError as exc:
            entries.append({"owner": user or "(main)", "repo": "",
                            "customer": "", "customer_id": "", "alt": 0,
                            "status": "listing_error", "html_url": "",
                            "error": str(exc)})
            listing_errors.append(f"{user or '(main)'}: {exc}")
            continue
        # When listing the main account only, skip unrelated repositories.
        for item in repos:
            repo = _norm_repo(item.get("name") or "")
            repo_owner = _norm_owner((item.get("owner") or {}).get("login") or "")
            repo_owner = repo_owner or owner or _norm_owner(user)
            key = (repo_owner, repo)
            if key in seen:
                continue
            seen.add(key)
            customer = by_name.get(repo)
            if customer is None and not user and not _looks_customerish(repo):
                # Main-account noise (core repo, docs, …) is not a customer alt.
                continue
            marker, status = _repo_status_flags(repo)
            base_name = repo.split(marker)[0] if marker else repo
            if customer is None:
                customer = by_name.get(base_name) or by_name.get(repo)
            alt = 0
            m = re.search(r"_alt(\d+)", base_name)
            if m:
                alt = int(m.group(1))
            entries.append({
                "owner": repo_owner,
                "repo": repo,
                "customer": (customer or {}).get("discord_username", "") or "",
                "customer_id": (customer or {}).get("discord_id", "") or "",
                "alt": alt,
                "status": status if status != "active" else
                          ("active" if customer and (customer or {}).get("active") else
                           ("orphan" if customer is None else "inactive")),
                "html_url": item.get("html_url") or f"https://github.com/{repo_owner}/{repo}",
            })

    # DB repos that no configured account could list (token/scope gaps) are
    # still reported so the admin can spot missing workers.
    listed_names = {e["repo"] for e in entries if e["repo"]}
    for c in customers:
        for repo in c.get("repos") or []:
            repo = _norm_repo(repo)
            if repo and repo not in listed_names:
                owner = _norm_owner(c.get("github_account") or "")
                entries.append({
                    "owner": owner or "(unknown)",
                    "repo": repo,
                    "customer": c.get("discord_username", "") or "",
                    "customer_id": c.get("discord_id", "") or "",
                    "alt": int(m.group(1)) if (m := re.search(r"_alt(\d+)", repo)) else 0,
                    "status": "unlisted",
                    "html_url": f"https://github.com/{owner or '?'}/{repo}",
                })
                listed_names.add(repo)
    return entries


def delete_customer_repo(
    repo_name: str,
    customers: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Hard-delete a customer alt repo by name (V8 bug-fix L).

    The owner is taken from the customer DB record first, then discovered by
    searching every configured worker account.  Returns ``{"ok": bool,
    "owner": str, "repo": str, "error": str}``.  The caller is responsible for
    requiring an explicit ``DELETE`` confirmation and for updating the
    customer record afterwards.
    """
    repo_name = _norm_repo(repo_name)
    if not repo_name:
        return {"ok": False, "owner": "", "repo": "", "error": "No repo name given."}
    if customers is None:
        try:
            import customer_manager as cm
            customers = cm.list_customers(active_only=False)
        except Exception:
            customers = []
    customers = customers or []

    # 1. Owner from the DB mapping.
    for c in customers:
        for repo in c.get("repos") or []:
            if _norm_repo(repo) != repo_name:
                continue
            owner = _norm_owner(c.get("github_account") or "")
            if not owner:
                continue
            try:
                deleted = delete_repo(owner, repo_name)
            except RuntimeError as exc:
                return {"ok": False, "owner": owner, "repo": repo_name, "error": str(exc)}
            if deleted:
                return {"ok": True, "owner": owner, "repo": repo_name,
                        "customer_id": c.get("discord_id", "") or "", "error": ""}
            return {"ok": False, "owner": owner, "repo": repo_name,
                    "error": "GitHub rejected the delete request."}

    # 2. Fallback: search every worker account for the repo.
    for entry in list_all_repos(customers):
        if entry.get("repo") != repo_name:
            continue
        owner = entry.get("owner") or ""
        if not owner:
            continue
        try:
            deleted = delete_repo(owner, repo_name)
        except RuntimeError as exc:
            return {"ok": False, "owner": owner, "repo": repo_name, "error": str(exc)}
        if deleted:
            return {"ok": True, "owner": owner, "repo": repo_name,
                    "customer_id": entry.get("customer_id", "") or "", "error": ""}
        return {"ok": False, "owner": owner, "repo": repo_name,
                "error": "GitHub rejected the delete request."}

    return {"ok": False, "owner": "", "repo": repo_name,
            "error": "Repo not found on any configured worker account."}
