"""TicketService — manual-payment workflows: renew, pause billing, proofs, policy acknowledgement."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..core.errors import ConflictError, ValidationError
from ..core.models import Customer
from ..core.rules import validate_days
from ..discord.policy import POLICY_ACCEPT_LABEL, POLICY_TEXT, POLICY_TITLE  # noqa: F401  (re-exported)
from ..discord.ports import Embed
from .container import Services

TX_HASH = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

MIN_SUPPORT_TOPIC_CHARS = 5


@dataclass(frozen=True)
class Ticket:
    id: int
    kind: str
    customer_id: str
    channel_id: str


class TicketService:
    def __init__(self, s: Services):
        self.s = s

    @property
    def ticket_channel_id(self) -> str:
        return self.s.repos.meta.get("ticket_channel_id") or self.s.settings.ticket_channel_id

    def set_ticket_channel(self, channel_id: str) -> None:
        self.s.repos.meta.set("ticket_channel_id", channel_id)

    # ── renew / pause billing ───────────────────────────────────────────────
    async def open_renewal(self, customer: Customer, *, days: int = 30, note: str = "") -> Ticket:
        days = validate_days(days)
        existing = self.s.repos.tickets.find_open(customer.discord_id, "renew")
        if existing:
            raise ConflictError(f"⚠️ You already have an open renewal ticket (#{existing['id']}). An admin will confirm your payment shortly.")
        tid = self.s.repos.tickets.open(customer.discord_id, "renew", now=self.s.now(), days=days, note=note[:300])
        channel = self.ticket_channel_id
        embed = Embed(title=f"🧾 Renewal ticket #{tid}", color=0x5865F2)
        embed.add("Customer", f"<@{customer.discord_id}> ({customer.username})", True)
        embed.add("Requested", f"{days} day(s)", True)
        embed.add("Current expiry", f"<t:{int(customer.expiry_date)}:D>", True)
        if self.s.settings.payment_address:
            embed.add("Pay to (BEP-20)", f"`{self.s.settings.payment_address}`")
        embed.add("Next", "Customer posts the tx hash with `/proofs`; admin confirms with `/admin extend`.")
        if note:
            embed.add("Note", note[:300])
        if channel:
            await self.s.discord.send(channel, f"<@{customer.discord_id}>", embed=embed)
        if self.s.alerts:
            await self.s.alerts.admin(f"ticket:renew:{customer.discord_id}", f"Renewal ticket #{tid} opened by {customer.username} ({days} d).", force=True)
            self.s.alerts.event(customer.discord_id, "ticket_opened", kind="renew", ticket=tid, days=days)
        return Ticket(tid, "renew", customer.discord_id, channel)

    async def open_pause_billing(self, customer: Customer, *, days: int, reason: str = "") -> Ticket:
        days = validate_days(days)
        existing = self.s.repos.tickets.find_open(customer.discord_id, "pause-billing")
        if existing:
            raise ConflictError(f"⚠️ A billing-pause request (#{existing['id']}) is already pending.")
        tid = self.s.repos.tickets.open(customer.discord_id, "pause-billing", now=self.s.now(), days=days, reason=reason[:300])
        channel = self.ticket_channel_id
        if channel:
            await self.s.discord.send(channel, f"⏸️ Billing-pause request #{tid} from <@{customer.discord_id}>: {days} day(s). {reason[:200]}")
        if self.s.alerts:
            await self.s.alerts.admin(f"ticket:pause:{customer.discord_id}", f"Billing pause #{tid} requested by {customer.username}.", force=True)
            self.s.alerts.event(customer.discord_id, "ticket_opened", kind="pause-billing", ticket=tid, days=days)
        return Ticket(tid, "pause-billing", customer.discord_id, channel)

    async def submit_proof(self, customer: Customer, *, tx_hash: str, note: str = "", attachment_url: str = "") -> int:
        tx = tx_hash.strip()
        if not TX_HASH.match(tx):
            raise ValidationError("❌ A BEP-20 transaction hash is 64 hex characters (optionally prefixed with 0x).")
        ticket = self.s.repos.tickets.find_open(customer.discord_id, "renew")
        tid = ticket["id"] if ticket else self.s.repos.tickets.open(customer.discord_id, "renew", now=self.s.now(), days=30, note="proof without ticket")
        self.s.repos.tickets.attach_proof(tid, now=self.s.now(), tx_hash=tx, proof_url=attachment_url, proof_note=note[:300])
        channel = self.s.settings.proofs_channel_id or self.ticket_channel_id
        embed = Embed(title=f"💳 Payment proof · ticket #{tid}", color=0x57F287)
        embed.add("Customer", f"<@{customer.discord_id}>", True)
        embed.add("Tx hash", f"`{tx}`", False)
        embed.add("Explorer", f"https://bscscan.com/tx/{tx if tx.startswith('0x') else '0x' + tx}")
        if attachment_url:
            embed.add("Screenshot", attachment_url)
        if note:
            embed.add("Note", note[:300])
        embed.footer = "Admin: verify on-chain, then /admin extend user:@customer days:N"
        if channel:
            await self.s.discord.send(channel, "", embed=embed)
        if self.s.alerts:
            await self.s.alerts.admin(f"proof:{customer.discord_id}", f"Payment proof for ticket #{tid} from {customer.username}.", force=True)
            self.s.alerts.event(customer.discord_id, "proof_submitted", ticket=tid, tx=tx)
        return tid

    async def resolve(self, ticket_id: int, *, actor_id: str, status: str = "closed", note: str = "") -> bool:
        ok = self.s.repos.tickets.close(ticket_id, now=self.s.now(), status=status, resolved_by=actor_id, resolution=note[:300])
        if ok and self.s.alerts:
            await self.s.alerts.audit(actor_id, "ticket.resolve", ticket=ticket_id, status=status)
        return ok

    def open_tickets(self) -> list[dict]:
        return self.s.repos.tickets.list_open()

    # ── policy ──────────────────────────────────────────────────────────────
    def policy_acked(self, discord_id: str) -> bool:
        return self.s.repos.policy_acks.has_acked(discord_id, self.s.settings.policy_version)

    def ack_policy(self, discord_id: str) -> None:
        self.s.repos.policy_acks.ack(discord_id, self.s.settings.policy_version, now=self.s.now())
        if self.s.alerts:
            self.s.alerts.event(discord_id, "policy_ack", version=self.s.settings.policy_version)

    def policy_embed(self) -> Embed:
        embed = Embed(title=POLICY_TITLE, description=POLICY_TEXT, color=0x5865F2)
        embed.footer = f"version {self.s.settings.policy_version} · {POLICY_ACCEPT_LABEL}"
        return embed

    # ── support (ticket panel button) ───────────────────────────────────────
    async def open_support(self, *, discord_id: str, topic: str, username: str = "") -> Ticket:
        """Open a support thread from the ticket panel (P1-7).

        Deliberately independent of the ``customers`` table: the people clicking the panel
        button are usually *prospective* buyers who do not have a row yet.
        """
        topic = " ".join(str(topic or "").split())[:300]
        if len(topic) < MIN_SUPPORT_TOPIC_CHARS:
            raise ValidationError(f"❌ Please describe what you need in at least {MIN_SUPPORT_TOPIC_CHARS} characters.")
        existing = self.s.repos.tickets.find_open(discord_id, "support")
        if existing:
            raise ConflictError(f"⚠️ Your ticket #{existing['id']} is still open — an admin will answer there shortly.")
        tid = self.s.repos.tickets.open(discord_id, "support", now=self.s.now(), topic=topic, username=str(username or "")[:60])
        channel = self.ticket_channel_id
        thread_id = ""
        if channel:
            thread_id = await self.s.discord.create_thread(channel, f"ticket-{tid}", f"🎫 **Ticket #{tid}** — <@{discord_id}>\n{topic}")
        if self.s.alerts:
            await self.s.alerts.admin(f"ticket:support:{discord_id}", f"Support ticket #{tid} opened by {username or discord_id}: {topic[:120]}", force=True)
            self.s.alerts.event(discord_id, "ticket_opened", kind="support", ticket=tid)
        return Ticket(tid, "support", discord_id, thread_id or channel)
