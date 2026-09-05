"""GitHubClient — the single REST wrapper (repos, secrets, variables, contents, workflows, gists).

Everything goes through ``request()`` so the whole GitHub surface can be faked with one class.
There is **no** dependency on the ``gh`` CLI (L-10); secrets are sealed with PyNaCl in
``secrets.py`` and uploaded through the REST API.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ..core.errors import ExternalServiceError

log = logging.getLogger(__name__)
API = "https://api.github.com"


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None, timeout: float) -> tuple[int, dict[str, str], bytes]: ...


class RequestsTransport:
    """Default transport built on ``requests`` (imported lazily so tests never need it)."""

    def __init__(self):
        import requests  # noqa: WPS433 - lazy import by design

        self._session = requests.Session()

    def request(self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
        resp = self._session.request(method, url, headers=headers, json=json_body, timeout=timeout)
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            return {}


class GitHubClient:
    def __init__(self, token: str, *, transport: HttpTransport | None = None, timeout: float = 20.0, retries: int = 3, user_agent: str = "adfarm-control/9"):
        self.token = (token or "").strip()
        self.transport = transport or RequestsTransport()
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.user_agent = user_agent

    def with_token(self, token: str) -> "GitHubClient":
        """Same transport, different identity (used per worker account)."""
        return GitHubClient(token, transport=self.transport, timeout=self.timeout, retries=self.retries, user_agent=self.user_agent)

    # ── core request ────────────────────────────────────────────────────────
    def request(self, method: str, path: str, *, json_body: Any | None = None, ok: tuple[int, ...] = (), raw: bool = False) -> Response:
        url = path if path.startswith("http") else f"{API}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last: Response | None = None
        for attempt in range(self.retries):
            try:
                status, hdrs, body = self.transport.request(method, url, headers=headers, json_body=json_body, timeout=self.timeout)
            except Exception as exc:  # connection error
                if attempt + 1 == self.retries:
                    raise ExternalServiceError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
                time.sleep(min(2.0 * (attempt + 1), 6.0))
                continue
            last = Response(status, hdrs, body)
            if status in (429,) or (status == 403 and "rate limit" in body.decode("utf-8", "ignore").lower()) or status >= 500:
                if attempt + 1 < self.retries:
                    retry_after = float(hdrs.get("retry-after") or 0) or min(2.0 * (attempt + 1), 8.0)
                    time.sleep(retry_after)
                    continue
            break
        assert last is not None
        if last.ok or last.status in ok:
            return last
        detail = last.json() if not raw else {}
        message = detail.get("message") if isinstance(detail, dict) else ""
        raise ExternalServiceError(f"{method} {path} → HTTP {last.status} {message or ''}".strip(), status=last.status)

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body: Any | None = None, **kw: Any) -> Response:
        return self.request("POST", path, json_body=json_body, **kw)

    def put(self, path: str, json_body: Any | None = None, **kw: Any) -> Response:
        return self.request("PUT", path, json_body=json_body, **kw)

    def patch(self, path: str, json_body: Any | None = None, **kw: Any) -> Response:
        return self.request("PATCH", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.request("DELETE", path, ok=(404,), **kw)

    # ── identity ────────────────────────────────────────────────────────────
    def viewer(self) -> dict[str, Any]:
        return self.get("/user").json()

    def token_scopes(self) -> tuple[str, ...]:
        resp = self.get("/user")
        return tuple(s.strip() for s in resp.headers.get("x-oauth-scopes", "").split(",") if s.strip())

    # ── repositories ────────────────────────────────────────────────────────
    def get_repo(self, owner: str, repo: str) -> Optional[dict[str, Any]]:
        resp = self.request("GET", f"/repos/{owner}/{repo}", ok=(404,))
        return None if resp.status == 404 else resp.json()

    def create_repo(self, name: str, *, private: bool = False, description: str = "", auto_init: bool = True) -> dict[str, Any]:
        return self.post("/user/repos", {"name": name, "private": private, "description": description, "auto_init": auto_init, "has_issues": False, "has_wiki": False, "has_projects": False}).json()

    def rename_repo(self, owner: str, repo: str, new_name: str) -> dict[str, Any]:
        return self.patch(f"/repos/{owner}/{repo}", {"name": new_name}).json()

    def delete_repo(self, owner: str, repo: str) -> bool:
        return self.delete(f"/repos/{owner}/{repo}").status in (204, 404)

    def enable_secret_scanning(self, owner: str, repo: str) -> bool:
        resp = self.request("PATCH", f"/repos/{owner}/{repo}", json_body={"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}, ok=(403, 422))
        return resp.ok

    # ── contents ────────────────────────────────────────────────────────────
    def get_file(self, owner: str, repo: str, path: str, ref: str = "") -> Optional[dict[str, Any]]:
        suffix = f"?ref={ref}" if ref else ""
        resp = self.request("GET", f"/repos/{owner}/{repo}/contents/{path}{suffix}", ok=(404,))
        return None if resp.status == 404 else resp.json()

    def put_file(self, owner: str, repo: str, path: str, content: bytes, message: str, *, branch: str = "") -> dict[str, Any]:
        existing = self.get_file(owner, repo, path, ref=branch)
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(content).decode("ascii")}
        if existing and existing.get("sha"):
            if base64.b64decode((existing.get("content") or "").encode()) == content:
                return existing  # unchanged
            body["sha"] = existing["sha"]
        if branch:
            body["branch"] = branch
        return self.put(f"/repos/{owner}/{repo}/contents/{path}", body).json()

    # ── secrets & variables ─────────────────────────────────────────────────
    def repo_public_key(self, owner: str, repo: str) -> dict[str, str]:
        data = self.get(f"/repos/{owner}/{repo}/actions/secrets/public-key").json()
        return {"key_id": str(data.get("key_id")), "key": str(data.get("key"))}

    def put_secret(self, owner: str, repo: str, name: str, encrypted_value: str, key_id: str) -> None:
        self.put(f"/repos/{owner}/{repo}/actions/secrets/{name}", {"encrypted_value": encrypted_value, "key_id": key_id})

    def delete_secret(self, owner: str, repo: str, name: str) -> None:
        self.delete(f"/repos/{owner}/{repo}/actions/secrets/{name}")

    def list_secret_names(self, owner: str, repo: str) -> list[str]:
        data = self.get(f"/repos/{owner}/{repo}/actions/secrets?per_page=100").json()
        return [s.get("name", "") for s in data.get("secrets", [])]

    def set_variable(self, owner: str, repo: str, name: str, value: str) -> None:
        resp = self.request("PATCH", f"/repos/{owner}/{repo}/actions/variables/{name}", json_body={"name": name, "value": value}, ok=(404,))
        if resp.status == 404:
            self.post(f"/repos/{owner}/{repo}/actions/variables", {"name": name, "value": value})

    # ── workflows ───────────────────────────────────────────────────────────
    def dispatch_workflow(self, owner: str, repo: str, workflow_file: str, inputs: dict[str, Any], ref: str = "main") -> None:
        self.post(f"/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches", {"ref": ref, "inputs": {k: str(v) for k, v in inputs.items()}})

    def list_runs(self, owner: str, repo: str, workflow_file: str = "", *, per_page: int = 10, status: str = "") -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{repo}/actions/" + (f"workflows/{workflow_file}/runs" if workflow_file else "runs") + f"?per_page={int(per_page)}"
        if status:
            path += f"&status={status}"
        resp = self.request("GET", path, ok=(404,))
        return [] if resp.status == 404 else list(resp.json().get("workflow_runs", []))

    def get_run(self, owner: str, repo: str, run_id: int) -> Optional[dict[str, Any]]:
        resp = self.request("GET", f"/repos/{owner}/{repo}/actions/runs/{int(run_id)}", ok=(404,))
        return None if resp.status == 404 else resp.json()

    def cancel_run(self, owner: str, repo: str, run_id: int) -> bool:
        resp = self.request("POST", f"/repos/{owner}/{repo}/actions/runs/{int(run_id)}/cancel", ok=(404, 409))
        return resp.status in (202,)

    def run_logs_url(self, owner: str, repo: str, run_id: int) -> str:
        return f"https://github.com/{owner}/{repo}/actions/runs/{int(run_id)}"

    # ── gists ───────────────────────────────────────────────────────────────
    def get_gist(self, gist_id: str) -> dict[str, Any]:
        return self.get(f"/gists/{gist_id}").json()

    def update_gist(self, gist_id: str, files: dict[str, Optional[str]]) -> dict[str, Any]:
        payload = {"files": {name: ({"content": content} if content is not None else None) for name, content in files.items()}}
        return self.patch(f"/gists/{gist_id}", payload).json()

    def create_gist(self, files: dict[str, str], *, description: str = "", public: bool = False) -> dict[str, Any]:
        return self.post("/gists", {"description": description, "public": public, "files": {k: {"content": v} for k, v in files.items()}}).json()

    def gist_revisions(self, gist_id: str, limit: int = 5) -> list[str]:
        data = self.get(f"/gists/{gist_id}/commits?per_page={int(limit)}").json()
        return [str(c.get("version")) for c in data if isinstance(c, dict) and c.get("version")]

    def get_gist_revision(self, gist_id: str, sha: str) -> dict[str, Any]:
        return self.get(f"/gists/{gist_id}/{sha}").json()

    def gist_file(self, gist_id: str, filename: str) -> Optional[str]:
        files = self.get_gist(gist_id).get("files") or {}
        entry = files.get(filename)
        if not entry:
            return None
        if entry.get("truncated") and entry.get("raw_url"):
            return self.request("GET", entry["raw_url"], raw=True).body.decode("utf-8", "replace")
        return entry.get("content")
