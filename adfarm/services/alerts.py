"""AlertService — admin alerts + audit trail, with debounce and redaction."""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..core.clock import Clock
from ..db.repositories import EventRepo
from ..discord.ports import DiscordPort, Embed
from ..security.redact import redact

log = logging.getLogger(__name__)


class AlertService:
    def __init__(self, discord: DiscordPort, events: EventRepo, *, clock: Clock, alerts_channel_id: str = "", audit_channel_id: str = "", debounce: int = 900):
        self.discord = discord
        self.events = events
        self.clock = clock
        self.alerts_channel_id = alerts_channel_id
        self.audit_channel_id = audit_channel_id
        self.debounce = int(debounce)
        self._last: dict[str, float] = {}

    async def admin(self, key: str, text: str, *, embed: Optional[Embed] = None, force: bool = False) -> bool:
        """Post to #admin-alerts; identical ``key`` is suppressed for ``debounce`` seconds."""
        now = self.clock.now()
        if not force and now - self._last.get(key, 0.0) < self.debounce:
            return False
        self._last[key] = now
        safe = redact(text)
        log.warning("ALERT[%s] %s", key, safe)
        if not self.alerts_channel_id:
            return False
        try:
            await self.discord.send(self.alerts_channel_id, safe[:1900], embed=embed)
            return True
        except Exception as exc:  # pragma: no cover - network
            log.error("alert delivery failed: %s", exc)
            return False

    async def audit(self, actor_id: str, action: str, *, customer_id: str = "", **details: Any) -> None:
        """Append to the events ledger and mirror to #audit-logs."""
        self.events.log(customer_id or actor_id, f"audit:{action}", now=self.clock.now(), actor=str(actor_id), **_jsonable(details))
        if not self.audit_channel_id:
            return
        parts = [f"**{action}** by <@{actor_id}>"]
        if customer_id:
            parts.append(f"customer <@{customer_id}>")
        for k, v in details.items():
            if v not in (None, "", [], {}):
                parts.append(f"{k}=`{str(v)[:80]}`")
        try:
            await self.discord.send(self.audit_channel_id, redact(" · ".join(parts))[:1900])
        except Exception as exc:  # pragma: no cover
            log.error("audit delivery failed: %s", exc)

    def event(self, customer_id: str, name: str, **payload: Any) -> None:
        self.events.log(customer_id, name, now=self.clock.now(), **_jsonable(payload))


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple, set, frozenset)):
            out[k] = [str(x) for x in v]
        elif isinstance(v, dict):
            out[k] = {str(a): str(b) for a, b in v.items()}
        else:
            out[k] = str(v)
    return out
