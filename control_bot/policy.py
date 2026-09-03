"""control_bot.policy — click-through ToS, pre-payment policy card, privacy.

TODO 0.4:
  * Pinned embed in #open-ticket with the full policy card.
  * Acknowledgement button recorded in customers.db BEFORE any wallet address
    is shared with the customer.
  * "First ban = time credit" (48h full, then pro-rated).
  * Minimal privacy notice (what we store, why, deletion on request).
  * Hard line: main accounts are never supported.
"""
from __future__ import annotations

import os
from typing import Any

import discord
from discord.ui import Button, View

import customer_manager as cm

# Bump this when the policy text changes; old acks become invalid.
POLICY_VERSION = "v8-2026-09-03-1"
WALLET_POLICY_REQUIRED = True

SETUP_VIDEO_URL = os.environ.get(
    "SETUP_VIDEO_URL",
    "https://github.com/DarkKitty-w/adfarm-core-AI/blob/main/docs/TOKEN_EXTRACTION_GUIDE.md",
)

POLICY_CARD = (
    "**📜 AdFarm V8 — Pre-Payment Policy Card**\n"
    "Please read before sending any payment.\n\n"
    "1. **No refunds** once the farm is provisioned and a run starts.\n"
    "2. **Time credit on bans:** if an alt is banned within **48 hours** of "
    "its first run you get **full time credit**; after 48h it is **pro-rated** "
    "for the unused time.\n"
    "3. **Alt survival is not guaranteed.** We use every known anti-detection "
    "measure, but platform bans are outside our control.\n"
    "4. **Main accounts are never supported.** We only run fresh alt accounts. "
    "Never give us a token for an account you cannot afford to lose.\n"
    "5. **Crypto payments are final.** BEP-20 (USDT/BUSD) via Trust Wallet "
    "only; confirm the TX hash with an admin before activation.\n"
    "6. **Data we store:** your Discord ID, username, alt repo names, setup "
    "dates, subscription status and (privately) alt tokens needed to run your "
    "farm. We never store anything else and we never sell data.\n"
    "7. **No SLA.** We aim to respond quickly, but support is best-effort; "
    "activations usually complete within a few hours.\n\n"
    "By clicking **I Agree** you confirm you have read and accept these terms."
)

PRIVACY_NOTICE = (
    "**🔒 Minimal Privacy Notice**\n\n"
    "**What we store:** your Discord ID + username, your alt repository names, "
    "subscription dates/status, and the alt tokens you submit through `/setup` "
    "(kept in the private control database; never logged, never shown in a channel).\n\n"
    "**Why:** the store exists so your farm keeps running across server restarts "
    "and so admin can verify your subscription.\n\n"
    "**Deletion:** contact an admin at any time and every record tied to your "
    "Discord ID is deleted on request (usually within 24h).\n\n"
    "**Main accounts:** we will never accept or store a token for a main "
    "account — alts only."
)

TOS_TEXT = (
    "**AdFarm V8 Terms of Service (summary)**\n\n"
    "- Service is a best-effort automation of alt advertising accounts.\n"
    "- **Main accounts are not supported.** Never automate an account you "
    "cannot afford to lose.\n"
    "- Cryptocurrency payments (BEP-20 USDT/BUSD) are **final**.\n"
    "- Bans within 48h = **full time credit**; after that, **pro-rated**.\n"
    "- We store the minimal data listed in the privacy notice; deletion on "
    "request.\n"
    "- No SLA; support is best-effort.\n"
    "- We reserve the right to refuse or terminate service for abusive use."
)

# A clean link/button label for the token-extraction walkthrough.
VIDEO_BUTTON_LABEL = "🎥 Need help? Watch the 3-min video"


def policy_card_embed() -> discord.Embed:
    return discord.Embed(
        title="📜 Pre-Payment Policy Card + Privacy Notice",
        description=f"{POLICY_CARD}\n\n{PRIVACY_NOTICE}",
        color=0xED4245,
    )


class PolicyAckView(View):
    """One-click acknowledgement; recorded before wallet sharing (0.4)."""

    def __init__(self, discord_user_id: int | None = None):
        super().__init__(timeout=600)
        self._uid = str(discord_user_id or 0)

    @discord.ui.button(label="✅ I Agree — I've read the policy", style=discord.ButtonStyle.success)
    async def _ack(self, inter: discord.Interaction, _btn: Button) -> None:
        uid = str(inter.user.id) if self._uid == "0" or not self._uid else self._uid
        if str(inter.user.id) != uid and uid != "0":
            await inter.response.send_message(
                "❌ This acknowledgement belongs to someone else.", ephemeral=True
            )
            return
        cm.ack_policy(uid, POLICY_VERSION)
        await inter.response.send_message(
            "✅ **Policy accepted.** Your acknowledgement is recorded. An admin "
            "can now share the payment address with you.", ephemeral=True
        )
        self.stop()


async def require_policy_ack(inter: discord.Interaction, channel: Any = None) -> bool:
    """Return True when the user has acknowledged the current policy.

    Used to gate wallet/TX sharing (0.4) and the ticket flow (Phase 1.1).
    """
    uid = str(inter.user.id)
    if not WALLET_POLICY_REQUIRED or cm.has_policy_ack(uid, POLICY_VERSION):
        return True
    await inter.response.send_message(
        "🚫 You must accept the **Pre-Payment Policy Card** before any payment "
        "address is shared. Click the button below to acknowledge it.",
        embed=policy_card_embed(), view=PolicyAckView(inter.user.id), ephemeral=True,
    )
    return False


async def pin_policy_in_channel(channel: Any, extra: str = "") -> bool:
    """Pin the policy embed in #open-ticket (called by /admin pin-policy)."""
    try:
        msg = await channel.send(
            f"{extra}\n{POLICY_CARD}\n\n{PRIVACY_NOTICE}",
            embed=policy_card_embed(),
            view=PolicyAckView(),
        )
        try:
            await msg.pin()
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[POLICY] pin failed: {exc}")
        return False
