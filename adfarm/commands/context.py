"""CommandContext — what a handler receives. Built from a discord.py Interaction by the registry
and from plain values in tests, so handlers never import discord.py."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..core.errors import AdFarmError
from ..core.models import Actor
from ..discord.ports import ChannelRef
from ..discord.replies import Reply
from ..security.guards import ChannelInfo
from ..security.policy import ChannelKind
from ..services.container import Services

log = logging.getLogger(__name__)

Handler = Callable[["CommandContext"], Awaitable[Reply]]


@dataclass
class CommandContext:
    services: Services
    user_id: str
    username: str
    channel: ChannelRef
    channel_info: ChannelInfo
    kind: ChannelKind
    actor: Actor
    command: str                         # "run", "admin activate", ...
    options: dict[str, Any] = field(default_factory=dict)
    attachment_url: str = ""
    attachment_bytes: Optional[bytes] = None
    attachment_content_type: str = ""

    def opt(self, name: str, default: Any = None) -> Any:
        value = self.options.get(name, default)
        return default if value is None else value

    def text(self, name: str, default: str = "") -> str:
        value = self.options.get(name)
        return default if value is None else str(value).strip()

    def integer(self, name: str, default: Optional[int] = None) -> Optional[int]:
        value = self.options.get(name)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def flag(self, name: str, default: Optional[bool] = None) -> Optional[bool]:
        value = self.options.get(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def s(self) -> Services:
        return self.services

    @property
    def is_admin(self) -> bool:
        return self.actor.is_admin


async def run_handler(handler: Handler, ctx: CommandContext) -> Reply:
    """Uniform error → Reply mapping, so no handler leaks a traceback into Discord."""
    try:
        return await handler(ctx)
    except AdFarmError as exc:
        return Reply.error(exc.user_message)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("command %s failed", ctx.command)
        if ctx.services.alerts:
            try:
                await ctx.services.alerts.admin(f"crash:{ctx.command}", f"Command /{ctx.command} crashed: {type(exc).__name__}: {exc}")
            except Exception:
                pass
        return Reply.error("❌ Something went wrong. Admins were notified.")
