"""Public + account commands: /help, /getstarted, /account."""
from __future__ import annotations

from ..discord.embeds import account_embed, help_embed
from ..discord.ports import Embed
from ..discord.replies import Reply
from .context import CommandContext

GETSTARTED = (
    "**How AdFarm works**\n"
    "1. Pick a plan (1–4 alts, 30 days) and pay by crypto (BEP-20). Open a ticket in the ticket channel.\n"
    "2. An admin activates you → a private hub (forum) appears with #control, #dashboard, #farm-logs, #deals.\n"
    "3. In your hub run `/setup` to store an alt token + target channels, then `/run` to start posting.\n"
    "4. Tune anything live with `/tune`, `/channels`, `/deals`; watch `/status`.\n"
    "5. Renew with `/renew` before expiry — you get reminders 7, 3 and 1 day before."
)


async def help_(ctx: CommandContext) -> Reply:
    return Reply(embed=help_embed(ctx.actor.tier), ephemeral=True)


async def getstarted(ctx: CommandContext) -> Reply:
    embed = Embed(title="🚀 Get started", description=GETSTARTED, color=0x5865F2)
    if ctx.s.settings.payment_address:
        embed.add("Payment address (BEP-20)", f"`{ctx.s.settings.payment_address}`")
    ticket = ctx.s.tickets.ticket_channel_id if ctx.s.tickets else ""
    if ticket:
        embed.add("Tickets", f"<#{ticket}>")
    return Reply(embed=embed, ephemeral=True)


async def account(ctx: CommandContext) -> Reply:
    customer = ctx.actor.customer
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    alts = ctx.s.repos.alts.for_customer(customer.discord_id)
    acked = ctx.s.tickets.policy_acked(customer.discord_id) if ctx.s.tickets else False
    return Reply(embed=account_embed(customer, alts, ctx.s.now(), policy_acked=acked), ephemeral=True)
