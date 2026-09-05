"""WebhookIngestor — routes sender webhook messages to the right alt (pure routing logic).

Legacy routing (L-7) matched messages by fuzzy ALT_NAME across the operator's global dashboard
channels, so two customers with alts called "main" collided and customer forums were ignored.
Here every customer forum thread id is a key: ``thread_id → customer``, and inside a customer
the alt is picked by the heartbeat's ``alt_id`` footer or the webhook username (``ALT_NAME``),
falling back to the customer's only alt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..core.models import Alt, Customer
from .fleet_state import AltKey, FleetState
from .heartbeat import EmbedLike, Heartbeat, parse_heartbeat

BAN_MARKERS = re.compile(
    r"(banned|token invalidated|token is invalid|\bDEAD\b|account deleted|shadowban|account flagged|"
    r"\bflagged\b|kicked|HTTP 403|HTTP 401|invalidated/revoked|PANIC)",
    re.I,
)
_LOG_KIND = re.compile(r"\[([A-Z][A-Z0-9_-]{1,23})\]")


@dataclass(frozen=True)
class IncomingMessage:
    channel_id: str            # thread the webhook posted into
    author_name: str           # webhook username (sender sets it to ALT_NAME)
    content: str
    embeds: Sequence[EmbedLike] = ()
    is_webhook: bool = True
    message_id: str = ""


@dataclass(frozen=True)
class IngestResult:
    kind: str                  # heartbeat | log | deal | dm | ignored
    key: Optional[AltKey] = None
    heartbeat: Optional[Heartbeat] = None
    ban_detected: bool = False
    dm_author_id: str = ""
    dm_text: str = ""


class WebhookIngestor:
    def __init__(self, fleet: FleetState, customer_by_thread: Callable[[str], Optional[tuple[Customer, str]]],
                 alts_for_customer: Callable[[str], list[Alt]]):
        """
        customer_by_thread(thread_id) → (customer, thread_role) where role ∈ dashboard/farm-logs/deals/dm-inbox/control
        alts_for_customer(customer_id) → list[Alt]
        """
        self.fleet = fleet
        self.customer_by_thread = customer_by_thread
        self.alts_for_customer = alts_for_customer

    # ── routing ─────────────────────────────────────────────────────────────
    def resolve_alt(self, customer: Customer, alts: list[Alt], *, sender_alt_id: int | None, author_name: str, text: str = "") -> Optional[Alt]:
        if sender_alt_id:
            for alt in alts:
                if alt.sender_alt_id == sender_alt_id:
                    return alt
        name = (author_name or "").strip().casefold()
        if name:
            for alt in alts:
                if name in {alt.username.casefold(), alt.display_name.casefold(), alt.label.casefold()}:
                    return alt
        m = re.search(r"alt[_\s-]?id\s*=\s*(\d+)", text or "", re.I)
        if m:
            for alt in alts:
                if alt.sender_alt_id == int(m.group(1)):
                    return alt
        return alts[0] if len(alts) == 1 else None

    def ingest(self, msg: IncomingMessage) -> IngestResult:
        if not msg.is_webhook:
            return IngestResult("ignored")
        located = self.customer_by_thread(msg.channel_id)
        if located is None:
            return IngestResult("ignored")
        customer, role = located
        alts = [a for a in self.alts_for_customer(customer.discord_id)]
        if not alts:
            return IngestResult("ignored")

        hb = parse_heartbeat(msg.content, msg.embeds)
        alt = self.resolve_alt(customer, alts, sender_alt_id=hb.sender_alt_id if hb else None, author_name=msg.author_name,
                               text=" ".join([msg.content] + [e.footer for e in msg.embeds]))
        if alt is None:
            return IngestResult("ignored")
        key: AltKey = (customer.discord_id, alt.alt_index)
        self.fleet.register(key, alt.sender_alt_id)

        if hb is not None:
            self.fleet.apply_heartbeat(hb, key=key)
            ban = bool(BAN_MARKERS.search(hb.last_error or "")) and hb.status in ("error", "stopped")
            return IngestResult("heartbeat", key=key, heartbeat=hb, ban_detected=ban)

        body = msg.content.replace("`", "").strip() or " ".join(f"{e.title} {e.description}" for e in msg.embeds).strip()
        if role == "deals" or any((e.title or "").lower().startswith(("📈", "deal")) for e in msg.embeds):
            self.fleet.append_log(key, body[:300], kind="DEAL")
            return IngestResult("deal", key=key)
        if role == "dm-inbox":
            author_id, text = _extract_dm(body, msg.embeds)
            self.fleet.append_log(key, f"DM from {author_id or '?'}: {text[:200]}", kind="DM")
            return IngestResult("dm", key=key, dm_author_id=author_id, dm_text=text)

        kind = "INFO"
        m = _LOG_KIND.search(body)
        if m:
            kind = m.group(1)
        elif "ERROR" in body.upper() or "FAIL" in body.upper():
            kind = "ERROR"
        elif "CAUTION" in body.upper():
            kind = "CAUTION"
        self.fleet.append_log(key, body[:300], kind=kind)
        live = self.fleet.get(key)
        if live is not None and not live.online and "STARTUP" in body.upper():
            live.online = True
            live.status = "active"
            live.last_heartbeat_at = self.fleet.clock.now()
        ban = bool(BAN_MARKERS.search(body)) and kind in ("ERROR", "BAN", "PANIC", "STOP", "CRITICAL", "INFO")
        return IngestResult("log", key=key, ban_detected=ban)


def _extract_dm(body: str, embeds: Sequence[EmbedLike]) -> tuple[str, str]:
    """Sender DM relays look like ``📩 DM from **name** (123456789012345678): text``."""
    text = body
    for e in embeds:
        text = " ".join(x for x in (text, e.title, e.description) if x)
        for name, value in e.fields:
            text += f" {name}: {value}"
    m = re.search(r"\((\d{15,21})\)", text)
    author = m.group(1) if m else ""
    parts = text.split(":", 1)
    content = parts[1].strip() if len(parts) == 2 else text.strip()
    return author, content[:1500]
