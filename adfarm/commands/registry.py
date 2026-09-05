"""Registry — binds the framework-neutral handlers to discord.py ``app_commands``.

This is the only command file importing discord.py. Every interaction follows the same path:
build ``CommandContext`` → ``Guard.check`` → handler → render ``Reply``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import discord
from discord import app_commands

from ..core.rules import AD_TYPES, INTERVALS_MIN, POLICY_TEMPLATES, RUNTIMES_HOURS
from ..discord.adapter import channel_ref, to_discord_embed
from ..discord.channels import ChannelClassifier
from ..discord.ports import ChannelRef
from ..discord.replies import Reply
from ..security.policy import ChannelKind
from ..services.container import Services
from . import admin as admin_cmds
from . import customer as cust
from . import public as pub
from . import vip as vip_cmds
from .context import CommandContext, Handler, run_handler

log = logging.getLogger(__name__)


class CommandRegistry:
    def __init__(self, tree: app_commands.CommandTree, services: Services, classifier: ChannelClassifier, *, guild_id: str = ""):
        self.tree = tree
        self.s = services
        self.classifier = classifier
        self.guild = discord.Object(id=int(guild_id)) if guild_id else None

    # ── context construction ────────────────────────────────────────────────
    def _context(self, inter: discord.Interaction, command: str, options: dict[str, Any]) -> tuple[CommandContext, Any]:
        ref = channel_ref(inter.channel) or ChannelRef(id=str(inter.channel_id or ""), kind="dm")
        info = self.classifier.classify(ref)
        gate = self.s.guard.check(str(inter.user.id), command, info)
        ctx = CommandContext(services=self.s, user_id=str(inter.user.id), username=inter.user.name, channel=ref, channel_info=info, kind=gate.kind,
                             actor=gate.actor, command=command, options=options)
        return ctx, gate

    async def _dispatch(self, inter: discord.Interaction, command: str, handler: Handler, options: dict[str, Any], *, attachment: Optional[discord.Attachment] = None) -> None:
        ctx, gate = self._context(inter, command, options)
        if not gate.decision.allowed:
            await inter.response.send_message(gate.decision.reason, ephemeral=True)
            return
        if attachment is not None:
            ctx.attachment_url = attachment.url
            ctx.attachment_content_type = attachment.content_type or ""
            try:
                ctx.attachment_bytes = await attachment.read()
            except discord.HTTPException:
                ctx.attachment_bytes = None
        modal = _modal_for(self, ctx, command)
        if modal is not None:
            await inter.response.send_modal(modal)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        reply = await run_handler(handler, ctx)
        await self.render(inter, reply, ctx)

    async def render(self, inter: discord.Interaction, reply: Reply, ctx: CommandContext | None = None) -> None:
        kwargs: dict[str, Any] = {"ephemeral": reply.ephemeral}
        if reply.embed is not None:
            kwargs["embed"] = to_discord_embed(reply.embed)
        view = _view_for(self, reply, ctx)
        if view is not None:
            kwargs["view"] = view
        content = reply.content if not reply.content.startswith(("policy:", "admin:", "setup:")) else None
        if reply.content == "admin:sync-commands":
            synced = await self.sync()
            content = f"🔁 Synced {synced} application command(s)."
        try:
            if inter.response.is_done():
                await inter.followup.send(content=content, **kwargs)
            else:
                await inter.response.send_message(content=content, **kwargs)
            for extra in reply.followups:
                await inter.followup.send(content=extra.content or None, embed=to_discord_embed(extra.embed) if extra.embed else None, ephemeral=extra.ephemeral)
        except discord.HTTPException as exc:
            log.warning("render failed: %s", exc)

    async def sync(self) -> int:
        if self.guild is not None:
            self.tree.copy_global_to(guild=self.guild)
            cmds = await self.tree.sync(guild=self.guild)
        else:
            cmds = await self.tree.sync()
        return len(cmds)

    # ── registration ────────────────────────────────────────────────────────
    def register_all(self) -> None:
        tree, reg = self.tree, self

        @tree.command(name="help", description="Show the commands available to you")
        async def _help(inter: discord.Interaction):
            await reg._dispatch(inter, "help", pub.help_, {})

        @tree.command(name="getstarted", description="How AdFarm works and how to buy a plan")
        async def _getstarted(inter: discord.Interaction):
            await reg._dispatch(inter, "getstarted", pub.getstarted, {})

        @tree.command(name="account", description="Your plan, expiry, alts and tickets")
        async def _account(inter: discord.Interaction):
            await reg._dispatch(inter, "account", pub.account, {})

        @tree.command(name="setup", description="Register an alt: token + target channels (private form)")
        @app_commands.describe(alt="Alt slot (1-4)")
        async def _setup(inter: discord.Interaction, alt: app_commands.Range[int, 1, 4] = 1):
            await reg._dispatch(inter, "setup", cust.setup, {"alt": alt})

        @tree.command(name="run", description="Start an alt")
        @app_commands.describe(mode="sell or buy", rate="price per 1k (0 < rate ≤ 20)", message="ad text (≤ 1900 chars)", interval="minutes between posts",
                               hours="runtime in hours (0 = limitless)", alt="which alt", image="optional image attached to every post", policy="posting policy preset")
        @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in AD_TYPES],
                              interval=[app_commands.Choice(name=f"{i} min", value=i) for i in INTERVALS_MIN],
                              hours=[app_commands.Choice(name=("Limitless" if h == 0 else f"{h} h"), value=h) for h in RUNTIMES_HOURS],
                              policy=[app_commands.Choice(name=p, value=p) for p in POLICY_TEMPLATES])
        async def _run(inter: discord.Interaction, mode: str, rate: str, message: str, interval: int = 5, hours: int = 24, alt: Optional[int] = None,
                       image: Optional[discord.Attachment] = None, policy: Optional[str] = None):
            await reg._dispatch(inter, "run", cust.run, {"mode": mode, "rate": rate, "message": message, "interval": interval, "hours": hours, "alt": alt, "policy": policy}, attachment=image)

        @tree.command(name="stop", description="Stop an alt's live run")
        async def _stop(inter: discord.Interaction, alt: Optional[int] = None, reason: Optional[str] = None):
            await reg._dispatch(inter, "stop", cust.stop, {"alt": alt, "reason": reason})

        @tree.command(name="pause", description="Pause public posting of an alt")
        async def _pause(inter: discord.Interaction, alt: Optional[int] = None):
            await reg._dispatch(inter, "pause", cust.pause, {"alt": alt})

        @tree.command(name="resume", description="Resume a paused alt")
        async def _resume(inter: discord.Interaction, alt: Optional[int] = None):
            await reg._dispatch(inter, "resume", cust.resume, {"alt": alt})

        @tree.command(name="tune", description="Change price / message / mode / cadence / runtime / policy live")
        @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in AD_TYPES],
                              interval=[app_commands.Choice(name=f"{i} min", value=i) for i in INTERVALS_MIN],
                              hours=[app_commands.Choice(name=f"{h} h", value=h) for h in RUNTIMES_HOURS if h],
                              policy=[app_commands.Choice(name=p, value=p) for p in POLICY_TEMPLATES])
        async def _tune(inter: discord.Interaction, alt: Optional[int] = None, price: Optional[str] = None, message: Optional[str] = None, mode: Optional[str] = None,
                        interval: Optional[int] = None, hours: Optional[int] = None, policy: Optional[str] = None):
            await reg._dispatch(inter, "tune", cust.tune, {"alt": alt, "price": price, "message": message, "mode": mode, "interval": interval, "hours": hours, "policy": policy})

        @tree.command(name="channels", description="Manage target channels of an alt")
        @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in ("view", "add", "remove", "replace", "overwrite", "rescan", "reset_caution")])
        async def _channels(inter: discord.Interaction, action: str = "view", alt: Optional[int] = None, channel: Optional[str] = None, new_channel: Optional[str] = None, channels: Optional[str] = None):
            await reg._dispatch(inter, "channels", cust.channels, {"action": action, "alt": alt, "channel": channel, "new_channel": new_channel, "channels": channels})

        @tree.command(name="deals", description="Deal scanner: keywords, edge threshold, on/off")
        async def _deals(inter: discord.Interaction, alt: Optional[int] = None, keywords: Optional[str] = None, delta: Optional[str] = None, enabled: Optional[bool] = None):
            await reg._dispatch(inter, "deals", cust.deals, {"alt": alt, "keywords": keywords, "delta": delta, "enabled": enabled})

        @tree.command(name="status", description="Live status of your alts")
        async def _status(inter: discord.Interaction, alt: Optional[int] = None, refresh: bool = False, post: bool = False, fleet: bool = False, customer: Optional[str] = None):
            await reg._dispatch(inter, "status", cust.status, {"alt": alt, "refresh": refresh, "post": post, "fleet": fleet, "customer": customer})

        @tree.command(name="reply", description="Reply to a buyer through one of your alts")
        async def _reply(inter: discord.Interaction, user: str, text: str, alt: Optional[int] = None):
            await reg._dispatch(inter, "reply", cust.reply, {"user": user, "text": text, "alt": alt})

        @tree.command(name="alt", description="Overview, logs, runs, self-check or removal of one alt")
        @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in ("overview", "logs", "clearlogs", "runs", "selfcheck", "remove")])
        async def _alt(inter: discord.Interaction, action: str = "overview", alt: Optional[int] = None, kind: Optional[str] = None, limit: Optional[int] = None, confirm: Optional[str] = None):
            await reg._dispatch(inter, "alt", cust.alt, {"action": action, "alt": alt, "kind": kind, "limit": limit, "confirm": confirm})

        @tree.command(name="renew", description="Open a renewal ticket")
        async def _renew(inter: discord.Interaction, days: app_commands.Range[int, 1, 366] = 30, note: Optional[str] = None):
            await reg._dispatch(inter, "renew", cust.renew, {"days": days, "note": note})

        @tree.command(name="pause-billing", description="Request a billing pause")
        async def _pause_billing(inter: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7, reason: Optional[str] = None):
            await reg._dispatch(inter, "pause-billing", cust.pause_billing, {"days": days, "reason": reason})

        @tree.command(name="proofs", description="Submit a payment proof (tx hash + optional screenshot)")
        async def _proofs(inter: discord.Interaction, tx_hash: str, note: Optional[str] = None, screenshot: Optional[discord.Attachment] = None):
            await reg._dispatch(inter, "proofs", cust.proofs, {"tx_hash": tx_hash, "note": note}, attachment=screenshot)

        vip_group = app_commands.Group(name="vip", description="VIP features")

        @vip_group.command(name="autoreply", description="Set / show / disable the DM auto-reply")
        async def _vip_autoreply(inter: discord.Interaction, text: Optional[str] = None):
            await reg._dispatch(inter, "vip", vip_cmds.vip, {"action": "autoreply", "text": text})

        @vip_group.command(name="squad", description="Group your alts under a name")
        async def _vip_squad(inter: discord.Interaction, name: Optional[str] = None, alts: Optional[str] = None):
            await reg._dispatch(inter, "vip", vip_cmds.vip, {"action": "squad", "name": name, "alts": alts})

        tree.add_command(vip_group)

        @tree.command(name="admin", description="Operator tools (admin rooms only)")
        @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in admin_cmds.ADMIN_ACTIONS])
        async def _admin(inter: discord.Interaction, action: str, user: Optional[str] = None, days: Optional[int] = None, alts: Optional[int] = None, vip: Optional[bool] = None,
                         alt: Optional[int] = None, sub: Optional[str] = None, confirm: Optional[str] = None, repo: Optional[str] = None, ticket: Optional[int] = None,
                         note: Optional[str] = None, reason: Optional[str] = None, channel: Optional[str] = None, fleet: Optional[bool] = None, enabled: Optional[bool] = None,
                         worker: Optional[str] = None, username: Optional[str] = None, limit: Optional[int] = None, hard: Optional[bool] = None):
            await reg._dispatch(inter, "admin", admin_cmds.admin, {
                "action": action, "user": user, "days": days, "alts": alts, "vip": vip, "alt": alt, "sub": sub, "confirm": confirm, "repo": repo, "ticket": ticket,
                "note": note, "reason": reason, "channel": channel, "fleet": fleet, "enabled": enabled, "worker": worker, "username": username, "limit": limit, "hard": hard,
            })


# ── interactive components ─────────────────────────────────────────────────
class SetupModal(discord.ui.Modal):
    def __init__(self, registry: CommandRegistry, ctx: CommandContext, alt_index: int):
        super().__init__(title=f"Setup alt {alt_index} (never share this token elsewhere)")
        self.registry, self.ctx, self.alt_index = registry, ctx, alt_index
        self.token = discord.ui.TextInput(label="Alt Discord user token", style=discord.TextStyle.short, required=True, max_length=120)
        self.channels = discord.ui.TextInput(label="Target channel IDs (comma-separated, ≤10)", style=discord.TextStyle.paragraph, required=True, max_length=400)
        self.name = discord.ui.TextInput(label="Display name (optional)", required=False, max_length=40)
        for item in (self.token, self.channels, self.name):
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction) -> None:  # type: ignore[override]
        await inter.response.defer(ephemeral=True, thinking=True)
        self.ctx.options.update({"alt": self.alt_index, "token": self.token.value, "channels": self.channels.value, "display_name": self.name.value})
        reply = await run_handler(cust.setup_submit, self.ctx)
        await self.registry.render(inter, reply, self.ctx)


class PolicyAckView(discord.ui.View):
    def __init__(self, registry: CommandRegistry, ctx: CommandContext):
        super().__init__(timeout=300)
        self.registry, self.ctx = registry, ctx

    @discord.ui.button(label="I accept the policy — start the run", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, _btn: discord.ui.Button) -> None:
        if str(inter.user.id) != self.ctx.user_id:
            await inter.response.send_message("❌ Only the person who issued /run can accept.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        self.ctx.options["policy_ack"] = True
        reply = await run_handler(cust.run, self.ctx)
        await self.registry.render(inter, reply, self.ctx)
        self.stop()


def _modal_for(registry: CommandRegistry, ctx: CommandContext, command: str) -> Optional[discord.ui.Modal]:
    if command == "setup" and not ctx.text("token"):
        return SetupModal(registry, ctx, ctx.integer("alt", 1) or 1)
    return None


def _view_for(registry: CommandRegistry, reply: Reply, ctx: CommandContext | None) -> Optional[discord.ui.View]:
    if isinstance(reply.view, dict) and reply.view.get("kind") == "policy_ack" and ctx is not None:
        return PolicyAckView(registry, ctx)
    if isinstance(reply.view, discord.ui.View):
        return reply.view
    return None
