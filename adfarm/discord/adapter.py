"""DiscordPyAdapter — implements ``DiscordPort`` with discord.py (the only place that touches it).

Imported lazily by ``app.py``; never imported by services or tests.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import discord

from .ports import ChannelRef, DiscordPort, Embed, ForumResult, ForumSpec, MessageRef

log = logging.getLogger(__name__)


def to_discord_embed(embed: Embed) -> discord.Embed:
    e = discord.Embed(title=embed.title[:256] or None, description=embed.description[:4096] or None, color=embed.color)
    for name, value, inline in embed.fields[:25]:
        e.add_field(name=name, value=value, inline=inline)
    if embed.footer:
        e.set_footer(text=embed.footer[:2048])
    if embed.timestamp:
        e.timestamp = discord.utils.utcnow()
    return e


def channel_ref(channel: Any) -> Optional[ChannelRef]:
    if channel is None:
        return None
    guild = getattr(channel, "guild", None)
    if guild is None or isinstance(channel, discord.DMChannel):
        return ChannelRef(id=str(getattr(channel, "id", "")), kind="dm")
    parent = getattr(channel, "parent", None)
    category = getattr(channel, "category", None) or getattr(parent, "category", None)
    kind = "thread" if isinstance(channel, discord.Thread) else "forum" if isinstance(channel, discord.ForumChannel) else "category" if isinstance(channel, discord.CategoryChannel) else "text"
    return ChannelRef(
        id=str(channel.id), name=str(getattr(channel, "name", "") or ""), kind=kind, parent_id=str(getattr(parent, "id", "") or ""),
        category_id=str(getattr(category, "id", "") or ""), category_name=str(getattr(category, "name", "") or ""), guild_id=str(guild.id),
    )


class DiscordPyAdapter(DiscordPort):
    def __init__(self, client: discord.Client, guild_id: str):
        self.client = client
        self.guild_id = int(guild_id) if guild_id else 0

    # ── helpers ─────────────────────────────────────────────────────────────
    @property
    def guild(self) -> Optional[discord.Guild]:
        if self.guild_id:
            return self.client.get_guild(self.guild_id)
        return self.client.guilds[0] if self.client.guilds else None

    async def _channel(self, channel_id: str) -> Any:
        if not channel_id:
            return None
        cid = int(channel_id)
        ch = self.client.get_channel(cid)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    async def _member(self, user_id: str) -> Optional[discord.Member]:
        guild = self.guild
        if guild is None or not user_id:
            return None
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                return None
        return member

    # ── lookup ──────────────────────────────────────────────────────────────
    async def get_channel(self, channel_id: str) -> Optional[ChannelRef]:
        return channel_ref(await self._channel(channel_id))

    async def find_channel_by_name(self, name: str) -> Optional[ChannelRef]:
        guild = self.guild
        if guild is None:
            return None
        wanted = name.lower().lstrip("#")
        for ch in list(guild.channels) + list(guild.threads):
            if str(getattr(ch, "name", "")).lower() == wanted:
                return channel_ref(ch)
        return None

    async def member_exists(self, user_id: str) -> bool:
        return await self._member(user_id) is not None

    async def display_name(self, user_id: str) -> str:
        member = await self._member(user_id)
        if member is not None:
            return member.display_name or member.name
        try:
            user = await self.client.fetch_user(int(user_id))
            return user.global_name or user.name
        except (discord.HTTPException, ValueError):
            return ""

    # ── messaging ───────────────────────────────────────────────────────────
    async def send(self, channel_id: str, content: str = "", *, embed: Embed | None = None, view: Any | None = None) -> Optional[str]:
        ch = await self._channel(channel_id)
        if ch is None or not hasattr(ch, "send"):
            return None
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions(users=True, roles=False, everyone=False)}
        if embed is not None:
            kwargs["embed"] = to_discord_embed(embed)
        if view is not None:
            kwargs["view"] = view
        try:
            msg = await ch.send(content[:2000] or None, **kwargs)
            return str(msg.id)
        except discord.HTTPException as exc:
            log.warning("send to %s failed: %s", channel_id, exc)
            return None

    async def edit_message(self, channel_id: str, message_id: str, content: str = "", *, embed: Embed | None = None) -> bool:
        ch = await self._channel(channel_id)
        if ch is None:
            return False
        try:
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(content=content[:2000] or None, embed=to_discord_embed(embed) if embed else None)
            return True
        except discord.HTTPException:
            return False

    async def dm(self, user_id: str, content: str, *, embed: Embed | None = None) -> bool:
        try:
            user = self.client.get_user(int(user_id)) or await self.client.fetch_user(int(user_id))
            await user.send(content[:2000] or None, embed=to_discord_embed(embed) if embed else None)
            return True
        except (discord.HTTPException, ValueError):
            return False

    async def recent_messages(self, channel_id: str, limit: int = 50) -> list[MessageRef]:
        ch = await self._channel(channel_id)
        if ch is None or not hasattr(ch, "history"):
            return []
        out: list[MessageRef] = []
        try:
            async for m in ch.history(limit=limit):
                out.append(MessageRef(id=str(m.id), channel_id=channel_id, content=m.content or "", author_id=str(m.author.id), author_name=m.author.name,
                                      is_webhook=bool(m.webhook_id), embeds=list(m.embeds)))
        except discord.HTTPException:
            pass
        return out

    async def pin(self, channel_id: str, message_id: str) -> bool:
        ch = await self._channel(channel_id)
        try:
            msg = await ch.fetch_message(int(message_id))
            await msg.pin()
            return True
        except (AttributeError, discord.HTTPException):
            return False

    # ── structure ───────────────────────────────────────────────────────────
    async def create_customer_forum(self, spec: ForumSpec) -> ForumResult:
        guild = self.guild
        if guild is None:
            raise RuntimeError("bot is not in the configured guild")
        category = guild.get_channel(int(spec.category_id)) if spec.category_id else None
        member = await self._member(spec.customer_user_id)
        me = guild.me
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, manage_webhooks=True,
                                            create_public_threads=True, create_private_threads=True, send_messages_in_threads=True),
        }
        if member is not None:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True, read_message_history=True,
                                                             attach_files=True, use_application_commands=True)
        if spec.admin_role_id:
            role = guild.get_role(int(spec.admin_role_id))
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, send_messages_in_threads=True)
        existing = next((c for c in guild.forums if c.name == spec.name and (category is None or c.category_id == getattr(category, "id", None))), None)
        created = existing is None
        forum = existing or await guild.create_forum(name=spec.name, category=category, overwrites=overwrites, reason="AdFarm customer hub")
        thread_ids: dict[str, str] = {}
        current = {t.name: t for t in list(forum.threads)}
        for role_name, thread_name, opening in spec.threads:
            thread = current.get(thread_name)
            if thread is None:
                try:
                    thread, _ = await forum.create_thread(name=thread_name, content=opening, reason="AdFarm customer thread")
                except discord.HTTPException as exc:
                    log.warning("thread %s creation failed: %s", thread_name, exc)
                    continue
            thread_ids[role_name] = str(thread.id)
        webhooks = await self.ensure_forum_webhooks(str(forum.id), thread_ids)
        return ForumResult(forum_id=str(forum.id), thread_ids=thread_ids, webhooks=webhooks, created=created)

    async def ensure_forum_webhooks(self, forum_id: str, thread_ids: dict[str, str]) -> dict[str, str]:
        """One webhook on the forum per sender-facing thread; the URL carries ``?thread_id=`` so
        posts land in the right thread (Discord webhooks on forums require a thread target)."""
        forum = await self._channel(forum_id)
        if forum is None or not hasattr(forum, "webhooks"):
            return {}
        try:
            existing = {w.name: w for w in await forum.webhooks()}
        except discord.HTTPException:
            existing = {}
        urls: dict[str, str] = {}
        for role_name in ("dashboard", "farm-logs", "deals", "dm-inbox"):
            tid = thread_ids.get(role_name)
            if not tid:
                continue
            name = f"adfarm-{role_name}"
            hook = existing.get(name)
            if hook is None:
                try:
                    hook = await forum.create_webhook(name=name, reason="AdFarm sender webhook")
                except discord.HTTPException as exc:
                    log.warning("webhook %s creation failed: %s", name, exc)
                    continue
            urls[role_name] = f"{hook.url}?thread_id={tid}"
        return urls

    async def set_forum_readonly(self, forum_id: str, customer_user_id: str, readonly: bool) -> bool:
        forum = await self._channel(forum_id)
        member = await self._member(customer_user_id)
        if forum is None or member is None:
            return False
        try:
            await forum.set_permissions(member, view_channel=True, send_messages=not readonly, send_messages_in_threads=not readonly, read_message_history=True,
                                        use_application_commands=not readonly, reason="AdFarm subscription state change")
            return True
        except discord.HTTPException:
            return False

    async def restore_forum_access(self, forum_id: str, customer_user_id: str) -> bool:
        return await self.set_forum_readonly(forum_id, customer_user_id, False)

    async def delete_channel(self, channel_id: str) -> bool:
        ch = await self._channel(channel_id)
        if ch is None:
            return False
        try:
            await ch.delete(reason="AdFarm cleanup")
            return True
        except discord.HTTPException:
            return False

    async def grant_role(self, user_id: str, role_name: str) -> bool:
        member = await self._member(user_id)
        guild = self.guild
        if member is None or guild is None:
            return False
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            try:
                role = await guild.create_role(name=role_name, reason="AdFarm role")
            except discord.HTTPException:
                return False
        try:
            await member.add_roles(role, reason="AdFarm")
            return True
        except discord.HTTPException:
            return False

    async def revoke_role(self, user_id: str, role_name: str) -> bool:
        member = await self._member(user_id)
        guild = self.guild
        if member is None or guild is None:
            return False
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None or role not in member.roles:
            return True
        try:
            await member.remove_roles(role, reason="AdFarm")
            return True
        except discord.HTTPException:
            return False


# ═════════════════════════════════════════════════════════════════════════════
class DiscordPyGuildAdmin:
    """discord.py implementation of ``provision.GuildAdminPort`` (server layout admin).

    Kept beside the adapter because this is the only other place allowed to import
    discord.py. Every method is idempotent-friendly: lookups return ``None`` instead of
    raising when something is missing.
    """

    def __init__(self, client: discord.Client, guild_id: str):
        self.client = client
        self.guild_id = int(guild_id) if guild_id else 0

    @property
    def guild(self) -> Optional[discord.Guild]:
        if self.guild_id:
            return self.client.get_guild(self.guild_id)
        return self.client.guilds[0] if self.client.guilds else None

    def _require_guild(self) -> discord.Guild:
        guild = self.guild
        if guild is None:
            raise RuntimeError("bot is not in the configured guild (check GUILD_ID and the invite)")
        return guild

    # ── permission translation ──────────────────────────────────────────────
    async def _resolve(self, guild: discord.Guild, overwrites) -> dict[Any, discord.PermissionOverwrite]:
        out: dict[Any, discord.PermissionOverwrite] = {}
        for ow in overwrites:
            if ow.target == "everyone":
                target: Any = guild.default_role
            elif ow.target == "bot":
                target = guild.me
            elif ow.target == "role":
                target = guild.get_role(int(ow.target_id)) if ow.target_id else None
            else:
                target = guild.get_member(int(ow.target_id)) if ow.target_id else None
                if target is None and ow.target_id:
                    try:
                        target = await guild.fetch_member(int(ow.target_id))
                    except (discord.HTTPException, ValueError):
                        target = None
            if target is None:
                continue
            perm = discord.PermissionOverwrite()
            for name in ow.allow:
                setattr(perm, name, True)
            for name in ow.deny:
                setattr(perm, name, False)
            out[target] = perm
        return out

    # ── categories ──────────────────────────────────────────────────────────
    async def find_category(self, name: str) -> Optional[str]:
        guild = self._require_guild()
        wanted = name.strip().lower()
        for cat in guild.categories:
            if cat.name.strip().lower() == wanted:
                return str(cat.id)
        return None

    async def create_category(self, name: str, overwrites) -> str:
        guild = self._require_guild()
        cat = await guild.create_category(name, overwrites=await self._resolve(guild, overwrites), reason="AdFarm setup")
        return str(cat.id)

    # ── channels ────────────────────────────────────────────────────────────
    async def find_text_channel(self, name: str) -> Optional[str]:
        guild = self._require_guild()
        wanted = name.strip().lower().lstrip("#")
        for ch in guild.text_channels:
            if ch.name.strip().lower() == wanted:
                return str(ch.id)
        return None

    async def create_text_channel(self, name: str, *, category_id: str, topic: str, overwrites) -> str:
        guild = self._require_guild()
        category = guild.get_channel(int(category_id)) if category_id else None
        ch = await guild.create_text_channel(name, category=category if isinstance(category, discord.CategoryChannel) else None,
                                             topic=topic or None, overwrites=await self._resolve(guild, overwrites), reason="AdFarm setup")
        return str(ch.id)

    async def apply_overwrites(self, channel_id: str, overwrites) -> bool:
        guild = self._require_guild()
        ch = guild.get_channel(int(channel_id))
        if ch is None:
            return False
        resolved = await self._resolve(guild, overwrites)
        ok = True
        for target, perm in resolved.items():
            try:
                await ch.set_permissions(target, overwrite=perm, reason="AdFarm setup")
            except discord.HTTPException as exc:
                log.warning("set_permissions on %s failed: %s", channel_id, exc)
                ok = False
        return ok

    async def move_to_category(self, channel_id: str, category_id: str) -> bool:
        guild = self._require_guild()
        ch = guild.get_channel(int(channel_id))
        cat = guild.get_channel(int(category_id)) if category_id else None
        if ch is None or not isinstance(cat, discord.CategoryChannel):
            return False
        if getattr(ch, "category_id", None) == cat.id:
            return True
        try:
            await ch.edit(category=cat, reason="AdFarm setup")
            return True
        except discord.HTTPException as exc:
            log.warning("move %s to category failed: %s", channel_id, exc)
            return False

    # ── roles ───────────────────────────────────────────────────────────────
    async def ensure_role(self, name: str) -> str:
        guild = self._require_guild()
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, hoist=True, mentionable=False, reason="AdFarm setup")
        return str(role.id)

    async def assign_role(self, role_id: str, user_id: str) -> bool:
        guild = self._require_guild()
        role = guild.get_role(int(role_id))
        if role is None:
            return False
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except (discord.HTTPException, ValueError):
                return False
        if role in member.roles:
            return True
        try:
            await member.add_roles(role, reason="AdFarm admin")
            return True
        except discord.HTTPException:
            return False
