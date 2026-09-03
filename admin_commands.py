"""admin_commands.py – V8 Admin-Only Slash Commands (Cog).

All commands in this cog require the invoking user to be in OWNER_IDS.
They are hidden from users who are not admins.

Commands:
  /admin list               – Show all active customers
  /admin activate @User days alts vip
                            – Onboard a new customer
  /admin extend @User days  – Extend a subscription
  /admin deactivate @User   – Shut down and lock a customer
  /admin shutdown all       – Emergency kill-switch for all customers
  /admin repo sync          – Push latest send_ads.py to all repos
  /admin logs @User         – View a customer's recent log thread
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import customer_manager as cm
from security import check_admin, is_admin


class AdminCog(commands.Cog):
    """Hidden admin panel (OWNER_IDS only)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._audit_ch_id: Optional[int] = None

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _audit(self, message: str) -> None:
        """Post an action to #audit-logs."""
        import os
        ch_id = self._audit_ch_id or int(os.environ.get("AUDIT_LOG_CH_ID", "0") or "0")
        if not ch_id:
            print(f"[AUDIT] {message}")
            return
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                print(f"[AUDIT-FALLBACK] {message}")
                return
        try:
            await ch.send(message[:2000], allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            print(f"[AUDIT] Failed to post: {exc}")

    async def _send_welcome_dm(self, user: discord.User, username: str) -> None:
        try:
            from control_bot.policy import SETUP_VIDEO_URL
            await user.send(
                f"🎉 **Welcome to AdFarm V8, {username}!**\n\n"
                "Your account has been activated. Run `/setup` in your "
                "`#control` thread to enter your alt tokens and channels. "
                "Once set up, use `/run` to start your ad farm.\n\n"
                f"🎥 **First time? Watch the 3-min token walkthrough:**\n{SETUP_VIDEO_URL}"
            )
        except Exception:
            pass

    # ── /admin group ─────────────────────────────────────────────────────────

    admin_group = app_commands.Group(
        name="admin",
        description="V8 admin panel (owner-only)",
    )

    # /admin list
    @admin_group.command(name="list", description="Show all active customers.")
    async def admin_list(self, inter: discord.Interaction) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        customers = cm.list_customers(active_only=False)
        if not customers:
            await inter.followup.send("📋 No customers in the database.", ephemeral=True)
            return

        now = time.time()
        lines = ["```", f"{'ID':<22} {'User':<20} {'Alts':>4} {'VIP':>4} {'Days':>6} {'Status':>8}"]
        lines.append("-" * 70)
        for c in customers:
            days_left = (c["expiry_date"] - now) / 86400
            status = "active" if c["active"] and days_left > 0 else "expired"
            vip_str = "✅" if c["vip"] else "—"
            lines.append(
                f"{c['discord_id']:<22} {c['discord_username'][:19]:<20} "
                f"{c['alt_count']:>4} {vip_str:>4} {days_left:>6.1f} {status:>8}"
            )
        lines.append("```")
        await inter.followup.send("\n".join(lines), ephemeral=True)

    # /admin activate
    @admin_group.command(
        name="activate",
        description="Onboard a new customer and provision their forum + repos.",
    )
    @app_commands.describe(
        user="The Discord user to activate",
        days="Subscription length in days",
        alts="Number of alt accounts",
        vip="Enable VIP tier features",
        github_account="GitHub account to use for this customer's repos",
    )
    async def admin_activate(
        self,
        inter: discord.Interaction,
        user: discord.Member,
        days: int = 30,
        alts: int = 1,
        vip: bool = False,
        github_account: str = "",
    ) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)

        uid = str(user.id)
        uname = user.display_name

        # 1. Create GitHub repos
        repos: list[str] = []
        gh_account = github_account.strip()
        from github_dispatch import provision_alt_repo
        if not gh_account:
            # Use round-robin worker selection for new customer repos
            from github_dispatch import pick_worker
            for i in range(1, alts + 1):
                repo_name = f"{uname.lower().replace(' ', '_')}_alt{i}"
                try:
                    worker_user, worker_token = pick_worker()
                    html_url = await asyncio.to_thread(
                        provision_alt_repo, worker_user, repo_name, worker_token
                    )
                    repos.append(repo_name)
                    await inter.followup.send(
                        f"✅ Created repo: [{repo_name}]({html_url})", ephemeral=True
                    )
                except Exception as exc:
                    await inter.followup.send(
                        f"⚠️ Repo `{repo_name}` creation failed: {exc}", ephemeral=True
                    )

        # 2. Create Discord forum
        forum_ids: dict[str, int] = {}
        if inter.guild:
            try:
                from discord_forum import create_customer_forum
                bot_member = inter.guild.me
                # Find an admin role if one exists
                admin_role = discord.utils.get(inter.guild.roles, name="Admin")
                forum_ids = await create_customer_forum(
                    inter.guild, bot_member, user,
                    display_name=uname, vip=vip, admin_role=admin_role,
                )
            except Exception as exc:
                await inter.followup.send(
                    f"⚠️ Forum creation failed: {exc}", ephemeral=True
                )

        # 3. Store customer record
        cm.add_customer(
            discord_id=uid,
            discord_username=uname,
            alt_count=alts,
            vip=vip,
            days=days,
            github_account=gh_account,
            repos=repos,
            forum_id=str(forum_ids.get("forum_id", "")),
            control_thread_id=str(forum_ids.get("control_thread_id", "")),
            dashboard_thread_id=str(forum_ids.get("dashboard_thread_id", "")),
            logs_thread_id=str(forum_ids.get("logs_thread_id", "")),
            dm_thread_id=str(forum_ids.get("dm_thread_id", "")),
            deals_thread_id=str(forum_ids.get("deals_thread_id", "")),
        )

        # 4. Send welcome DM
        await self._send_welcome_dm(user, uname)

        # 5. Audit log
        await self._audit(
            f"✅ **Activated** `{uname}` (`{uid}`) — {days}d, {alts} alt(s), "
            f"VIP={'yes' if vip else 'no'}, repos={repos}"
        )

        await inter.followup.send(
            f"🎉 **{uname}** has been activated!\n"
            f"• Days: **{days}**  • Alts: **{alts}**  • VIP: **{'✅' if vip else '❌'}**\n"
            f"• Repos: `{repos}`\n"
            f"• Forum ID: `{forum_ids.get('forum_id', 'N/A')}`",
            ephemeral=True,
        )

    # /admin extend
    @admin_group.command(name="extend", description="Extend a customer's subscription.")
    @app_commands.describe(user="The customer", days="Number of days to add")
    async def admin_extend(
        self,
        inter: discord.Interaction,
        user: discord.Member,
        days: int = 7,
    ) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        uid = str(user.id)
        ok = cm.extend_customer(uid, days)
        if ok:
            await self._audit(
                f"📅 **Extended** `{user.display_name}` (`{uid}`) by {days} day(s)"
            )
            await inter.followup.send(
                f"📅 Extended **{user.display_name}**'s subscription by **{days}** day(s).",
                ephemeral=True,
            )
        else:
            await inter.followup.send(
                f"❌ Customer `{user.display_name}` not found in the database.",
                ephemeral=True,
            )

    # /admin deactivate
    @admin_group.command(name="deactivate", description="Shut down and lock a customer.")
    @app_commands.describe(user="The customer to deactivate")
    async def admin_deactivate(
        self,
        inter: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        uid = str(user.id)
        from timer_engine import run_shutdown_for_customer
        await run_shutdown_for_customer(uid)
        await self._audit(
            f"🔴 **Deactivated** `{user.display_name}` (`{uid}`) by admin "
            f"`{inter.user.display_name}`"
        )
        await inter.followup.send(
            f"🔴 **{user.display_name}** has been deactivated and their alts stopped.",
            ephemeral=True,
        )

    # /admin shutdown all
    @admin_group.command(
        name="shutdown",
        description="Emergency kill-switch: shut down ALL active customers (2-admin confirm).",
    )
    @app_commands.describe(confirm="Type 'ALL' to confirm emergency shutdown")
    async def admin_shutdown_all(
        self, inter: discord.Interaction, confirm: str = ""
    ) -> None:
        if not await check_admin(inter):
            return
        if confirm.upper() != "ALL":
            await inter.response.send_message(
                "⚠️ To confirm, run `/admin shutdown confirm:ALL`", ephemeral=True
            )
            return
        # TODO 3.2 — multi-sig: a different admin must confirm within 120s.
        from security import MULTISIG
        state, msg = MULTISIG.request("admin_shutdown_all", inter.user.id)
        await inter.response.send_message(msg, ephemeral=True)
        if state != "confirmed":
            await self._audit(
                f"🚨 **{state.upper()}** emergency shutdown ALL by "
                f"`{inter.user.display_name}` (awaiting 2nd admin)"
            )
            return
        await inter.response.defer(ephemeral=True)
        customers = cm.list_customers(active_only=True)
        from timer_engine import run_shutdown_for_customer
        tasks = [run_shutdown_for_customer(c["discord_id"]) for c in customers]
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._audit(
            f"🚨 **EMERGENCY SHUTDOWN ALL** executed by `{inter.user.display_name}` — "
            f"{len(customers)} customer(s) stopped."
        )
        await inter.followup.send(
            f"🚨 Emergency shutdown complete — **{len(customers)}** customer(s) deactivated.",
            ephemeral=True,
        )

    # /admin repo sync
    @admin_group.command(
        name="repo",
        description="Sync the latest send_ads.py to all customer repos.",
    )
    @app_commands.describe(action="Action to perform (sync)")
    async def admin_repo(
        self, inter: discord.Interaction, action: str = "sync"
    ) -> None:
        if not await check_admin(inter):
            return
        if action.lower() != "sync":
            await inter.response.send_message(
                "ℹ️ Available actions: `sync`", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        customers = cm.list_customers(active_only=False)
        from github_dispatch import sync_sender_to_all_repos
        results = await asyncio.to_thread(sync_sender_to_all_repos, customers)
        ok = sum(1 for v in results.values() if v == "ok")
        fail = len(results) - ok
        await self._audit(
            f"🔄 **Repo sync** by `{inter.user.display_name}` — {ok} ok, {fail} failed"
        )
        lines = [f"`{k}`: {v}" for k, v in results.items()]
        body = "\n".join(lines) or "No repos to sync."
        await inter.followup.send(
            f"🔄 Repo sync complete: **{ok} ok**, **{fail} failed**\n{body[:1800]}",
            ephemeral=True,
        )

    # /admin expiry-alerts — dry-run (TODO 0.6)
    @admin_group.command(
        name="expiry-alerts",
        description="Dry-run the reminder/expiry path without messaging customers.",
    )
    async def admin_expiry_alerts(self, inter: discord.Interaction) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        from timer_engine import dry_run_expiry_alerts
        report = await dry_run_expiry_alerts()
        lines = [
            f"🧪 **Expiry-alert dry-run** — {len(report['expired'])} expired, "
            f"{len(report['reminders'])} reminder(s) in window.",
        ]
        for r in report["reminders"]:
            if r["would_send"]:
                lines.append(
                    f"• WOULD SEND {r['threshold']}d → `{r['username']}` "
                    f"({r['days_left']}d left)"
                )
        for r in report["expired"]:
            lines.append(f"• EXPIRED → `{r['username']}` ({len(r['repos'])} repo(s))")
        if not lines[1:]:
            lines.append("• No actions would fire right now.")
        await self._audit(
            "🧪 **Expiry-alerts dry-run** executed by "
            f"`{inter.user.display_name}` — {len(report['reminders'])} due "
            f"({sum(1 for r in report['reminders'] if r['would_send'])} would send)"
        )
        await inter.followup.send("\n".join(lines), ephemeral=True)

    # /admin pin-policy — TODO 0.4
    @admin_group.command(
        name="pin-policy",
        description="Pin the pre-payment policy card + privacy notice in #open-ticket.",
    )
    @app_commands.describe(channel="Target text channel (default: OPEN_TICKET_CH_ID)")
    async def admin_pin_policy(
        self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        if not await check_admin(inter):
            return
        import os as _os
        ch = channel
        if ch is None:
            ch_id = _os.environ.get("OPEN_TICKET_CH_ID", "") or _os.environ.get("TICKET_CH_ID", "")
            if ch_id:
                ch = self.bot.get_channel(int(ch_id))
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(int(ch_id))
                    except Exception:
                        ch = None
        if ch is None:
            await inter.response.send_message(
                "❌ No target channel. Pass `channel:` or set OPEN_TICKET_CH_ID.",
                ephemeral=True,
            )
            return
        from control_bot.policy import pin_policy_in_channel
        ok = await pin_policy_in_channel(ch)
        await inter.response.send_message(
            "📌 Policy card pinned."
            if ok else "⚠️ Policy card sent (pinning may not be permitted).",
            ephemeral=True,
        )

    # /admin activate-template — one-click activation pre-filled from ticket (1.1)
    # /admin ticket-panel — post the ticket button in #open-ticket
    @admin_group.command(
        name="ticket-panel",
        description="Post the 🎫 Open Ticket button in #open-ticket.",
    )
    @app_commands.describe(channel="Target channel (default: OPEN_TICKET_CH_ID)")
    async def admin_ticket_panel(
        self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        if not await check_admin(inter):
            return
        import os as _os
        ch = channel
        if ch is None:
            ch_id = _os.environ.get("OPEN_TICKET_CH_ID", "")
            if ch_id:
                ch = self.bot.get_channel(int(ch_id))
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(int(ch_id))
                    except Exception:
                        ch = None
        if ch is None:
            await inter.response.send_message(
                "❌ No target channel. Pass `channel:` or set OPEN_TICKET_CH_ID.",
                ephemeral=True,
            )
            return
        from control_bot.tickets import TicketPanelView
        embed = discord.Embed(
            title="🎫 Need Help? Open a Ticket!",
            description=(
                "Click the button below to open a support ticket.\n\n"
                "**💰 Payment** — subscription, pricing, activation\n"
                "**🐛 Bug Report** — something is broken\n"
                "**💡 Suggestion** — feature request or idea\n\n"
                "A private thread will be created where you can talk directly to the team."
            ),
            color=0x5865F2,
        )
        await ch.send(embed=embed, view=TicketPanelView())
        await inter.response.send_message(
            f"✅ Ticket panel posted in {ch.mention}", ephemeral=True
        )

    @admin_group.command(
        name="activate-template",
        description="Generate a pre-filled /admin activate command from a customer ticket.",
    )
    @app_commands.describe(user="Customer", tx_hash="BEP-20 transaction hash (optional)")
    async def admin_activate_template(
        self, inter: discord.Interaction, user: discord.Member, tx_hash: str = ""
    ) -> None:
        if not await check_admin(inter):
            return
        c = cm.get_customer(str(user.id))
        if not c:
            await inter.response.send_message(
                f"❌ No record for `{user.display_name}`.", ephemeral=True
            )
            return
        from control_bot.payments import activation_template
        await inter.response.send_message(
            activation_template(c, tx_hash), ephemeral=True
        )

    # /admin payment-address — Money-Gate: only after the customer acked policy (0.4)
    @admin_group.command(
        name="payment-address",
        description="Share the BEP-20 payment address ONLY after the customer acked the policy card.",
    )
    @app_commands.describe(user="The customer")
    async def admin_payment_address(
        self, inter: discord.Interaction, user: discord.Member
    ) -> None:
        if not await check_admin(inter):
            return
        from control_bot.policy import POLICY_VERSION
        uid = str(user.id)
        if not cm.get_customer(uid):
            await inter.response.send_message(
                f"❌ No customer record for `{user.display_name}`.", ephemeral=True
            )
            return
        if not cm.has_policy_ack(uid, POLICY_VERSION):
            await self._audit(
                f"⛔ Payment-address request DENIED for `{user.display_name}` (`{uid}`) "
                f"— policy not acknowledged (money-gate 0.4)."
            )
            await inter.response.send_message(
                f"🚫 **Money-Gate:** `{user.display_name}` has NOT acknowledged the "
                "Pre-Payment Policy Card. Pin it with `/admin pin-policy` and get the "
                "ack first (recorded in the DB).",
                ephemeral=True,
            )
            return
        import os as _os
        addr = _os.environ.get("PAYMENT_ADDRESS", "").strip()
        if not addr:
            await inter.response.send_message(
                "❌ PAYMENT_ADDRESS secret is not configured.", ephemeral=True
            )
            return
        try:
            await user.send(
                f"💰 **Payment address (BEP-20, USDT/BUSD only):**\n`{addr}`\n\n"
                "Send your payment, then paste the **TX hash** into this thread "
                "so we can verify it on BSCScan and activate your farm."
            )
        except Exception as exc:
            await inter.response.send_message(
                f"⚠️ Could not DM the customer: {exc}", ephemeral=True
            )
            return
        await self._audit(
            f"💰 Payment address shared with `{user.display_name}` (`{uid}`) — "
            f"policy ack verified (version {POLICY_VERSION})."
        )
        await inter.response.send_message(
            f"✅ Payment address DMed to **{user.display_name}** (policy ack verified).",
            ephemeral=True,
        )

    # /admin verify-tokens — write-proof + expiry health for all worker tokens
    @admin_group.command(
        name="verify-tokens",
        description="Prove write access and check expiry for every configured worker token.",
    )
    async def admin_verify_tokens(self, inter: discord.Interaction) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        from github_dispatch import list_worker_tokens, verify_github_token, check_token_status

        lines = ["🔐 **Worker token verification**"]
        tokens = list_worker_tokens()
        if not tokens:
            await inter.followup.send("❌ No worker tokens configured (WORKER_TOKENS / GH_ADMIN_TOKEN).", ephemeral=True)
            return
        for item in tokens:
            owner = item.get("owner") or "(default)"
            try:
                proof = await asyncio.to_thread(verify_github_token, owner, item["token"])
            except Exception as exc:
                lines.append(f"• ❌ `{owner}` — check threw: {exc}")
                continue
            if proof.get("ok"):
                status = await asyncio.to_thread(check_token_status, item["token"], owner)
                days = status.get("days_left")
                expiry = f" exp in {days:.1f}d" if days is not None else ""
                lines.append(f"• ✅ `{owner}` — write-proof OK (scratch create+delete){expiry}")
            else:
                lines.append(f"• ❌ `{owner}` — {proof.get('error', 'write-proof failed')}")
        await self._audit("🔐 Worker token verification run by " f"`{inter.user.display_name}`")
        await inter.followup.send("\n".join(lines), ephemeral=True)

    # /admin logs
    @admin_group.command(
        name="logs",
        description="View recent logs for a customer (links to their #farm-logs thread).",
    )
    @app_commands.describe(user="The customer")
    async def admin_logs(
        self, inter: discord.Interaction, user: discord.Member
    ) -> None:
        if not await check_admin(inter):
            return
        c = cm.get_customer(str(user.id))
        if not c:
            await inter.response.send_message(
                f"❌ No record found for `{user.display_name}`.", ephemeral=True
            )
            return
        thread_id = c.get("logs_thread_id", "")
        if thread_id and thread_id != "0":
            await inter.response.send_message(
                f"📋 Logs for **{user.display_name}**: <#{thread_id}>",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                f"📋 No logs thread found for **{user.display_name}**.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    """Called by bot.load_extension() to register this cog."""
    cog = AdminCog(bot)
    # Register audit log channel from env
    import os
    ch_id = int(os.environ.get("AUDIT_LOG_CH_ID", "0") or "0")
    cog._audit_ch_id = ch_id or None
    bot.tree.add_command(cog.admin_group)
    await bot.add_cog(cog)
