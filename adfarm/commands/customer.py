"""Customer commands. Each handler resolves the target alt through ``AltService.resolve`` so
ownership is enforced uniformly; admins may pass ``customer`` to act on behalf of someone."""
from __future__ import annotations

import base64

from ..core.errors import ValidationError
from ..core.models import Alt
from ..core.rules import validate_channel_ids, validate_snowflake
from ..discord.embeds import alt_status_embed, fleet_overview_embed
from ..discord.replies import Reply
from ..services.runs import RunRequest
from .context import CommandContext


def _target(ctx: CommandContext) -> Alt:
    customer_id = ctx.text("customer") or None
    return ctx.s.alts.resolve(ctx.actor, ctx.integer("alt"), customer_id=customer_id)


# ── /setup ─────────────────────────────────────────────────────────────────
async def setup(ctx: CommandContext) -> Reply:
    """Interactive path: the registry opens a modal (token + channels) and calls ``setup_submit``.
    Non-interactive path (tests / admin): options token, channels, alt, display_name."""
    if not ctx.text("token"):
        alts = ctx.s.alts.list_for(ctx.actor.customer.discord_id) if ctx.actor.customer else []
        return Reply(content="setup:modal", ephemeral=True, modal={"kind": "setup", "alt_count": ctx.actor.customer.alt_count if ctx.actor.customer else 1, "registered": [a.alt_index for a in alts]})
    return await setup_submit(ctx)


async def setup_submit(ctx: CommandContext) -> Reply:
    alt_index = ctx.integer("alt", 1) or 1
    alt = await ctx.s.alts.store_credentials(
        ctx.actor, alt_index, token=ctx.text("token"), channel_ids=ctx.text("channels"), display_name=ctx.text("display_name"),
        customer_id=ctx.text("customer") or None,
    )
    return Reply.ok(
        f"✅ Alt {alt.alt_index} (`{alt.label}`) is ready.\n"
        f"• Account: `{alt.username}` · Channels: {len(alt.channel_ids)} · Repo: `{alt.repo_slug}`\n"
        f"• Next: `/run alt:{alt.alt_index}` — your token was stored in the runner secrets, never in chat."
    )


# ── /run ───────────────────────────────────────────────────────────────────
async def run(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    if ctx.s.tickets and not ctx.s.tickets.policy_acked(alt.customer_id) and not ctx.flag("policy_ack", False):
        return Reply(content="policy:ack-required", embed=ctx.s.tickets.policy_embed(), ephemeral=True, view={"kind": "policy_ack"})
    if ctx.flag("policy_ack", False) and ctx.s.tickets and not ctx.s.tickets.policy_acked(alt.customer_id):
        ctx.s.tickets.ack_policy(alt.customer_id)
    if ctx.attachment_bytes is not None:
        from ..core.rules import IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES
        if ctx.attachment_content_type not in IMAGE_CONTENT_TYPES:
            raise ValidationError("❌ Image must be PNG, JPEG or WEBP.")
        if len(ctx.attachment_bytes) > MAX_IMAGE_BYTES:
            raise ValidationError("❌ Image must be smaller than 8 MB.")
    req = RunRequest.validated(
        mode=ctx.text("mode", "sell"), rate=ctx.text("rate"), message=ctx.text("message"), interval=ctx.integer("interval", 5) or 5,
        hours=ctx.integer("hours", 24) if ctx.integer("hours", 24) is not None else 24, attach_image=ctx.attachment_bytes is not None,
        buy_style=ctx.text("buy_style", "simple"), buy_items=ctx.text("buy_items"), buy_items_price=ctx.text("buy_items_price"), policy=ctx.text("policy"),
    )
    if req.attach_image and ctx.attachment_bytes is not None:
        await ctx.s.alts.push_secrets(alt)  # ensure channel/webhook secrets are fresh before the image run
        ctx.s.provisioner.set_secrets(alt.repo_owner, alt.repo_name, {"AD_IMAGE_B64": base64.b64encode(ctx.attachment_bytes).decode()})
    result = await ctx.s.runs.start(alt, req, actor_id=ctx.user_id)
    runtime = "limitless (auto-renewed every 48 h)" if req.limitless else f"{req.total_hours} h"
    return Reply.ok(
        f"🚀 Alt {alt.alt_index} (`{alt.label}`) dispatched.\n"
        f"• `{req.ad_type}` at `${req.rate:.2f}/1k` every {req.interval_min} min for {runtime}\n"
        f"• {len(alt.channel_ids)} channel(s) · run {result.run_url}\n"
        f"• First heartbeat arrives in #dashboard within ~5 minutes."
    )


# ── /stop /pause /resume ───────────────────────────────────────────────────
async def stop(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    cancelled = await ctx.s.runs.stop(alt, reason=ctx.text("reason", "customer stop"), actor_id=ctx.user_id)
    return Reply.ok(f"🛑 Stop sent to alt {alt.alt_index} (`{alt.label}`). " + (f"Cancelled {cancelled} GitHub run(s); " if cancelled else "") + "the sender exits within 30-45 s.")


async def pause(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    await ctx.s.runs.pause(alt, actor_id=ctx.user_id)
    return Reply.ok(f"⏸️ Alt {alt.alt_index} paused — public posts stop within ~30 s; the run stays alive. `/resume` to continue.")


async def resume(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    await ctx.s.runs.resume(alt, actor_id=ctx.user_id)
    return Reply.ok(f"▶️ Alt {alt.alt_index} resumed — normal posting within ~30 s.")


# ── /tune /deals /reply ────────────────────────────────────────────────────
async def tune(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    applied = await ctx.s.runs.tune(
        alt, actor_id=ctx.user_id, price=ctx.text("price") or None, message=ctx.text("message") or None, mode=ctx.text("mode") or None,
        interval=ctx.integer("interval"), hours=ctx.integer("hours"), policy=ctx.text("policy") or None,
    )
    return Reply.ok(f"🎛️ Alt {alt.alt_index}: " + "; ".join(applied) + ". Applied on the next sync (≤ 30 s).")


async def deals(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    applied = await ctx.s.runs.deals(alt, actor_id=ctx.user_id, keywords=ctx.text("keywords") or None, delta=ctx.text("delta") or None, enabled=ctx.flag("enabled"))
    return Reply.ok(f"📈 Alt {alt.alt_index} deal scanner: " + "; ".join(applied) + ".")


async def reply(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    user = validate_snowflake(ctx.text("user"), "User ID")
    text = ctx.text("text")
    await ctx.s.runs.reply(alt, user, text, actor_id=ctx.user_id)
    return Reply.ok(f"📩 Reply queued through alt {alt.alt_index} to `{user}`.")


# ── /channels ──────────────────────────────────────────────────────────────
async def channels(ctx: CommandContext) -> Reply:
    alt = _target(ctx)
    action = ctx.text("action", "view").lower()
    svc = ctx.s.alts
    if action in ("view", "list"):
        live = ctx.s.fleet.get((alt.customer_id, alt.alt_index))
        lines = []
        for cid in alt.channel_ids:
            stat = live.channels.get(cid) if live else None
            lines.append(f"• `{cid}`" + (f" #{stat.name} · {'✅' if stat.alive else '❌'} · sent {stat.sent} · errors {stat.errors}" if stat else ""))
        return Reply.ok(f"📡 Alt {alt.alt_index} channels ({len(alt.channel_ids)}/10):\n" + ("\n".join(lines) or "none"))
    if action == "add":
        alt = await svc.add_channel(alt, validate_snowflake(ctx.text("channel"), "Channel ID"), actor_id=ctx.user_id)
    elif action == "remove":
        alt = await svc.remove_channel(alt, validate_snowflake(ctx.text("channel"), "Channel ID"), actor_id=ctx.user_id)
    elif action == "replace":
        alt = await svc.replace_channel(alt, validate_snowflake(ctx.text("channel"), "Old channel ID"), validate_snowflake(ctx.text("new_channel"), "New channel ID"), actor_id=ctx.user_id)
    elif action == "overwrite":
        alt = await svc.set_channels(alt, validate_channel_ids(ctx.text("channels") or ctx.text("channel")), actor_id=ctx.user_id)
    elif action == "rescan":
        await ctx.s.runs.rescan(alt)
        return Reply.ok(f"🔎 Rescan requested for alt {alt.alt_index}; results appear in #farm-logs.")
    elif action == "reset_caution":
        await ctx.s.runs.reset_caution(alt, ctx.text("channel"))
        return Reply.ok(f"🚨 Caution/backoff reset requested for alt {alt.alt_index}.")
    else:
        raise ValidationError("❌ Action must be view, add, remove, replace, overwrite, rescan or reset_caution.")
    return Reply.ok(f"✅ Alt {alt.alt_index} now targets {len(alt.channel_ids)} channel(s). Live runs pick it up within ~30 s.")


# ── /status ────────────────────────────────────────────────────────────────
async def status(ctx: CommandContext) -> Reply:
    now = ctx.s.now()
    if ctx.flag("refresh", False):
        await ctx.s.runs.poll_runs()
    if ctx.is_admin and ctx.flag("fleet", False):
        rows = []
        for customer in ctx.s.repos.customers.all():
            for alt in ctx.s.repos.alts.for_customer(customer.discord_id):
                rows.append((customer, alt, ctx.s.fleet.get((customer.discord_id, alt.alt_index)), ctx.s.repos.runs.get(customer.discord_id, alt.alt_index)))
        return Reply(embed=fleet_overview_embed(rows, now), ephemeral=True)
    customer_id = ctx.text("customer") if ctx.is_admin and ctx.text("customer") else ctx.user_id
    customer = ctx.s.repos.customers.get(customer_id)
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    alts = ctx.s.repos.alts.for_customer(customer_id)
    if not alts:
        return Reply.ok("ℹ️ No alt registered yet. Run `/setup` first.")
    alt_index = ctx.integer("alt")
    chosen = [a for a in alts if alt_index is None or a.alt_index == alt_index]
    if not chosen:
        raise ValidationError(f"❌ Alt must be one of {', '.join(str(a.alt_index) for a in alts)}.")
    embeds = [alt_status_embed(a, ctx.s.fleet.get((customer_id, a.alt_index)), ctx.s.repos.runs.get(customer_id, a.alt_index), now) for a in chosen]
    reply = Reply(embed=embeds[0], ephemeral=not ctx.flag("post", False), followups=[Reply(embed=e, ephemeral=not ctx.flag("post", False)) for e in embeds[1:]])
    if ctx.flag("post", False) and customer.thread("dashboard"):
        for e in embeds:
            await ctx.s.discord.send(customer.thread("dashboard"), "", embed=e)
        reply.content = "📊 Dashboard card(s) posted to #dashboard."
    return reply


# ── /alt ───────────────────────────────────────────────────────────────────
async def alt(ctx: CommandContext) -> Reply:
    action = ctx.text("action", "overview").lower()
    target = _target(ctx)
    key = (target.customer_id, target.alt_index)
    if action == "overview":
        live = ctx.s.fleet.get(key)
        embed = alt_status_embed(target, live, ctx.s.repos.runs.get(*key), ctx.s.now())
        embed.add("Sender ALT_ID", f"`{target.sender_alt_id}`", True).add("Sync", target.sync_state.value, True)
        return Reply(embed=embed)
    if action == "logs":
        kind = ctx.text("kind") or None
        lines = ctx.s.fleet.recent_logs(key, limit=ctx.integer("limit", 20) or 20, kind=kind)
        body = "\n".join(f"<t:{int(l.ts)}:T> `{l.kind}` {l.text[:150]}" for l in lines) or "no log lines captured since the bot started"
        return Reply.ok(f"📜 Alt {target.alt_index} logs:\n{body}"[:1900])
    if action == "clearlogs":
        ctx.s.fleet.clear_logs(key)
        return Reply.ok(f"🧹 Cleared the in-memory log buffer of alt {target.alt_index}.")
    if action == "runs":
        runs = ctx.s.dispatcher.recent(target.repo_owner, target.repo_name, limit=5)
        body = "\n".join(f"• `{r.run_id}` {r.status}/{r.conclusion or '—'} {r.created_at} {r.html_url}" for r in runs) or "no runs yet"
        return Reply.ok(f"🏃 Recent runs of `{target.repo_slug}`:\n{body}")
    if action == "selfcheck":
        ctx.s.dispatcher.self_check(target.repo_owner, target.repo_name, ctx.s.settings.self_check_workflow)
        return Reply.ok(f"🩺 Self-check dispatched for alt {target.alt_index}; results in #farm-logs / GitHub Actions.")
    if action == "remove":
        if ctx.text("confirm").upper() != "REMOVE":
            raise ValidationError("❌ Type `REMOVE` in the confirm field to remove this alt (token and repo are wiped).")
        await ctx.s.alts.remove(target.customer_id, target.alt_index, actor_id=ctx.user_id, soft=True)
        return Reply.ok(f"🗑️ Alt {target.alt_index} removed. Run `/setup` to register a new one in that slot.")
    raise ValidationError("❌ Action must be overview, logs, clearlogs, runs, selfcheck or remove.")


# ── billing ────────────────────────────────────────────────────────────────
async def renew(ctx: CommandContext) -> Reply:
    customer = ctx.actor.customer if not (ctx.is_admin and ctx.text("customer")) else ctx.s.repos.customers.get(ctx.text("customer"))
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    ticket = await ctx.s.tickets.open_renewal(customer, days=ctx.integer("days", 30) or 30, note=ctx.text("note"))
    where = f"<#{ticket.channel_id}>" if ticket.channel_id else "the ticket channel"
    return Reply.ok(f"🧾 Renewal ticket #{ticket.id} opened in {where}. Pay to the address shown there, then post the tx hash with `/proofs`.")


async def pause_billing(ctx: CommandContext) -> Reply:
    customer = ctx.actor.customer
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    ticket = await ctx.s.tickets.open_pause_billing(customer, days=ctx.integer("days", 7) or 7, reason=ctx.text("reason"))
    return Reply.ok(f"⏸️ Billing-pause request #{ticket.id} sent to the admins. Your alts keep running until they confirm.")


async def proofs(ctx: CommandContext) -> Reply:
    customer = ctx.actor.customer
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    tid = await ctx.s.tickets.submit_proof(customer, tx_hash=ctx.text("tx_hash"), note=ctx.text("note"), attachment_url=ctx.attachment_url)
    return Reply.ok(f"💳 Proof recorded on ticket #{tid}. An admin verifies on-chain and extends your plan.")


