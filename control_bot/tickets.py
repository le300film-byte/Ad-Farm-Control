"""control_bot.tickets — Real Discord ticket system with categories.

Ticket Types:
1. 💰 Payment — monthly/yearly, VIP yes/no, number of alts
2. 🐛 Bug Report — free text description
3. 💡 Suggestion — free text description

Flow:
- Customer clicks "🎫 Open Ticket" button in #open-ticket
- Selects category from dropdown
- Fills out the form (modal)
- Private thread created (only customer + admins can see)
- Admins notified in #admin-alerts
"""
from __future__ import annotations

import os
import discord
from discord.ui import Button, View, Select, Modal, TextInput
from discord import TextStyle, ButtonStyle
from typing import Optional


# ─── Ticket channel resolution (V8 bug-fix, plan #2) ────────────────────────


def resolve_ticket_channel_candidates(guild: Optional[discord.Guild] = None,
                                      panel_channel_id: str | int = "") -> list[int]:
    """Ordered #open-ticket channel-id candidates from every available source.

    Order:
      1. ``OPEN_TICKET_CH_ID`` / ``TICKET_CH_ID`` environment (workflow secret,
         or exported at runtime by ``/admin ticket-panel``).
      2. ``open_ticket_ch_id`` persisted in customers.db meta — written by
         ``/admin ticket-panel`` so the choice survives restarts/chunk
         handoffs even when the secret was never configured.
      3. The channel the ticket panel button lives in (captured on click).
      4. A guild channel literally named ``open-ticket`` / ``tickets``.
    """
    candidates: list[int] = []

    def _add(raw: str | int) -> None:
        text = str(raw or "").strip()
        if text.isdigit() and int(text) not in candidates:
            candidates.append(int(text))

    # 1. Environment
    for env_name in ("OPEN_TICKET_CH_ID", "TICKET_CH_ID"):
        _add(os.environ.get(env_name, ""))
    # 2. DB meta (persisted by /admin ticket-panel)
    try:
        import customer_manager as cm
        _add(cm.get_meta("open_ticket_ch_id", ""))
    except Exception as _ignored_exc:
        print(f"[TICKET] resolve_ticket_channel_candidates: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # 3. Channel the panel button was clicked in
    _add(panel_channel_id)
    # 4. Guild lookup by name
    if guild is not None:
        for wanted in ("open-ticket", "tickets", "ticket", "support-tickets"):
            ch = discord.utils.get(getattr(guild, "channels", []) or [], name=wanted)
            if ch is not None:
                _add(ch.id)
    return candidates


def resolve_ticket_channel_id(guild: Optional[discord.Guild] = None,
                              panel_channel_id: str | int = "") -> Optional[int]:
    """Best known ticket channel id (first candidate), or None."""
    candidates = resolve_ticket_channel_candidates(guild, panel_channel_id)
    return candidates[0] if candidates else None


async def _resolve_ticket_channel(guild: Optional[discord.Guild],
                                  panel_channel_id: str | int = ""):
    """Return the first LIVE ticket channel object, or None.

    A stale id (deleted channel) simply falls through to the next candidate so
    one outdated secret can never wedge the whole ticket flow again.
    """
    if guild is None:
        return None
    for ch_id in resolve_ticket_channel_candidates(guild, panel_channel_id):
        ch = guild.get_channel(ch_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(ch_id)
            except Exception:
                continue
        if ch is not None:
            return ch
    return None


# ─── Modals (Forms) ───────────────────────────────────────────────────────


class PaymentModal(Modal, title="💰 Payment Ticket"):
    """Form for payment tickets: plan, VIP, alts."""
    
    plan = TextInput(
        label="Subscription Plan",
        placeholder="Type: monthly or yearly",
        required=True,
        max_length=20,
    )
    
    vip = TextInput(
        label="VIP?",
        placeholder="Type: yes or no",
        required=True,
        max_length=5,
    )
    
    alts = TextInput(
        label="Number of Alts",
        placeholder="e.g. 2",
        required=True,
        max_length=3,
    )
    
    extra = TextInput(
        label="Extra Notes (optional)",
        placeholder="Any special requests?",
        required=False,
        max_length=500,
        style=TextStyle.paragraph,
    )

    def __init__(self, customer_id: str, panel_channel_id: str | int = ""):
        super().__init__()
        self.customer_id = customer_id
        self.panel_channel_id = panel_channel_id

    async def on_submit(self, inter: discord.Interaction) -> None:
        await create_ticket_thread(
            inter,
            category="💰 Payment",
            customer_id=self.customer_id,
            fields={
                "Plan": self.plan.value,
                "VIP": self.vip.value,
                "Alts": self.alts.value,
                "Notes": self.extra.value or "—",
            },
            panel_channel_id=self.panel_channel_id,
        )


class BugReportModal(Modal, title="🐛 Bug Report"):
    """Form for bug reports: free text."""
    
    description = TextInput(
        label="Describe the bug",
        placeholder="What happened? What did you expect?",
        required=True,
        max_length=2000,
        style=TextStyle.paragraph,
    )
    
    steps = TextInput(
        label="Steps to reproduce (optional)",
        placeholder="1. I ran /command\n2. Then I saw...",
        required=False,
        max_length=1000,
        style=TextStyle.paragraph,
    )

    def __init__(self, customer_id: str, panel_channel_id: str | int = ""):
        super().__init__()
        self.customer_id = customer_id
        self.panel_channel_id = panel_channel_id

    async def on_submit(self, inter: discord.Interaction) -> None:
        await create_ticket_thread(
            inter,
            category="🐛 Bug Report",
            customer_id=self.customer_id,
            fields={
                "Description": self.description.value,
                "Steps to Reproduce": self.steps.value or "—",
            },
            panel_channel_id=self.panel_channel_id,
        )


class SuggestionModal(Modal, title="💡 Suggestion"):
    """Form for suggestions: free text."""
    
    suggestion = TextInput(
        label="Your Suggestion",
        placeholder="What would you like to see?",
        required=True,
        max_length=2000,
        style=TextStyle.paragraph,
    )

    def __init__(self, customer_id: str, panel_channel_id: str | int = ""):
        super().__init__()
        self.customer_id = customer_id
        self.panel_channel_id = panel_channel_id

    async def on_submit(self, inter: discord.Interaction) -> None:
        await create_ticket_thread(
            inter,
            category="💡 Suggestion",
            customer_id=self.customer_id,
            fields={
                "Suggestion": self.suggestion.value,
            },
            panel_channel_id=self.panel_channel_id,
        )


# ─── Category Selector ────────────────────────────────────────────────────


class TicketCategoryView(View):
    """Dropdown to pick ticket category."""

    def __init__(self, customer_id: str, panel_channel_id: str | int = ""):
        super().__init__(timeout=300)
        self.customer_id = customer_id
        # Channel the panel button lives in — the natural home for ticket
        # threads when OPEN_TICKET_CH_ID was never configured (plan #2).
        self.panel_channel_id = panel_channel_id

    @discord.ui.select(
        placeholder="🎫 Choose a ticket category...",
        options=[
            discord.SelectOption(
                label="Payment",
                description="Subscription, pricing, activation",
                emoji="💰",
                value="payment",
            ),
            discord.SelectOption(
                label="Bug Report",
                description="Something is broken or not working",
                emoji="🐛",
                value="bug",
            ),
            discord.SelectOption(
                label="Suggestion",
                description="Feature request or improvement idea",
                emoji="💡",
                value="suggestion",
            ),
        ],
    )
    async def select_category(self, inter: discord.Interaction, select: Select) -> None:
        category = select.values[0]
        panel_ch = self.panel_channel_id or getattr(inter, "channel_id", "") or ""

        if category == "payment":
            await inter.response.send_modal(PaymentModal(self.customer_id, panel_ch))
        elif category == "bug":
            await inter.response.send_modal(BugReportModal(self.customer_id, panel_ch))
        elif category == "suggestion":
            await inter.response.send_modal(SuggestionModal(self.customer_id, panel_ch))


# ─── Ticket Panel (the button customers see) ─────────────────────────────


class TicketPanelView(View):
    """Persistent button that opens the ticket category selector."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open a Ticket",
        emoji="🎫",
        style=ButtonStyle.primary,
        custom_id="ticket:open",
    )
    async def open_ticket(self, inter: discord.Interaction, _btn: Button) -> None:
        uid = str(inter.user.id)
        # Capture the channel hosting the panel (normally #open-ticket) so the
        # thread can be created there even without OPEN_TICKET_CH_ID (plan #2).
        panel_ch = getattr(inter, "channel_id", "") or ""
        await inter.response.send_message(
            "Choose what your ticket is about:",
            view=TicketCategoryView(uid, panel_ch),
            ephemeral=True,
        )


# ─── Thread Creation ──────────────────────────────────────────────────────


async def create_ticket_thread(
    inter: discord.Interaction,
    category: str,
    customer_id: str,
    fields: dict[str, str],
    panel_channel_id: str | int = "",
) -> None:
    """Create a private thread for the ticket and notify admins."""
    guild = inter.guild
    if not guild:
        await inter.response.send_message("❌ Tickets can only be opened in a server.", ephemeral=True)
        return

    # V8 bug-fix (plan #2): resolve the ticket channel from EVERY source —
    # env secret, DB meta persisted by /admin ticket-panel, the channel the
    # panel button lives in, or a name lookup — instead of only the env var.
    ticket_ch = await _resolve_ticket_channel(
        guild, panel_channel_id or getattr(inter, "channel_id", "") or ""
    )
    if not ticket_ch:
        await inter.response.send_message(
            "❌ Ticket channel not configured.\n"
            "**Admin fix:** run `/admin ticket-panel channel:#open-ticket` "
            "(this now also saves the channel), or set the `OPEN_TICKET_CH_ID` "
            "secret, or re-run `setup.py` to create `#open-ticket`.",
            ephemeral=True,
        )
        return

    # Create a private thread
    thread_name = f"{category} — {inter.user.display_name}"
    try:
        thread = await ticket_ch.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.private_thread,
            auto_archive_duration=4320,  # 7 days
        )
    except Exception as exc:
        await inter.response.send_message(f"❌ Could not create ticket thread: {exc}", ephemeral=True)
        return

    # Add the customer to the thread
    try:
        await thread.add_user(inter.user)
    except Exception as _ignored_exc:
        print(f"[TICKET] create_ticket_thread: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    # Add all members with Admin role
    admin_role = discord.utils.get(guild.roles, name="Admin")
    if admin_role:
        for member in admin_role.members:
            try:
                await thread.add_user(member)
            except Exception as _ignored_exc:
                print(f"[TICKET] create_ticket_thread: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    # Build the ticket embed
    embed = discord.Embed(
        title=f"{category} — New Ticket",
        color=0x5865F2,
    )
    embed.add_field(name="Customer", value=f"{inter.user.mention} (`{customer_id}`)", inline=False)
    for label, value in fields.items():
        embed.add_field(name=label, value=value, inline=False)
    embed.set_footer(text="An admin will respond shortly.")

    # Post in the thread
    await thread.send(embed=embed)
    await thread.send(
        f"{inter.user.mention} — your ticket has been opened. "
        f"An admin will respond here shortly. You can add more details below."
    )

    # Notify admins in #admin-alerts
    alert_ch_id = os.environ.get("ADMIN_ALERTS_CH_ID", "")
    if alert_ch_id:
        try:
            alert_ch = guild.get_channel(int(alert_ch_id)) or await guild.fetch_channel(int(alert_ch_id))
            alert_embed = discord.Embed(
                title="🎫 New Ticket",
                description=f"**{category}** from {inter.user.mention} (`{customer_id}`)\n[View ticket]({thread.jump_url})",
                color=0xED4245,
            )
            if admin_role:
                await alert_ch.send(admin_role.mention, embed=alert_embed)
            else:
                await alert_ch.send(embed=alert_embed)
        except Exception as _ignored_exc:
            print(f"[TICKET] create_ticket_thread: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    # Confirm to customer
    await inter.response.send_message(
        f"✅ **Ticket opened!** Your private thread has been created: {thread.mention}\n"
        f"An admin will respond there shortly.",
        ephemeral=True,
    )


# ─── Setup Function ───────────────────────────────────────────────────────


def setup(bot: discord.Client) -> None:
    """Register the ticket panel view so it persists across restarts."""
    bot.add_view(TicketPanelView())