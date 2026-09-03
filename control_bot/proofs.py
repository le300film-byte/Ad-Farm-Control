"""control_bot.proofs — anonymized public proofs channel (TODO 3.3).

Opt-in: customers share first-post screenshots / supplier-alert wins through
``/proofs``; the bot re-posts to the public #proofs channel with the customer
ID redacted and only the result + the customer's chosen caption.
"""
from __future__ import annotations

import os
from typing import Any

import discord

import customer_manager as cm

# Wired from PROOFS_CH_ID env at startup (see wire_proofs_channel()).
PROOFS_CH_ID: int | None = None


def wire_proofs_channel() -> int | None:
    """Read PROOFS_CH_ID from env once at bot startup."""
    global PROOFS_CH_ID
    raw = os.environ.get("PROOFS_CH_ID", "").strip()
    try:
        PROOFS_CH_ID = int(raw) or None if raw else None
    except ValueError:
        print(f"[PROOFS] Invalid PROOFS_CH_ID: {raw!r}")
        PROOFS_CH_ID = None
    return PROOFS_CH_ID


def redact(text: str, customer_id: str) -> str:
    """Replace the customer's snowflake with a pseudonymous tag."""
    if not text or not customer_id:
        return text or ""
    tag = f"`{customer_id[:4]}…`"
    return text.replace(customer_id, tag)


class ProofModal(discord.ui.Modal, title="Post your proof (redacted)"):
    """Captures the proof link/caption; publishes straight to #proofs."""

    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.body = discord.ui.TextInput(
            label="Screenshot link or caption",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. first post sold at $2.50/1K — https://i.imgur.com/…",
            min_length=5,
            max_length=400,
        )
        self.add_item(self.body)

    async def on_submit(self, inter: discord.Interaction) -> None:
        uid = str(inter.user.id)
        if not cm.is_active(uid):
            await inter.response.send_message("❌ Active subscription required.", ephemeral=True)
            return
        ok = await publish_proof(inter.client, uid, self.body.value or "")
        if ok:
            await inter.response.send_message(
                "🏆 **Proof posted to #proofs** (your ID is redacted). Thanks!", ephemeral=True
            )
        else:
            await inter.response.send_message(
                "⚠️ Could not post — #proofs channel is not configured yet. "
                "An admin will publish it manually.",
                ephemeral=True,
            )


class ProofsView(discord.ui.View):
    @discord.ui.button(label="📸 Post my proof (redacted)", style=discord.ButtonStyle.secondary)
    async def _proof(self, inter: discord.Interaction, _btn: discord.ui.Button) -> None:
        uid = str(inter.user.id)
        if not cm.is_active(uid):
            await inter.response.send_message("❌ Active subscription required.", ephemeral=True)
            return
        cm.record_event(uid, "proof_opted_in", {})
        await inter.response.send_modal(ProofModal())


async def publish_proof(bot: Any, customer_id: str, body: str) -> bool:
    """Post one redacted proof to the public #proofs channel."""
    ch_id = PROOFS_CH_ID
    if not ch_id:
        return False
    try:
        ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        await ch.send(
            f"🏆 **Proof** — {redact(body, customer_id)}"
        )
        cm.record_event(customer_id, "proof_published", {})
        return True
    except Exception as exc:
        print(f"[PROOFS] publish failed: {exc}")
        return False
