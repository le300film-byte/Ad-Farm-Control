"""VIP commands: /vip autoreply, /vip squad."""
from __future__ import annotations

from ..core.errors import ValidationError
from ..core.rules import validate_autoreply
from ..discord.replies import Reply
from .context import CommandContext


async def vip(ctx: CommandContext) -> Reply:
    action = ctx.text("action", "autoreply").lower()
    customer = ctx.actor.customer if not (ctx.is_admin and ctx.text("customer")) else ctx.s.repos.customers.get(ctx.text("customer"))
    if customer is None:
        return Reply.error("❌ You do not have an active subscription. You are not authorized to use this command.")
    if action == "autoreply":
        text = ctx.text("text")
        if text.lower() in ("", "show"):
            current = customer.autoreply_text or "(not set)"
            return Reply.ok(f"🤖 Current DM auto-reply:\n```{current[:1500]}```\nSet a new one with `text:` or disable with `text:off`.")
        if text.lower() in ("off", "disable", "none"):
            await ctx.s.customers.set_autoreply(customer.discord_id, "")
            return Reply.ok("🤖 DM auto-reply disabled.")
        clean = validate_autoreply(text)
        await ctx.s.customers.set_autoreply(customer.discord_id, clean)
        return Reply.ok(f"🤖 DM auto-reply saved ({len(clean)} chars). Buyers who DM your alts get it once per {ctx.s.settings.autoreply_cooldown // 60} min.")
    if action == "squad":
        # Squads = named groups of the customer's own alts for one-shot tuning (VIP convenience).
        name = ctx.text("name").lower()
        members = ctx.text("alts")
        meta_key = f"squad:{customer.discord_id}"
        import json
        squads = json.loads(ctx.s.repos.meta.get(meta_key, "{}") or "{}")
        if not name:
            body = "\n".join(f"• **{k}** → alts {', '.join(map(str, v))}" for k, v in squads.items()) or "no squads yet"
            return Reply.ok(f"👥 Your squads:\n{body}")
        if members.lower() == "delete":
            squads.pop(name, None)
            ctx.s.repos.meta.set(meta_key, json.dumps(squads))
            return Reply.ok(f"👥 Squad **{name}** deleted.")
        try:
            idxs = sorted({int(x) for x in members.replace(" ", "").split(",") if x})
        except ValueError:
            raise ValidationError("❌ `alts` must be a comma-separated list of alt numbers, e.g. `1,2`.")
        owned = {a.alt_index for a in ctx.s.repos.alts.for_customer(customer.discord_id)}
        if not idxs or not set(idxs) <= owned:
            raise ValidationError(f"❌ You can only group your own registered alts ({', '.join(map(str, sorted(owned))) or 'none'}).")
        squads[name[:32]] = idxs
        ctx.s.repos.meta.set(meta_key, json.dumps(squads))
        return Reply.ok(f"👥 Squad **{name}** = alts {', '.join(map(str, idxs))}. Use it with `/tune squad:{name}`.")
    raise ValidationError("❌ Action must be autoreply or squad.")
