"""control_bot.payments — TX-hash detection, auto-ack, ticket ledger (1.1).

Semi-automated payment-ack pipeline:
  * Any message containing a 0x-prefixed 64-char BEP-20 TX hash in the ticket
    channel gets an immediate auto-acknowledgement (kills the silence gap at
    the highest-anxiety point of the funnel).
  * The hash + customer + timestamp is recorded in the events table.
  * A one-click activation template is generated for admins (pre-filled from
    the ticket) via ``/admin activate-template``.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

import customer_manager as cm

TX_HASH_RE = re.compile(r"\b0x[a-fA-F0-9]{64}\b")

ACK_TEMPLATE = (
    "✅ **We've received your transaction hash.**\n"
    "Our team will verify it on BSCScan and activate your farm. "
    "**We aim to activate within a few hours** (no SLA, but usually much faster)."
)


def extract_tx_hashes(text: str) -> list[str]:
    return list(dict.fromkeys(TX_HASH_RE.findall(text or "")))


def record_ticket_message(discord_id: str, content: str, thread_id: str = "") -> dict[str, Any]:
    """Return the first TX hash found, or None (recorded as open ticket)."""
    hashes = extract_tx_hashes(content)
    result: dict[str, Any] = {
        "hashes": hashes,
        "tx_hash": hashes[0] if hashes else "",
        "thread_id": thread_id,
    }
    if hashes:
        cm.record_event(discord_id, "payment_tx_posted", {
            "tx_hash": hashes[0], "count": len(hashes), "thread_id": thread_id,
        })
    else:
        cm.record_event(discord_id, "ticket_open", {"thread_id": thread_id})
    return result


def activation_template(customer: dict[str, Any], tx_hash: str = "") -> str:
    """One-click `/admin activate` command pre-filled from the ticket."""
    days = max(1, int(float(customer.get("expiry_date", 0) or 0) - time.time()) // 86400) or 30
    alts = int(customer.get("alt_count", 1) or 1)
    vip = bool(customer.get("vip"))
    gh_account = customer.get("github_account", "")
    tx_note = f"\nTX: `{tx_hash}` (verify on BSCScan before running)" if tx_hash else ""
    return (
        f"**One-click activation template**\n"
        f"`/admin activate user:@` days:{days} alts:{alts} vip:{'true' if vip else 'false'}"
        + (f" github_account:{gh_account}" if gh_account else "")
        + f"`\n{tx_note}\n"
        "Mark the ticket as paid, then paste the command in #admin-commands."
    )


async def maybe_auto_ack(channel: Any, discord_id: str, content: str) -> Optional[dict[str, Any]]:
    """Post the auto-ack reply when the *first* TX hash appears in a ticket.

    Only acknowledges a hash once per customer (deduped via the events table)
    so follow-up replies cannot spam the same acknowledgement.
    """
    result = record_ticket_message(discord_id, content)
    if not result["hashes"]:
        return None
    posted = cm.get_events("payment_tx_posted", discord_id=discord_id, limit=5)
    if len(posted) > 1:
        return result  # already acked this customer
    try:
        await channel.send(ACK_TEMPLATE)
    except Exception as exc:
        print(f"[PAYMENTS] auto-ack send failed: {exc}")
    return result
