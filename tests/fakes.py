"""In-memory fakes for GitHub (REST), Gists, Discord and Discord token checks.

``FakeGitHubTransport`` implements ``HttpTransport`` with a model of repos/secrets/variables/
contents/workflow runs/gists so the *real* ``GitHubClient`` and every layer above it are
exercised; only the network is replaced. Nothing here can reach a real service.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from nacl import encoding, public

from adfarm.discord.ports import ChannelRef, DiscordPort, Embed, ForumResult, ForumSpec, MessageRef
from adfarm.services.alts import TokenCheck


# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class FakeRepo:
    owner: str
    name: str
    private: bool = False
    files: dict[str, bytes] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)      # name → sealed (base64)
    variables: dict[str, str] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    secret_scanning: bool = False


class FakeGitHubTransport:
    """Stateful model behind the REST paths GitHubClient uses."""

    def __init__(self, *, tokens: dict[str, str] | None = None, fail_dispatch: bool = False):
        self.tokens = dict(tokens or {})            # token → login
        self.repos: dict[str, FakeRepo] = {}
        self.gists: dict[str, dict[str, Any]] = {}  # id → {"files": {name: {"content": str}}, "history": [files...]}
        self.calls: list[tuple[str, str]] = []
        self.fail_dispatch = fail_dispatch
        self.fail_next: list[tuple[str, int]] = []  # (path substring, status) → injected failures
        self.down: set[str] = set()                 # logins whose token yields 503 on every call (worker outage)
        self._run_seq = 1000
        self._key = public.PrivateKey.generate()
        self.auto_start_runs = True

    # ── helpers ────────────────────────────────────────────────────────────
    def add_gist(self, gist_id: str, files: dict[str, str] | None = None) -> None:
        self.gists[gist_id] = {"files": {k: {"content": v} for k, v in (files or {}).items()}, "history": []}

    def repo(self, owner: str, name: str) -> Optional[FakeRepo]:
        return self.repos.get(f"{owner}/{name}".lower())

    def unseal(self, sealed: str) -> str:
        return public.SealedBox(self._key).decrypt(base64.b64decode(sealed)).decode()

    def secret(self, owner: str, name: str, key: str) -> Optional[str]:
        repo = self.repo(owner, name)
        if not repo or key not in repo.secrets:
            return None
        return self.unseal(repo.secrets[key])

    def login_for(self, headers: dict[str, str]) -> str:
        auth = headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if " " in auth else ""
        return self.tokens.get(token, "")

    def _resp(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        raw = b"" if body is None else json.dumps(body).encode()
        return status, headers or {}, raw

    # ── HttpTransport ──────────────────────────────────────────────────────
    def request(self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.calls.append((method, path))
        for i, (needle, status) in enumerate(list(self.fail_next)):
            if needle in path:
                self.fail_next.pop(i)
                return self._resp(status, {"message": "injected failure"})
        login = self.login_for(headers)
        if login == "" and "Authorization" in headers:
            return self._resp(401, {"message": "Bad credentials"})
        if login in self.down:
            return self._resp(503, {"message": "Service Unavailable (simulated outage)"})

        if path == "/user":
            return self._resp(200, {"login": login, "id": abs(hash(login)) % 10000}, {"x-oauth-scopes": "repo, workflow, gist"})
        if path == "/user/repos" and method == "POST":
            name = json_body["name"]
            key = f"{login}/{name}".lower()
            if key in self.repos:
                return self._resp(422, {"message": "name already exists"})
            self.repos[key] = FakeRepo(owner=login, name=name, private=bool(json_body.get("private")))
            return self._resp(201, {"full_name": f"{login}/{name}", "name": name, "owner": {"login": login}})
        if path == "/gists" and method == "POST":
            gid = f"gist{len(self.gists) + 1}"
            self.add_gist(gid, {k: v["content"] for k, v in json_body["files"].items()})
            return self._resp(201, {"id": gid, "files": self.gists[gid]["files"]})

        m = re.match(r"^/gists/([^/]+)(?:/(.+))?$", path)
        if m:
            return self._gist(method, m.group(1), m.group(2) or "", json_body, query)
        m = re.match(r"^/repos/([^/]+)/([^/]+)(?:/(.*))?$", path)
        if m:
            return self._repo(method, m.group(1), m.group(2), m.group(3) or "", json_body, query, login)
        return self._resp(404, {"message": f"unhandled {method} {path}"})

    # ── gists ──────────────────────────────────────────────────────────────
    def _gist(self, method: str, gid: str, rest: str, body: Any, query: dict) -> tuple[int, dict[str, str], bytes]:
        gist = self.gists.get(gid)
        if gist is None:
            return self._resp(404, {"message": "Not Found"})
        if rest == "" and method == "GET":
            return self._resp(200, {"id": gid, "files": gist["files"]})
        if rest == "" and method == "PATCH":
            gist["history"].append(json.loads(json.dumps(gist["files"])))
            for name, entry in (body or {}).get("files", {}).items():
                if entry is None:
                    gist["files"].pop(name, None)
                else:
                    gist["files"][name] = {"content": entry.get("content", "")}
            return self._resp(200, {"id": gid, "files": gist["files"]})
        if rest == "commits":
            return self._resp(200, [{"version": f"rev{i}"} for i in range(len(gist["history"]) - 1, -1, -1)])
        rm = re.match(r"^rev(\d+)$", rest)
        if rm and method == "GET":
            idx = int(rm.group(1))
            if idx < len(gist["history"]):
                return self._resp(200, {"id": gid, "files": gist["history"][idx]})
            return self._resp(404, {"message": "no such revision"})
        return self._resp(404, {"message": f"unhandled gist op {rest}"})

    # ── repos ──────────────────────────────────────────────────────────────
    def _repo(self, method: str, owner: str, name: str, rest: str, body: Any, query: dict, login: str) -> tuple[int, dict[str, str], bytes]:
        repo = self.repo(owner, name)
        if repo is None:
            return self._resp(404, {"message": "Not Found"})
        if rest == "":
            if method == "GET":
                return self._resp(200, {"full_name": f"{repo.owner}/{repo.name}", "name": repo.name, "private": repo.private, "owner": {"login": repo.owner}})
            if method == "PATCH":
                if "name" in (body or {}):
                    del self.repos[f"{repo.owner}/{repo.name}".lower()]
                    repo.name = body["name"]
                    self.repos[f"{repo.owner}/{repo.name}".lower()] = repo
                if "security_and_analysis" in (body or {}):
                    repo.secret_scanning = True
                return self._resp(200, {"full_name": f"{repo.owner}/{repo.name}", "name": repo.name})
            if method == "DELETE":
                del self.repos[f"{repo.owner}/{repo.name}".lower()]
                return self._resp(204)
        if rest.startswith("contents/"):
            fpath = rest[len("contents/"):]
            if method == "GET":
                if fpath not in repo.files:
                    return self._resp(404, {"message": "Not Found"})
                return self._resp(200, {"path": fpath, "sha": f"sha-{abs(hash(repo.files[fpath])) % 99999}", "content": base64.b64encode(repo.files[fpath]).decode()})
            if method == "PUT":
                repo.files[fpath] = base64.b64decode(body["content"])
                return self._resp(201, {"content": {"path": fpath, "sha": "new"}})
        if rest == "actions/secrets/public-key":
            return self._resp(200, {"key_id": "kid-1", "key": self._key.public_key.encode(encoding.Base64Encoder()).decode()})
        if rest == "actions/secrets":
            return self._resp(200, {"secrets": [{"name": n} for n in repo.secrets]})
        sm = re.match(r"^actions/secrets/([A-Z0-9_]+)$", rest)
        if sm:
            if method == "PUT":
                repo.secrets[sm.group(1)] = body["encrypted_value"]
                return self._resp(201)
            if method == "DELETE":
                repo.secrets.pop(sm.group(1), None)
                return self._resp(204)
        if rest == "actions/variables" and method == "POST":
            repo.variables[body["name"]] = body["value"]
            return self._resp(201)
        vm = re.match(r"^actions/variables/([A-Z0-9_]+)$", rest)
        if vm and method == "PATCH":
            if vm.group(1) not in repo.variables:
                return self._resp(404, {"message": "Not Found"})
            repo.variables[vm.group(1)] = body["value"]
            return self._resp(204)
        dm = re.match(r"^actions/workflows/([^/]+)/dispatches$", rest)
        if dm and method == "POST":
            if self.fail_dispatch:
                return self._resp(422, {"message": "workflow not found"})
            self._run_seq += 1
            run = {"id": self._run_seq, "status": "queued" if not self.auto_start_runs else "in_progress", "conclusion": None, "created_at": "2026-09-04T10:00:00Z",
                   "html_url": f"https://github.com/{repo.owner}/{repo.name}/actions/runs/{self._run_seq}", "path": f".github/workflows/{dm.group(1)}"}
            repo.runs.insert(0, run)
            repo.dispatches.append({"workflow": dm.group(1), "inputs": body.get("inputs", {}), "ref": body.get("ref")})
            return self._resp(204)
        lm = re.match(r"^actions/workflows/([^/]+)/runs$", rest)
        if lm:
            wf = f".github/workflows/{lm.group(1)}"
            runs = [r for r in repo.runs if r["path"] == wf]
            return self._resp(200, {"workflow_runs": runs[: int(query.get("per_page", ["10"])[0])]})
        if rest == "actions/runs":
            return self._resp(200, {"workflow_runs": repo.runs[: int(query.get("per_page", ["10"])[0])]})
        rm = re.match(r"^actions/runs/(\d+)(?:/(cancel))?$", rest)
        if rm:
            run = next((r for r in repo.runs if r["id"] == int(rm.group(1))), None)
            if run is None:
                return self._resp(404, {"message": "Not Found"})
            if rm.group(2) == "cancel":
                if run["status"] not in ("queued", "in_progress"):
                    return self._resp(409, {"message": "not active"})
                run["status"], run["conclusion"] = "completed", "cancelled"
                return self._resp(202)
            return self._resp(200, run)
        return self._resp(404, {"message": f"unhandled repo op {method} {rest}"})


# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class SentMessage:
    channel_id: str
    content: str
    embed: Optional[Embed] = None
    view: Any = None


class FakeDiscord(DiscordPort):
    def __init__(self):
        self.channels: dict[str, ChannelRef] = {}
        self.members: set[str] = set()
        self.names: dict[str, str] = {}
        self.sent: list[SentMessage] = []
        self.dms: list[tuple[str, str, Optional[Embed]]] = []
        self.roles: dict[str, set[str]] = {}
        self.readonly: dict[str, bool] = {}
        self.deleted: list[str] = []
        self.pinned: list[tuple[str, str]] = []
        self.webhook_calls = 0
        self._seq = 900_000_000_000_000_000
        self.forums: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[MessageRef]] = {}
        self.forum_specs: list[ForumSpec] = []            # every ForumSpec handed to create_customer_forum
        self.threads_created: list[tuple[str, str, str]] = []   # (parent channel, name, content)

    def _next(self) -> str:
        self._seq += 1
        return str(self._seq)

    def add_channel(self, channel_id: str, name: str, *, kind: str = "text", parent_id: str = "", category_id: str = "", category_name: str = "", guild_id: str = "1") -> ChannelRef:
        ref = ChannelRef(id=channel_id, name=name, kind=kind, parent_id=parent_id, category_id=category_id, category_name=category_name, guild_id=guild_id)
        self.channels[channel_id] = ref
        return ref

    async def get_channel(self, channel_id: str) -> Optional[ChannelRef]:
        return self.channels.get(channel_id)

    async def find_channel_by_name(self, name: str) -> Optional[ChannelRef]:
        return next((c for c in self.channels.values() if c.name == name), None)

    async def member_exists(self, user_id: str) -> bool:
        return user_id in self.members

    async def display_name(self, user_id: str) -> str:
        return self.names.get(user_id, "")

    async def send(self, channel_id: str, content: str = "", *, embed: Embed | None = None, view: Any | None = None) -> Optional[str]:
        if channel_id not in self.channels:
            return None
        self.sent.append(SentMessage(channel_id, content, embed, view))
        return self._next()

    async def edit_message(self, channel_id: str, message_id: str, content: str = "", *, embed: Embed | None = None) -> bool:
        return channel_id in self.channels

    async def dm(self, user_id: str, content: str, *, embed: Embed | None = None) -> bool:
        self.dms.append((user_id, content, embed))
        return user_id in self.members

    async def recent_messages(self, channel_id: str, limit: int = 50) -> list[MessageRef]:
        return self.history.get(channel_id, [])[:limit]

    async def pin(self, channel_id: str, message_id: str) -> bool:
        self.pinned.append((channel_id, message_id))
        return True

    async def create_customer_forum(self, spec: ForumSpec) -> ForumResult:
        self.forum_specs.append(spec)
        existing = next((fid for fid, f in self.forums.items() if f["name"] == spec.name), None)
        created = existing is None
        fid = existing or self._next()
        if created:
            self.forums[fid] = {"name": spec.name, "threads": {}, "customer": spec.customer_user_id}
            self.add_channel(fid, spec.name, kind="forum", category_id=spec.category_id, category_name="🏢 Customer Hub")
        threads = self.forums[fid]["threads"]
        for role, tname, _opening in spec.threads:
            if role not in threads:
                tid = self._next()
                threads[role] = tid
                self.add_channel(tid, tname, kind="thread", parent_id=fid, category_id=spec.category_id, category_name="🏢 Customer Hub")
        webhooks = await self.ensure_forum_webhooks(fid, dict(threads))
        return ForumResult(forum_id=fid, thread_ids=dict(threads), webhooks=webhooks, created=created)

    async def ensure_forum_webhooks(self, forum_id: str, thread_ids: dict[str, str]) -> dict[str, str]:
        self.webhook_calls += 1
        return {role: f"https://discord.com/api/webhooks/{forum_id}/tok_{role}_{'x' * 30}?thread_id={tid}" for role, tid in thread_ids.items() if role in ("dashboard", "farm-logs", "deals", "dm-inbox")}

    async def create_thread(self, channel_id: str, name: str, content: str = "") -> str:
        if channel_id not in self.channels:
            return ""
        tid = self._next()
        self.add_channel(tid, name, kind="thread", parent_id=channel_id)
        self.threads_created.append((channel_id, name, content))
        return tid

    async def set_forum_readonly(self, forum_id: str, customer_user_id: str, readonly: bool) -> bool:
        self.readonly[forum_id] = readonly
        return forum_id in self.channels

    async def restore_forum_access(self, forum_id: str, customer_user_id: str) -> bool:
        self.readonly[forum_id] = False
        return forum_id in self.channels

    async def delete_channel(self, channel_id: str) -> bool:
        self.deleted.append(channel_id)
        return self.channels.pop(channel_id, None) is not None

    async def grant_role(self, user_id: str, role_name: str) -> bool:
        self.roles.setdefault(user_id, set()).add(role_name)
        return True

    async def revoke_role(self, user_id: str, role_name: str) -> bool:
        self.roles.setdefault(user_id, set()).discard(role_name)
        return True

    # helpers for assertions
    def messages_in(self, channel_id: str) -> list[SentMessage]:
        return [m for m in self.sent if m.channel_id == channel_id]


# ═════════════════════════════════════════════════════════════════════════════
def fake_token_checker(valid: dict[str, TokenCheck] | None = None):
    table = valid or {}

    def check(token: str) -> TokenCheck:
        if token in table:
            return table[token]
        if token.startswith("bad"):
            return TokenCheck(False, detail="HTTP 401")
        return TokenCheck(True, user_id=str(abs(hash(token)) % 10**17 + 10**17), username=f"alt_{token[:4]}", display_name=f"Alt {token[:4]}")

    return check


def valid_token(seed: str = "A") -> str:
    """A string that satisfies ``looks_like_token`` (three dotted base64ish segments)."""
    return f"{seed * 24}.{'B' * 6}.{'C' * 38}"


# ═════════════════════════════════════════════════════════════════════════════
class FakeGuildAdmin:
    """In-memory guild used to exercise ``GuildProvisioner`` (implements GuildAdminPort)."""

    def __init__(self, *, existing_channels: dict[str, str] | None = None, existing_categories: dict[str, str] | None = None,
                 fail_on: set[str] | None = None):
        self.categories: dict[str, str] = dict(existing_categories or {})     # name → id
        self.channels: dict[str, str] = dict(existing_channels or {})         # name → id
        self.roles: dict[str, str] = {}
        self.overwrites: dict[str, list] = {}
        self.parents: dict[str, str] = {}
        self.topics: dict[str, str] = {}
        self.role_members: dict[str, list[str]] = {}
        self.fail_on = set(fail_on or ())
        self._seq = 900000000000000000

    def _id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _check(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"missing permissions for {name}")

    async def find_category(self, name):
        return self.categories.get(name)

    async def create_category(self, name, overwrites):
        self._check(name)
        cid = self._id()
        self.categories[name] = cid
        self.overwrites[cid] = list(overwrites)
        return cid

    async def find_text_channel(self, name):
        return self.channels.get(name)

    async def create_text_channel(self, name, *, category_id, topic, overwrites):
        self._check(name)
        cid = self._id()
        self.channels[name] = cid
        self.parents[cid] = category_id
        self.topics[cid] = topic
        self.overwrites[cid] = list(overwrites)
        return cid

    async def apply_overwrites(self, channel_id, overwrites):
        self.overwrites[channel_id] = list(overwrites)
        return True

    async def move_to_category(self, channel_id, category_id):
        self.parents[channel_id] = category_id
        return True

    async def ensure_role(self, name):
        return self.roles.setdefault(name, self._id())

    async def assign_role(self, role_id, user_id):
        self.role_members.setdefault(role_id, []).append(user_id)
        return True
