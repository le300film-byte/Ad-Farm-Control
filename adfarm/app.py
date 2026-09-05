"""Composition root: Settings → Database → Services → discord.py bot → scheduler.

``build_services`` is also used by tests (with fakes) and by ``tools/``; ``main`` is the
production entry point (``python -m adfarm``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any, Optional

from .config import Settings
from .core.clock import Clock, SystemClock
from .db import Database, GistBackup, TokenVault
from .discord.forums import ForumProvisioner
from .discord.ports import DiscordPort
from .github import ControlQueue, GitHubClient, RepoProvisioner, WorkerPool, WorkflowDispatcher
from .security.guards import Guard, MultiSig
from .services import AlertService, AltService, BanService, CustomerService, Repos, RunService, Services, TicketService
from .telemetry import FleetState, IncomingMessage, WebhookIngestor
from .telemetry.heartbeat import EmbedLike
from .timers import ExpiryEngine, LimitlessRenewer, Scheduler

log = logging.getLogger("adfarm")


def build_services(settings: Settings, discord: DiscordPort, *, clock: Clock | None = None, github: GitHubClient | None = None,
                   token_checker=None, db: Database | None = None) -> Services:
    clock = clock or SystemClock()
    db = db or Database(settings.db_path)
    db.migrate()
    repos = Repos.for_db(db)
    # ids provisioned by `python setup.py --discord` are stored in the meta table; env wins
    settings = settings.with_channel_ids(repos.meta.all())
    vault = TokenVault(settings.token_vault_key)
    github = github or GitHubClient(settings.github_token, timeout=settings.http_timeout)
    gist_client = github if not settings.gist_token or settings.gist_token == settings.github_token else github.with_token(settings.gist_token)
    backup = GistBackup(db, gist_client if settings.backup_gist_id else None, settings.backup_gist_id, run_id=settings.run_id, clock=clock, lease_ttl=settings.lease_ttl)
    backup.attach()
    main_login = settings.core_repo.split("/", 1)[0] if "/" in settings.core_repo else ""
    workers = WorkerPool(settings.workers, github, repos.meta, main_login=main_login, main_client=github)
    provisioner = RepoProvisioner(workers)
    dispatcher = WorkflowDispatcher(workers, workflow_file=settings.workflow_file, clock=clock)
    queue = ControlQueue(gist_client if settings.control_gist_id else None, settings.control_gist_id, clock=clock)
    fleet = FleetState(clock=clock, offline_after=settings.offline_after)
    guard = Guard(settings.owner_ids, repos.customers.get, clock=clock)
    multisig = MultiSig(window=settings.multisig_window, clock=clock)
    s = Services(settings=settings, clock=clock, db=db, repos=repos, vault=vault, backup=backup, discord=discord, workers=workers, provisioner=provisioner,
                 dispatcher=dispatcher, queue=queue, fleet=fleet, guard=guard, multisig=multisig)
    s.alerts = AlertService(discord, repos.events, clock=clock, alerts_channel_id=settings.admin_alerts_channel_id, audit_channel_id=settings.audit_log_channel_id)
    forums = ForumProvisioner(discord, category_id=settings.customer_hub_category_id)
    s.customers = CustomerService(s, forums)
    s.alts = AltService(s, token_checker=token_checker)
    s.runs = RunService(s)
    s.tickets = TicketService(s)
    s.bans = BanService(s)
    rehydrate(s)
    return s


def rehydrate(s: Services) -> int:
    """(Re)build the in-memory fleet map from the DB. Called after build and again after a Gist restore
    (the restored snapshot may carry a different set of alts and an older schema)."""
    s.db.migrate()
    count = 0
    for alt in s.repos.alts.all():
        s.fleet.register((alt.customer_id, alt.alt_index), alt.sender_alt_id)
        count += 1
    return count


def build_ingestor(s: Services) -> WebhookIngestor:
    return WebhookIngestor(s.fleet, s.customers.by_thread, s.repos.alts.for_customer)


def register_jobs(s: Services, scheduler: Scheduler, *, discord_send=None) -> None:
    """Attach the periodic jobs (pure engines + service callbacks)."""
    from .discord.embeds import reminder_embed

    expiry = ExpiryEngine(s.repos.customers, s.repos.reminders)
    renewer = LimitlessRenewer(s.repos.runs, s.repos.customers)

    async def expiry_job() -> None:
        plan = expiry.plan(s.now())
        if plan.empty:
            return
        pending: list[Any] = []

        def on_reminder(rem) -> None:
            pending.append(("reminder", rem))

        def on_expired(customer) -> None:
            pending.append(("expired", customer))

        expiry.apply(plan, now=s.now(), on_reminder=on_reminder, on_expired=on_expired)
        for kind, item in pending:
            if kind == "reminder":
                embed = reminder_embed(item.days_left, item.customer.expiry_date, s.settings.payment_address)
                await s.discord.dm(item.customer.discord_id, "", embed=embed)
                if item.customer.thread("control"):
                    await s.discord.send(item.customer.thread("control"), f"⏰ <@{item.customer.discord_id}> your plan expires in {item.days_left:.0f} day(s). Use `/renew`.", embed=embed)
                s.alerts.event(item.customer.discord_id, "reminder_sent", threshold=item.threshold_days)
            else:
                await s.customers.deactivate(item.discord_id, reason="expired", actor_id="system")

    async def renewal_job() -> None:
        plan = renewer.plan(s.now())
        for run in plan.due:
            try:
                await s.runs.renew(run)
            except Exception as exc:
                await s.alerts.admin(f"renew:{run.customer_id}:{run.alt_index}", f"Limitless renewal failed for {run.customer_id}#{run.alt_index}: {exc}")
        for run in plan.orphaned:
            alt = s.repos.alts.get(run.customer_id, run.alt_index)
            if alt:
                await s.runs.stop(alt, reason="subscription inactive", actor_id="system", quiet=True)

    async def stale_job() -> None:
        for key in s.fleet.mark_stale():
            customer = s.repos.customers.get(key[0])
            if customer and customer.thread("control"):
                await s.discord.send(customer.thread("control"), f"⚫ Alt {key[1]} went silent (no heartbeat for {s.settings.offline_after // 60} min). Check `/alt action:runs`.")

    async def lease_job() -> None:
        await asyncio.to_thread(s.backup.renew_lease)

    scheduler.add("expiry", s.settings.expiry_scan_interval, expiry_job, run_immediately=True)
    scheduler.add("renewal", s.settings.renewal_scan_interval, renewal_job, run_immediately=True)
    scheduler.add("poll-runs", s.settings.github_poll_interval, s.runs.poll_runs)
    scheduler.add("sweep-alts", s.settings.sync_sweep_interval, s.alts.sweep_dirty)
    scheduler.add("stale", 60, stale_job)
    scheduler.add("lease", s.settings.lease_renew_interval, lease_job)


# ═════════════════════════════════════════════════════════════════════════════
def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    problems = settings.problems()
    for p in problems:
        log.error("config: %s", p)
    if not settings.bot_token or not settings.owner_ids:
        log.error("refusing to start without BOT_TOKEN and OWNER_IDS")
        return 2

    import discord

    from .commands.registry import CommandRegistry
    from .discord.adapter import DiscordPyAdapter
    from .discord.channels import ChannelClassifier

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)
    adapter = DiscordPyAdapter(client, settings.guild_id)
    services = build_services(settings, adapter)
    settings = services.settings   # may now carry channel ids provisioned into the meta table
    scheduler = Scheduler(on_error=lambda name, exc: services.alerts.admin(f"job:{name}", f"Job {name} failed: {type(exc).__name__}: {exc}"))
    ingestor = build_ingestor(services)
    classifier = ChannelClassifier(settings, services.customers.by_forum)
    registry = CommandRegistry(tree, services, classifier, guild_id=settings.guild_id)
    registry.register_all()
    state: dict[str, Any] = {"ready": False}

    @client.event
    async def on_ready() -> None:
        if state["ready"]:
            return
        state["ready"] = True
        log.info("logged in as %s (guilds=%d)", client.user, len(client.guilds))
        restored = await asyncio.to_thread(services.backup.restore_if_missing)
        log.info("database source: %s (%s)", restored, settings.db_path)
        if restored not in ("local", "disabled"):
            log.info("fleet rehydrated: %d alt(s)", await asyncio.to_thread(rehydrate, services))
        if not await asyncio.to_thread(services.backup.acquire_lease):
            log.error("another runner holds the DB lease (%s) — exiting to avoid a split brain", services.backup.lease_holder)
            await client.close()
            return
        services.backup.start()
        if settings.register_commands:
            try:
                log.info("synced %d commands", await registry.sync())
            except Exception as exc:
                log.error("command sync failed: %s", exc)
        flag = services.repos.meta.get("shutdown_requested")
        if flag:
            # informative only: a previous runner was stopped by /admin shutdown-bot; a stuck flag must never brick the bot
            services.repos.meta.delete("shutdown_requested")
            await services.alerts.admin("shutdown-flag", f"Previous runner was stopped via /admin shutdown-bot (at unix {flag}). Starting normally; flag cleared.", force=True)
        register_jobs(services, scheduler)
        await scheduler.start()
        await services.alerts.admin("boot", f"AdFarm control bot online (run {settings.run_id}); config problems: {len(problems)}", force=True)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.id == getattr(client.user, "id", 0):
            return
        if not message.webhook_id:
            return
        embeds = [EmbedLike(title=e.title or "", description=e.description or "", footer=(e.footer.text if e.footer else "") or "",
                            fields=[(f.name or "", f.value or "") for f in e.fields]) for e in message.embeds]
        result = ingestor.ingest(IncomingMessage(channel_id=str(message.channel.id), author_name=message.author.name, content=message.content or "", embeds=embeds,
                                                 is_webhook=True, message_id=str(message.id)))
        if result.key is None:
            return
        customer_id, alt_index = result.key
        if result.ban_detected:
            alt = services.repos.alts.get(customer_id, alt_index)
            if alt:
                await services.bans.handle(alt, reason=(message.content or "heartbeat error")[:200])
        if result.kind == "dm" and result.dm_author_id:
            customer = services.repos.customers.get(customer_id)
            if customer and customer.vip and customer.autoreply_text and services.fleet.should_autoreply(result.key, result.dm_author_id, settings.autoreply_cooldown):
                alt = services.repos.alts.get(customer_id, alt_index)
                if alt:
                    try:
                        await services.runs.reply(alt, result.dm_author_id, customer.autoreply_text, actor_id="autoreply")
                    except Exception as exc:
                        log.warning("autoreply failed: %s", exc)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        async def watch_shutdown() -> None:
            while not stop_event.is_set():
                if services.shutdown_requested:
                    stop_event.set()
                    break
                await asyncio.sleep(5)

        bot_task = asyncio.create_task(client.start(settings.bot_token))
        watcher = asyncio.create_task(watch_shutdown())
        stopper = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait({bot_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        watcher.cancel()
        await scheduler.stop()
        services.backup.stop(flush=True)
        await asyncio.to_thread(services.backup.release_lease)
        if not client.is_closed():
            await client.close()
        if bot_task in done and bot_task.exception():
            raise bot_task.exception()  # type: ignore[misc]

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
