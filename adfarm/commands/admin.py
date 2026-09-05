"""/admin command group (admin rooms only; every action audited)."""
from __future__ import annotations

import asyncio

from ..core.errors import NotFound, ValidationError
from ..core.models import AltStatus
from ..core.rules import validate_confirmation, validate_snowflake
from ..discord.embeds import customer_card, fleet_overview_embed
from ..discord.ports import Embed
from ..discord.replies import Reply
from ..security.policy import MULTISIG_ACTIONS
from .context import CommandContext

ADMIN_ACTIONS = (
    "list", "customer", "activate", "extend", "deactivate", "vip", "alt", "repo", "health", "backup", "tickets", "resolve",
    "ticket-panel", "payment-address", "sync-commands", "logs", "reset", "shutdown-bot",
)

# Rendered by /help-admin. One line per action: summary + a copy-pasteable example.
ADMIN_HELP: tuple[tuple[str, str, str], ...] = (
    ("list", "List every customer with plan, VIP flag and days left.", "/admin action:list  ·  /admin action:list fleet:true"),
    ("customer", "Full customer card: plan, expiry, hub, webhooks and alts.", "/admin action:customer user:2000…01"),
    ("activate", "Activate (or re-activate) a customer; creates their private hub.", "/admin action:activate user:2000…01 days:30 alts:2 vip:false"),
    ("extend", "Extend a plan by N days and close their open renewal ticket.", "/admin action:extend user:2000…01 days:30"),
    ("deactivate", "Stop every run, lock the hub read-only and remove roles.", "/admin action:deactivate user:2000…01 confirm:DEACTIVATE"),
    ("vip", "Grant or revoke VIP (adds #dm-inbox and DM auto-reply).", "/admin action:vip user:2000…01 enabled:true"),
    ("alt", "Manage one customer's alts: list, add, remove, sync, replace.", "/admin action:alt user:2000…01 sub:sync"),
    ("repo", "Alt repositories: list, push the sender, hard-delete an orphan.", "/admin action:repo sub:delete repo:worker1/foo_alt1 confirm:DELETE"),
    ("health", "Workers, backup, lease, dirty alts and config problems.", "/admin action:health"),
    ("backup", "Gist backup status, or force an upload right now.", "/admin action:backup sub:now  ·  sub:force"),
    ("tickets", "List open renewal / billing / support tickets.", "/admin action:tickets"),
    ("resolve", "Close a ticket after the payment was verified.", "/admin action:resolve ticket:12 note:paid"),
    ("ticket-panel", "Post the ticket panel (with its 🎫 button) and register the ticket channel.", "/admin action:ticket-panel channel:4000…03"),
    ("payment-address", "Show the configured BEP-20 payment address.", "/admin action:payment-address"),
    ("sync-commands", "Re-sync the slash commands to this guild.", "/admin action:sync-commands"),
    ("logs", "Recent audit/event log, optionally filtered by user.", "/admin action:logs user:2000…01 limit:50"),
    ("reset", "Platform reset: stop all runs and remove all alts (typed confirm:RESET).", "/admin action:reset confirm:RESET"),
    ("shutdown-bot", "Stop this runner after flushing the backup (typed confirm:SHUTDOWN).", "/admin action:shutdown-bot confirm:SHUTDOWN"),
)

ADMIN_HELP_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("👥 Customer Management", ("list", "customer", "activate", "extend", "deactivate", "vip")),
    ("📦 Alts / Repos", ("alt", "repo")),
    ("🩺 Ops", ("health", "backup", "tickets", "resolve", "ticket-panel", "payment-address", "sync-commands", "logs")),
    ("🧨 Destructive", ("reset", "shutdown-bot")),
)


def _user(ctx: CommandContext) -> str:
    return validate_snowflake(ctx.text("user") or ctx.text("customer"), "User ID")


async def admin(ctx: CommandContext) -> Reply:
    action = ctx.text("action").lower().replace("_", "-")
    handler = _ACTIONS.get(action)
    if handler is None:
        raise ValidationError("❌ Unknown admin action. Options: " + ", ".join(ADMIN_ACTIONS))
    return await handler(ctx)


# ── customers ──────────────────────────────────────────────────────────────
async def _list(ctx: CommandContext) -> Reply:
    now = ctx.s.now()
    customers = ctx.s.repos.customers.all()
    if ctx.flag("fleet", False):
        rows = [(c, a, ctx.s.fleet.get((c.discord_id, a.alt_index)), ctx.s.repos.runs.get(c.discord_id, a.alt_index)) for c in customers for a in ctx.s.repos.alts.for_customer(c.discord_id)]
        return Reply(embed=fleet_overview_embed(rows, now))
    lines = []
    for c in customers:
        flag = "🟢" if c.is_active(now) else "🔴"
        lines.append(f"{flag} <@{c.discord_id}> `{c.username}` · {c.alt_count} alt · {'VIP' if c.vip else 'std'} · {c.days_remaining(now):.0f} d left")
    embed = Embed(title=f"Customers ({len(customers)})", description="\n".join(lines[:50]) or "none")
    return Reply(embed=embed)


async def _customer(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    customer = ctx.s.customers.require(uid)
    alts = ctx.s.repos.alts.for_customer(uid, include_removed=True)
    hooks = ctx.s.customers.webhooks(uid)
    return Reply(embed=customer_card(customer, alts, ctx.s.now(), webhooks_ok=bool(hooks and hooks.complete())))


async def _activate(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    username = ctx.text("username") or await ctx.s.discord.display_name(uid) or uid
    result = await ctx.s.customers.activate(discord_id=uid, username=username, alt_count=ctx.integer("alts", 1) or 1, days=ctx.integer("days", 30) or 30,
                                            vip=bool(ctx.flag("vip", False)), actor_id=ctx.user_id)
    c = result.customer
    return Reply.ok(
        f"✅ {'Re-activated' if result.reactivated else 'Activated'} <@{c.discord_id}> — {c.alt_count} alt(s), {'VIP' if c.vip else 'standard'}, expires <t:{int(c.expiry_date)}:D>.\n"
        f"• Hub: <#{c.forum_id}> ({'created' if result.forum_created else 'reused'}) · webhooks {'✅' if result.webhooks_complete else '⚠️ incomplete — run /admin alt action:sync'}"
    )


async def _extend(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    c = await ctx.s.customers.extend(uid, ctx.integer("days", 30) or 30, actor_id=ctx.user_id)
    ticket = ctx.s.repos.tickets.find_open(uid, "renew")
    if ticket:
        await ctx.s.tickets.resolve(ticket["id"], actor_id=ctx.user_id, note="extended")
    if c.thread("control"):
        await ctx.s.discord.send(c.thread("control"), f"✅ <@{uid}> your plan was extended — new expiry <t:{int(c.expiry_date)}:D>.")
    return Reply.ok(f"✅ <@{uid}> extended to <t:{int(c.expiry_date)}:F>." + (f" Ticket #{ticket['id']} closed." if ticket else ""))


async def _deactivate(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    validate_confirmation(ctx.text("confirm"), "DEACTIVATE")
    await ctx.s.customers.deactivate(uid, reason=ctx.text("reason", "deactivated by admin"), actor_id=ctx.user_id)
    return Reply.ok(f"⛔ <@{uid}> deactivated: runs stopped, hub read-only, roles removed.")


async def _vip(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    c = await ctx.s.customers.set_vip(uid, bool(ctx.flag("enabled", True)), actor_id=ctx.user_id)
    return Reply.ok(f"{'⭐ VIP enabled' if c.vip else 'VIP removed'} for <@{uid}>.")


# ── alts / repos ───────────────────────────────────────────────────────────
async def _alt(ctx: CommandContext) -> Reply:
    uid = _user(ctx)
    sub = ctx.text("sub", "list").lower()
    if sub == "list":
        alts = ctx.s.repos.alts.for_customer(uid, include_removed=True)
        body = "\n".join(f"`{a.alt_index}` {a.label or '—'} · `{a.repo_slug}` · {a.status.value} · sync {a.sync_state.value} · ALT_ID {a.sender_alt_id}" for a in alts) or "none"
        return Reply.ok(f"Alts of <@{uid}>:\n{body}")
    if sub == "add":
        idx = ctx.integer("alt")
        if idx is None:
            existing = {a.alt_index for a in ctx.s.repos.alts.for_customer(uid)}
            idx = next(i for i in range(1, 5) if i not in existing)
        alt = await ctx.s.alts.register(uid, idx, actor_id=ctx.user_id, owner=ctx.text("worker") or None)
        return Reply.ok(f"✅ Alt {alt.alt_index} registered at `{alt.repo_slug}` (ALT_ID {alt.sender_alt_id}). Customer completes it with `/setup alt:{alt.alt_index}`.")
    if sub == "remove":
        validate_confirmation(ctx.text("confirm"), "REMOVE")
        alt = await ctx.s.alts.remove(uid, ctx.integer("alt", 1) or 1, actor_id=ctx.user_id, soft=not ctx.flag("hard", False))
        return Reply.ok(f"🗑️ Alt {alt.alt_index} of <@{uid}> removed (`{alt.repo_slug}`).")
    if sub == "sync":
        _, n = await ctx.s.customers.repair_hub(uid, actor_id=ctx.user_id)
        return Reply.ok(f"🔄 Hub threads/webhooks verified and {n} alt repo(s) re-synced for <@{uid}>.")
    if sub == "replace":
        alt = ctx.s.repos.alts.get(uid, ctx.integer("alt", 1) or 1)
        if alt is None:
            raise NotFound("❓ That alt is not registered.")
        fresh = await ctx.s.alts.prepare_replacement(alt, actor_id=ctx.user_id)
        return Reply.ok(f"♻️ Replacement repo `{fresh.repo_slug}` ready; customer runs `/setup alt:{fresh.alt_index}`.")
    raise ValidationError("❌ sub must be list, add, remove, sync or replace.")


async def _repo(ctx: CommandContext) -> Reply:
    sub = ctx.text("sub", "list").lower()
    if sub == "list":
        alts = ctx.s.repos.alts.all()
        by_owner: dict[str, list[str]] = {}
        for a in alts:
            by_owner.setdefault(a.repo_owner, []).append(f"{a.repo_name} ({a.customer_id}#{a.alt_index}, {a.status.value})")
        body = "\n".join(f"**{o}** ({len(v)}): " + ", ".join(v)[:600] for o, v in by_owner.items()) or "no repos"
        return Reply.ok(f"📦 Alt repositories:\n{body}"[:1900])
    if sub == "sync":
        count = 0
        for a in alts_ready(ctx):
            try:
                await asyncio.to_thread(ctx.s.provisioner.upload_sender, a.repo_owner, a.repo_name)
                count += 1
            except Exception:
                continue
        return Reply.ok(f"🔁 Sender files pushed to {count} repo(s) (version {ctx.s.provisioner.sender_version()}).")
    if sub == "delete":
        validate_confirmation(ctx.text("confirm"), "DELETE")
        owner, _, name = ctx.text("repo").partition("/")
        if not owner or not name:
            raise ValidationError("❌ repo must be owner/name.")
        alt = ctx.s.repos.alts.by_repo(owner, name)
        if alt and alt.status is not AltStatus.REMOVED:
            raise ValidationError("❌ That repo belongs to an active alt — use `/admin alt sub:remove` instead.")
        ok = await asyncio.to_thread(ctx.s.provisioner.hard_delete, owner, name)
        await ctx.s.alerts.audit(ctx.user_id, "repo.delete", repo=f"{owner}/{name}", ok=ok)
        return Reply.ok(f"{'🗑️ Deleted' if ok else '⚠️ Could not delete'} `{owner}/{name}`.")
    raise ValidationError("❌ sub must be list, sync or delete.")


def alts_ready(ctx: CommandContext):
    return [a for a in ctx.s.repos.alts.all() if a.status in (AltStatus.READY, AltStatus.PENDING)]


# ── ops ────────────────────────────────────────────────────────────────────
async def _health(ctx: CommandContext) -> Reply:
    embed = Embed(title="🩺 Health", color=0x5865F2)
    workers = await asyncio.to_thread(ctx.s.workers.health)
    embed.add("Workers", "\n".join(f"{'✅' if w.ok else '❌'} `{w.login}` — {w.detail}" for w in workers) or "none configured")
    b = ctx.s.backup.status()
    meta = await asyncio.to_thread(ctx.s.backup.remote_meta)
    embed.add("Backup", f"{'enabled' if b.enabled else 'DISABLED'} · last upload {('<t:%d:R>' % b.last_upload_at) if b.last_upload_at else 'never'} · remote seq {meta.get('seq', '?')} · {b.last_error or 'no errors'}")
    embed.add("Lease", f"holder `{b.lease_holder or '—'}` · expires {('<t:%d:R>' % b.lease_expires_at) if b.lease_expires_at else '—'}")
    dirty = ctx.s.repos.alts.dirty()
    missing = ctx.s.repos.alts.all(statuses=[AltStatus.MISSING])
    embed.add("Alts", f"{len(ctx.s.repos.alts.all())} total · {len(dirty)} dirty · {len(missing)} missing repo")
    no_hooks = [c.username for c in ctx.s.repos.customers.all(active_only=True) if not (ctx.s.customers.webhooks(c.discord_id) or None) or not ctx.s.customers.webhooks(c.discord_id).complete()]
    embed.add("Customers without webhooks", ", ".join(no_hooks) or "none")
    embed.add("Config problems", "\n".join(ctx.s.settings.problems()) or "none")
    embed.add("Run id", f"`{ctx.s.settings.run_id}`", True)
    return Reply(embed=embed)


async def _backup(ctx: CommandContext) -> Reply:
    sub = ctx.text("sub", "status").lower()
    if sub in ("now", "force"):
        force = sub == "force"
        ok = await asyncio.to_thread(lambda: ctx.s.backup.flush(force=force))
        await ctx.s.alerts.audit(ctx.user_id, "backup.now", ok=ok, force=force)
        if ok:
            return Reply.ok("💾 Backup uploaded." + (" (forced past the empty-database interlock.)" if force else ""))
        return Reply.ok(f"⚠️ Backup failed: {ctx.s.backup.last_error or 'disabled'}")
    b = ctx.s.backup.status()
    meta = await asyncio.to_thread(ctx.s.backup.remote_meta)
    blocked = " · ⛔ restore failed, uploads held (sub:force to override)" if b.restore_blocked else ""
    return Reply.ok(f"💾 Backup {'enabled' if b.enabled else 'disabled'} (gist `{b.gist_id or '—'}`) · pending={b.pending} · seq={b.seq}{blocked} · remote={meta}")


async def _tickets(ctx: CommandContext) -> Reply:
    rows = ctx.s.tickets.open_tickets()
    body = "\n".join(f"#{t['id']} {t['kind']} <@{t['customer_id']}> {t['payload'].get('days', '')}d {t['payload'].get('tx_hash', '')[:16]}" for t in rows) or "none"
    return Reply.ok(f"🎫 Open tickets:\n{body}")


async def _resolve(ctx: CommandContext) -> Reply:
    tid = ctx.integer("ticket")
    if not tid:
        raise ValidationError("❌ ticket id required.")
    ok = await ctx.s.tickets.resolve(tid, actor_id=ctx.user_id, status=ctx.text("status", "closed"), note=ctx.text("note"))
    return Reply.ok(f"✅ Ticket #{tid} resolved." if ok else f"❓ Ticket #{tid} not found.")


async def _ticket_panel(ctx: CommandContext) -> Reply:
    channel = ctx.text("channel") or ctx.channel.id
    ctx.s.tickets.set_ticket_channel(channel)
    embed = Embed(title="🎫 Tickets & payments", color=0x5865F2, description=(
        "• New here? Click **🎫 Open Ticket** below and tell us what you need.\n"
        "• Want the details first? Read `/getstarted`.\n"
        "• Renewing? Use `/renew` here or in your hub.\n"
        "• Paid? Post the tx hash with `/proofs`.\n"
        f"• Address (BEP-20): `{ctx.s.settings.payment_address or 'ask an admin'}`"))
    # P1-7: the panel is useless without the button. Handlers stay framework-neutral, so the
    # actual posting is handed back to the registry (the only module allowed to import
    # discord.py) which attaches the persistent ``TicketPanelView`` and pins the message.
    return Reply(content=f"📌 Ticket panel posted in <#{channel}> with the 🎫 Open Ticket button, and registered as the ticket channel.",
                 ephemeral=True, view={"kind": "post_ticket_panel", "channel": channel, "embed": embed})


async def _payment_address(ctx: CommandContext) -> Reply:
    return Reply.ok(f"💳 Payment address: `{ctx.s.settings.payment_address or '(not configured — set PAYMENT_ADDRESS)'}`")


# ── /help-admin ────────────────────────────────────────────────────────────
async def help_admin(ctx: CommandContext) -> Reply:
    """P1-3: the operator command reference. Rendered from ``ADMIN_HELP`` so it cannot drift
    from ``ADMIN_ACTIONS`` — and ``/help-admin`` itself is listed, so nothing is discoverable
    only by reading the source."""
    by_name = {h[0]: h for h in ADMIN_HELP}
    lines = [
        "Every operator command is `/admin action:<name>` (admin channels only).",
        "Destructive actions need a typed confirmation (`confirm:RESET` / `confirm:SHUTDOWN`).",
        "One admin is enough — no second-admin multi-sig.",
        "",
    ]
    for category, names in ADMIN_HELP_CATEGORIES:
        lines.append(f"**{category}**")
        for name in names:
            _n, summary, example = by_name[name]
            lines.append(f"• **{name}** — {summary}")
            lines.append(f"  `{example}`")
        lines.append("")
    embed = Embed(title="🛡️ AdFarm — operator commands", color=0xED4245,
                  description="\n".join(lines)[:4000])
    for name, summary, example in ADMIN_HELP:
        embed.add(f"/admin action:{name}", f"• {summary}\n`{example}`")
    embed.add("/help-admin", "Show this grouped reference.")
    missing = [a for a in ADMIN_ACTIONS if a not in {h[0] for h in ADMIN_HELP}]
    if missing:  # pragma: no cover - guarded by a test
        embed.add("⚠️ undocumented", ", ".join(missing))
    embed.footer = f"{len(ADMIN_HELP)} actions · typed confirmation on destructive commands"
    return Reply(embed=embed, ephemeral=True)


async def _sync_commands(ctx: CommandContext) -> Reply:
    # No view marker here: the registry performs the sync from the content sentinel and reports
    # the count itself. (V9.1 also returned {"kind": "sync_commands"}, which no renderer ever
    # handled — a marker for a button that did not exist.)
    return Reply(content="admin:sync-commands", ephemeral=True)


async def _logs(ctx: CommandContext) -> Reply:
    uid = ctx.text("user")
    events = ctx.s.repos.events.recent(limit=ctx.integer("limit", 20) or 20, discord_id=uid or None)
    body = "\n".join(f"<t:{int(e.ts)}:T> `{e.event}` {e.discord_id} {str(e.payload)[:80]}" for e in events) or "none"
    return Reply.ok(f"📜 Events:\n{body}"[:1900])


# ── destructive ────────────────────────────────────────────────────────────
async def _reset(ctx: CommandContext) -> Reply:
    validate_confirmation(ctx.text("confirm"), MULTISIG_ACTIONS[("admin", "reset")])
    stopped = 0
    for c in ctx.s.repos.customers.all():
        stopped += await ctx.s.runs.stop_all_for(c.discord_id, reason="platform reset")
    for a in ctx.s.repos.alts.all():
        try:
            await ctx.s.alts.remove(a.customer_id, a.alt_index, actor_id=ctx.user_id, soft=True)
        except Exception:
            continue
    await ctx.s.alerts.audit(ctx.user_id, "platform.reset", stopped=stopped)
    return Reply.ok(f"🧨 Reset executed: {stopped} run(s) stopped, all alts removed (repos renamed `_DELETED_`). Customer rows are kept; use `/admin deactivate` per customer if needed.")


async def _shutdown_bot(ctx: CommandContext) -> Reply:
    validate_confirmation(ctx.text("confirm"), MULTISIG_ACTIONS[("admin", "shutdown-bot")])
    ctx.s.shutdown_requested = True
    ctx.s.repos.meta.set("shutdown_requested", str(int(ctx.s.now())))
    await ctx.s.alerts.audit(ctx.user_id, "bot.shutdown")
    return Reply.ok("🛑 Shutdown confirmed. This runner stops after flushing the backup and releasing the DB lease; the next scheduled cron chunk (or a manual workflow dispatch) starts a fresh one.")


_ACTIONS = {
    "list": _list, "customer": _customer, "activate": _activate, "extend": _extend, "deactivate": _deactivate, "vip": _vip, "alt": _alt, "repo": _repo,
    "health": _health, "backup": _backup, "tickets": _tickets, "resolve": _resolve, "ticket-panel": _ticket_panel, "payment-address": _payment_address,
    "sync-commands": _sync_commands, "logs": _logs, "reset": _reset, "shutdown-bot": _shutdown_bot,
}
