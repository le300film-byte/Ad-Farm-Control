"""admin_commands.py – V8 Admin-Only Slash Commands (Cog).

All commands in this cog require the invoking user to be in OWNER_IDS.
They are hidden from users who are not admins.

Commands:
  /admin list               – Show all active customers
  /admin activate @User days alts vip
                            – Onboard a new customer (reuses existing
                              repos + forum; never duplicates them)
  /admin extend @User days  – Extend a subscription
  /admin deactivate @User   – Shut down and lock a customer
  /admin shutdown all       – Emergency kill-switch for all customers
  /admin repos              – List every repo across all worker accounts
                              (customer, alt, status) — V8 bug-fix K
  /admin repo sync          – Push latest send_ads.py to all repos
  /admin repo delete        – Delete one customer repo (confirm: DELETE)
                              — V8 bug-fix L
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


def _remember_ticket_channel(channel) -> None:
    """Persist the ticket channel id for this process AND across restarts.

    V8 bug-fix (plan #2): the "❌ Ticket channel not configured." error fired
    because ``OPEN_TICKET_CH_ID`` only exists as a repository secret — it is
    read from the environment at runtime, and clicking the ticket panel button
    could never work when the secret/env was missing.  Whenever an admin
    explicitly points ``/admin ticket-panel`` (or ``/admin pin-policy``) at a
    channel we now:

    1. export it into ``os.environ`` so the current process resolves it, and
    2. store it in ``customers.db`` meta (``open_ticket_ch_id``) which is
       backed up to the private Gist and restored on every boot.
    """
    try:
        ch_id = str(int(channel.id))
    except Exception:
        return
    import os
    os.environ["OPEN_TICKET_CH_ID"] = ch_id
    try:
        cm.set_meta("open_ticket_ch_id", ch_id)
    except Exception as exc:
        print(f"[TICKET] Could not persist open_ticket_ch_id in DB: {exc}")


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
            # V8 bug-fix (plan #3): the token walkthrough video link was
            # removed entirely — no video will be produced. Text instructions
            # live in docs/SETUP_GUIDE.md §9.
            await user.send(
                f"🎉 **Welcome to AdFarm V8, {username}!**\n\n"
                "Your account has been activated. Run `/setup` in your "
                "`#control` thread to enter your alt tokens and channels. "
                "Once set up, use `/run` to start your ad farm.\n\n"
                "📖 Need help finding your alt token? See the step-by-step "
                "text guide in `docs/SETUP_GUIDE.md` (section 9) or open a "
                "ticket with the 🎫 button in `#open-ticket`."
            )
        except Exception:
            pass

    # ── /admin group ─────────────────────────────────────────────────────────

    admin_group = app_commands.Group(
        name="admin",
        description="V8 admin panel (owner-only)",
        # V8 bug-fix F: hide /admin from everyone who lacks the Administrator
        # permission in the guild (Discord-side visibility).  The OWNER_IDS
        # allow-list is still enforced in every subcommand via check_admin.
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
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

        existing = cm.get_customer(uid)

        # 1. GitHub repos — REUSE what the customer already has (bug-fix D/E):
        #    re-activating must never create duplicate repos or forums.
        repos: list[str] = []
        gh_account = github_account.strip().strip("/").strip()
        from github_dispatch import provision_alt_repo
        if existing and existing.get("repos"):
            repos = list(existing.get("repos") or [])
            if not gh_account:
                gh_account = (existing.get("github_account") or "").strip()
            if repos and not gh_account:
                # V8 bug-fix (plan #1): legacy records created before worker
                # round-robin stored an empty github_account, which later made
                # the timer engine call GitHub with `/repos//<name>` (404).
                # Discover which configured account actually holds the repo and
                # backfill it so cancels/syncs target the right owner.
                try:
                    from github_dispatch import discover_repo_owner
                    discovered = await asyncio.to_thread(discover_repo_owner, repos[0])
                except Exception:
                    discovered = ""
                if discovered:
                    gh_account = discovered
                    await inter.followup.send(
                        f"🧭 Recovered GitHub owner for existing repo "
                        f"`{repos[0]}` → `{gh_account}` (record backfilled).",
                        ephemeral=True,
                    )
            if repos:
                await inter.followup.send(
                    f"♻️ Reusing {len(repos)} existing repo(s) for **{uname}** — "
                    f"`{repos}`",
                    ephemeral=True,
                )
        if not repos and gh_account:
            # Explicit worker account: all alts live under it.
            from github_dispatch import get_workers
            known_workers = {w[0].strip().strip("/").lower() for w in get_workers() if w[0]}
            if known_workers and gh_account.lower() not in known_workers:
                await inter.followup.send(
                    f"⚠️ `{gh_account}` is not one of the configured worker "
                    f"accounts ({', '.join(sorted(known_workers))}). Repos will "
                    "only be created if GH_ADMIN_TOKEN has access there.",
                    ephemeral=True,
                )
            for i in range(1, alts + 1):
                repo_name = f"{uname.lower().replace(' ', '_')}_alt{i}"
                try:
                    html_url = await asyncio.to_thread(
                        provision_alt_repo, gh_account, repo_name
                    )
                    repos.append(repo_name)
                    await inter.followup.send(
                        f"✅ Created repo: [{repo_name}]({html_url}) on `{gh_account}`",
                        ephemeral=True,
                    )
                except Exception as exc:
                    await inter.followup.send(
                        f"⚠️ Repo `{repo_name}` creation failed: {exc}", ephemeral=True
                    )
        elif not repos:
            # Round-robin worker selection — but pick ONE worker for the whole
            # customer so every alt repo + the stored github_account stay
            # consistent (bug-fix C: never store an empty/ambiguous owner).
            from github_dispatch import pick_worker, _norm_owner
            worker_user, worker_token = pick_worker()
            gh_account = _norm_owner(worker_user)
            if not gh_account:
                # V8 bug-fix (plan #1): customer alt repos must NEVER be
                # created in the main account. An empty worker username means
                # no worker GitHub accounts are configured — fail loudly with
                # the exact remedy instead of silently provisioning under the
                # main token (which also stored an empty github_account and
                # broke later cancels/syncs).
                msg = (
                    "❌ **No worker GitHub accounts are configured** — refusing "
                    f"to create {alts} alt repo(s) for **{uname}** in the main "
                    "account.\n\n"
                    "**Fix:** set the `WORKER_TOKENS` repository secret "
                    "(`org1:token1,org2:token2`, fine-grained PATs per worker "
                    "account) or `WORKER_1_USER`/`WORKER_1_TOKEN` … "
                    "`WORKER_3_USER`/`WORKER_3_TOKEN`, then run this command "
                    "again. Alternatively pass `github_account:<worker-org>` "
                    "explicitly.\n"
                    "(`setup.py` writes these secrets automatically when you "
                    "register the 3 worker accounts.)"
                )
                await inter.followup.send(msg, ephemeral=True)
                await self._audit(
                    f"⛔ **Activation blocked** for `{uname}` (`{uid}`) — no "
                    "worker GitHub accounts configured (WORKER_TOKENS missing); "
                    "refused to create repos in the main account."
                )
                return
            for i in range(1, alts + 1):
                repo_name = f"{uname.lower().replace(' ', '_')}_alt{i}"
                try:
                    html_url = await asyncio.to_thread(
                        provision_alt_repo, worker_user, repo_name, worker_token
                    )
                    repos.append(repo_name)
                    await inter.followup.send(
                        f"✅ Created repo: [{repo_name}]({html_url}) on worker `{gh_account}`",
                        ephemeral=True,
                    )
                except Exception as exc:
                    await inter.followup.send(
                        f"⚠️ Repo `{repo_name}` creation failed on worker "
                        f"`{gh_account}`: {exc}", ephemeral=True
                    )

        # 2. Create/reuse the Discord forum (bug-fix E: never duplicated)
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
        if not forum_ids and existing:
            # No guild/forum available — keep any previously stored ids.
            forum_ids = {
                "forum_id": int(existing["forum_id"]) if str(existing.get("forum_id", "") or "").isdigit() else 0,
                "control_thread_id": int(existing["control_thread_id"]) if str(existing.get("control_thread_id", "") or "").isdigit() else 0,
                "dashboard_thread_id": int(existing["dashboard_thread_id"]) if str(existing.get("dashboard_thread_id", "") or "").isdigit() else 0,
                "logs_thread_id": int(existing["logs_thread_id"]) if str(existing.get("logs_thread_id", "") or "").isdigit() else 0,
                "dm_thread_id": int(existing["dm_thread_id"]) if str(existing.get("dm_thread_id", "") or "").isdigit() else 0,
                "deals_thread_id": int(existing["deals_thread_id"]) if str(existing.get("deals_thread_id", "") or "").isdigit() else 0,
            }

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
            f"• GitHub account: `{gh_account or '(unknown)'}`\n"
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

    # /admin repos — V8 bug-fix K: inventory across ALL worker accounts
    @admin_group.command(
        name="repos",
        description="List every repo across all worker accounts (customer, alt, status).",
    )
    async def admin_repos(self, inter: discord.Interaction) -> None:
        if not await check_admin(inter):
            return
        await inter.response.defer(ephemeral=True)
        from github_dispatch import list_all_repos
        customers = cm.list_customers(active_only=False)
        entries = await asyncio.to_thread(list_all_repos, customers)
        if not entries:
            await inter.followup.send("📭 No repos found on any worker account.", ephemeral=True)
            return
        lines = ["```", f"{'REPO':<38} {'OWNER':<20} {'CUSTOMER':<18} {'ALT':>3}  STATUS"]
        lines.append("-" * 88)
        for e in sorted(entries, key=lambda x: (x.get("owner", ""), x.get("repo", ""))):
            repo = e.get("repo", "") or f"(listing failed: {e.get('error', '')})"
            status = str(e.get("status", "?")).upper()
            lines.append(
                f"{repo:<38} {str(e.get('owner', ''))[:19]:<20} "
                f"{str(e.get('customer', ''))[:17]:<18} {e.get('alt', 0):>3}  {status}"
            )
        lines.append("```")
        await self._audit(
            f"📦 **Repo inventory** viewed by `{inter.user.display_name}` — "
            f"{len(entries)} repo(s)."
        )
        await inter.followup.send(
            f"📦 **{len(entries)}** repo(s) across worker accounts:\n" + "\n".join(lines),
            ephemeral=True,
        )

    # /admin repo sync|delete — V8 bug-fix L: delete a repo from Discord with
    # an explicit `DELETE` confirmation (accident guard).
    @admin_group.command(
        name="repo",
        description="Repo actions: sync sender to all repos, or delete one repo (confirm:DELETE).",
    )
    @app_commands.describe(
        action="Action to perform (sync | delete)",
        repo_name="Repo to delete (required when action=delete)",
        confirmation="Type DELETE to confirm deleting the repo",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="🔄 Sync — push latest sender to all repos", value="sync"),
        app_commands.Choice(name="🗑️ Delete — permanently delete one customer repo", value="delete"),
    ])
    async def admin_repo(
        self,
        inter: discord.Interaction,
        action: str = "sync",
        repo_name: str = "",
        confirmation: str = "",
    ) -> None:
        if not await check_admin(inter):
            return
        action = (action or "sync").lower()
        if action not in ("sync", "delete"):
            await inter.response.send_message(
                "ℹ️ Available actions: `sync`, `delete`", ephemeral=True
            )
            return

        if action == "delete":
            # V8 bug-fix L — safety: the operator must type DELETE.
            repo_name = (repo_name or "").strip().strip("/").strip()
            if not repo_name:
                await inter.response.send_message(
                    "❌ Pass the repo to delete: `/admin repo action:delete repo_name:<name> "
                    "confirmation:DELETE`",
                    ephemeral=True,
                )
                return
            if (confirmation or "").strip().upper() != "DELETE":
                await inter.response.send_message(
                    f"⚠️ **Deleting `{repo_name}` is irreversible.** Type "
                    "`confirmation:DELETE` to confirm.",
                    ephemeral=True,
                )
                return
            await inter.response.defer(ephemeral=True)
            customers = cm.list_customers(active_only=False)
            from github_dispatch import delete_customer_repo
            res = await asyncio.to_thread(delete_customer_repo, repo_name, customers)
            if not res.get("ok"):
                await inter.followup.send(
                    f"❌ Repo `{repo_name}` could not be deleted: {res.get('error', 'unknown error')}",
                    ephemeral=True,
                )
                return
            owner = res.get("owner", "")
            # Keep the customer record in sync (remove the repo from repos[]).
            removed_from = ""
            for c in customers:
                repos = [r for r in (c.get("repos") or []) if r.strip().strip("/") != repo_name]
                if len(repos) != len(c.get("repos") or []):
                    cm.update_repos(c["discord_id"], repos)
                    removed_from = c.get("discord_username", c["discord_id"])
                    break
            await self._audit(
                f"🗑️ **Repo deleted** `{owner}/{repo_name}` by "
                f"`{inter.user.display_name}` (confirmation: DELETE)"
            )
            await inter.followup.send(
                f"🗑️ **Deleted `{owner}/{repo_name}`.**"
                + (f"\nRemoved from customer **{removed_from}**'s record." if removed_from else ""),
                ephemeral=True,
            )
            return

        # action == sync (existing behaviour)
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
        _remember_ticket_channel(ch)
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
        # V8 bug-fix (plan #2): remember this channel as THE ticket channel so
        # clicking "Open a Ticket" can create threads even when the
        # OPEN_TICKET_CH_ID workflow secret was never set. Persisted in the
        # environment (this process) and in customers.db meta (survives
        # restarts/chunk handoffs via the Gist backup).
        _remember_ticket_channel(ch)
        await inter.response.send_message(
            f"✅ Ticket panel posted in {ch.mention} — ticket channel saved "
            f"(id `{ch.id}`). New tickets will open threads here.",
            ephemeral=True,
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
