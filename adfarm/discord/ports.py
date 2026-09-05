"""Ports — the tiny surface of Discord the services depend on.

``DiscordPort`` is implemented by ``adapter.DiscordPyAdapter`` (real) and ``tests/fakes.FakeDiscord``.
All ids are strings; all methods are async; nothing here imports discord.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence


@dataclass(frozen=True)
class ChannelRef:
    id: str
    name: str = ""
    kind: str = ""              # forum | thread | text | category | dm
    parent_id: str = ""         # forum id for a thread, category id for a channel
    category_id: str = ""
    category_name: str = ""
    guild_id: str = ""


@dataclass(frozen=True)
class MessageRef:
    id: str
    channel_id: str
    content: str = ""
    author_id: str = ""
    author_name: str = ""
    is_webhook: bool = False
    embeds: Sequence[Any] = ()


@dataclass
class Embed:
    """Framework-neutral embed (rendered by the adapter)."""

    title: str = ""
    description: str = ""
    color: int = 0x2F3136
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    footer: str = ""
    timestamp: bool = False

    def add(self, name: str, value: str, inline: bool = False) -> "Embed":
        self.fields.append((str(name)[:256], str(value)[:1024] or "—", inline))
        return self

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title[:256], "description": self.description[:4096], "color": self.color,
                               "fields": [{"name": n, "value": v, "inline": i} for n, v, i in self.fields[:25]]}
        if self.footer:
            out["footer"] = {"text": self.footer[:2048]}
        return out


@dataclass(frozen=True)
class ForumSpec:
    name: str
    category_id: str
    customer_user_id: str
    admin_role_id: str = ""
    admin_user_ids: tuple[str, ...] = ()    # operators granted explicit view/send in the hub
    threads: tuple[tuple[str, str, str], ...] = ()    # (role, thread name, opening message)


@dataclass(frozen=True)
class ForumResult:
    forum_id: str
    thread_ids: dict[str, str]        # role → thread id
    webhooks: dict[str, str]          # role → webhook URL (created for dashboard/farm-logs/deals/dm-inbox)
    created: bool


class DiscordPort(Protocol):
    # ── lookup ──────────────────────────────────────────────────────────────
    async def get_channel(self, channel_id: str) -> Optional[ChannelRef]: ...
    async def find_channel_by_name(self, name: str) -> Optional[ChannelRef]: ...
    async def member_exists(self, user_id: str) -> bool: ...
    async def display_name(self, user_id: str) -> str: ...

    # ── messaging ───────────────────────────────────────────────────────────
    async def send(self, channel_id: str, content: str = "", *, embed: Embed | None = None, view: Any | None = None) -> Optional[str]: ...
    async def edit_message(self, channel_id: str, message_id: str, content: str = "", *, embed: Embed | None = None) -> bool: ...
    async def dm(self, user_id: str, content: str, *, embed: Embed | None = None) -> bool: ...
    async def recent_messages(self, channel_id: str, limit: int = 50) -> list[MessageRef]: ...
    async def pin(self, channel_id: str, message_id: str) -> bool: ...

    # ── structure ───────────────────────────────────────────────────────────
    async def create_customer_forum(self, spec: ForumSpec) -> ForumResult: ...
    async def ensure_forum_webhooks(self, forum_id: str, thread_ids: dict[str, str]) -> dict[str, str]: ...
    async def create_thread(self, channel_id: str, name: str, content: str = "") -> str: ...
    async def set_forum_readonly(self, forum_id: str, customer_user_id: str, readonly: bool) -> bool: ...
    async def restore_forum_access(self, forum_id: str, customer_user_id: str) -> bool: ...
    async def delete_channel(self, channel_id: str) -> bool: ...
    async def grant_role(self, user_id: str, role_name: str) -> bool: ...
    async def revoke_role(self, user_id: str, role_name: str) -> bool: ...
