#!/usr/bin/env python3
"""AdFarm V9 — idempotent installer (REST only, --dry-run safe).

Installs the new control plane without touching the legacy code. It:

  1. creates the backup Gist (ADFARM_GIST_ID) if one is not already configured;
  2. uploads the workflow files (control_bot.yml + sender/*) to the core repo
     (only with --push-workflows, and never overwriting unless --force);
  3. sets the GitHub *repository* secrets for the core repo (sealed with the
     repo public key — same path the bot uses at runtime);
  4. initialises adfarm.db (migrations + empty tables);
  5. optionally (--discord, requires discord.py) provisions the **entire** Discord
     server — public channels, staff channels, the 🏢 Customer Hub category, a
     "Bot Admin" role and every permission overwrite — stores the resulting ids in the
     `meta` table (and as repo secrets) and registers the slash commands.
     Nothing has to be created by hand in Discord.

Everything is --dry-run safe: pass --dry-run to print the plan and exit.

    python setup.py --dry-run
    python setup.py --push-workflows --discord
    ADFARM_GIST_ID=xxx python setup.py --push-workflows

Environment (same names as the bot, see adfarm/config.py):
    GH_TOKEN            main-account PAT (repo, workflow, gist)
    CORE_REPO           owner/repo of this core repo (or GITHUB_REPOSITORY)
    BOT_TOKEN, OWNER_IDS, WORKER_TOKENS, CONTROL_GIST_ID, ADFARM_GIST_ID,
    TOKEN_VAULT_KEY, PAYMENT_ADDRESS, GUILD_ID, ...
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from adfarm.github.secrets import seal_secret  # noqa: E402
from adfarm.db import Database  # noqa: E402

API = "https://api.github.com"
TIMEOUT = 30


def _env(names: tuple[str, ...], default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "adfarm-setup/9",
    }


def _api(method: str, url: str, token: str, json_body: Optional[dict] = None) -> dict:
    resp = requests.request(method, url, headers=_headers(token), json=json_body, timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} → HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json() if resp.content else {}


# ─────────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────────
def ensure_backup_gist(token: str, gist_id: str, dry_run: bool) -> str:
    """Create the backup Gist once and return its id (existing id is kept)."""
    if gist_id:
        print(f"[gist] using configured ADFARM_GIST_ID={gist_id} (left untouched)")
        return gist_id
    if dry_run:
        print("[gist] would create backup Gist (ADFARM_GIST_ID) and print its id")
        return "<NEW-GIST-ID>"
    data = _api("POST", f"{API}/gists", token, {
        "description": "AdFarm V9 control-plane database backup",
        "public": False,
        "files": {"adfarm.db.b64": {"content": ""}, "db-meta.json": {"content": "{}"}},
    })
    gid = data.get("id", "")
    print(f"[gist] created backup Gist: {gid}  → set ADFARM_GIST_ID={gid} as a repo secret")
    return gid


def push_workflows(token: str, core_repo: str, *, force: bool, dry_run: bool) -> None:
    sources = [
        (Path("workflows/control_bot.yml"), f".github/workflows/control_bot.yml"),
        (Path("sender/workflows/send_ads.yml"), "sender/workflows/send_ads.yml"),
        (Path("sender/workflows/self_check.yml"), "sender/workflows/self_check.yml"),
        (Path("sender/send_ads.py"), "sender/send_ads.py"),
        (Path("sender/channel_registry.py"), "sender/channel_registry.py"),
    ]
    for local, repo_path in sources:
        if not local.exists():
            print(f"[workflows] skip missing {local}")
            continue
        content = local.read_bytes()
        if dry_run:
            print(f"[workflows] would upload {repo_path} ({len(content)} bytes)")
            continue
        existing = None
        try:
            resp = requests.get(f"{API}/repos/{core_repo}/contents/{repo_path}", headers=_headers(token), timeout=TIMEOUT)
            if resp.status_code == 200:
                existing = resp.json().get("sha")
        except requests.RequestException:
            existing = None
        body = {"message": f"adfarm: sync {repo_path}", "content": base64.b64encode(content).decode("ascii")}
        if existing:
            if not force:
                print(f"[workflows] {repo_path} exists — skipping (use --force to overwrite)")
                continue
            body["sha"] = existing
        _api("PUT", f"{API}/repos/{core_repo}/contents/{repo_path}", token, body)
        print(f"[workflows] uploaded {repo_path}")


def set_repo_secrets(token: str, core_repo: str, *, dry_run: bool, extra: Optional[dict[str, str]] = None) -> None:
    """Seal and upload the core-repo secrets the bot needs (idempotent)."""
    secrets = {
        "BOT_TOKEN": _env(("BOT_TOKEN",)),
        "OWNER_IDS": _env(("OWNER_IDS", "OWNER_ID")),
        "GUILD_ID": _env(("GUILD_ID",)),
        "GH_TOKEN": _env(("GH_TOKEN", "GH_ADMIN_TOKEN", "GITHUB_PAT")),
        "WORKER_TOKENS": _env(("WORKER_TOKENS",)),
        "ADMIN_ALERTS_CH_ID": _env(("ADMIN_ALERTS_CH_ID",)),
        "AUDIT_LOG_CH_ID": _env(("AUDIT_LOG_CH_ID", "AUDIT_LOGS_CH_ID")),
        "OPEN_TICKET_CH_ID": _env(("OPEN_TICKET_CH_ID", "TICKET_CH_ID")),
        "CUSTOMER_HUB_ID": _env(("CUSTOMER_HUB_ID",)),
        "PAYMENT_ADDRESS": _env(("PAYMENT_ADDRESS",)),
        "CONTROL_GIST_ID": _env(("CONTROL_GIST_ID",)),
        "ADFARM_GIST_ID": _env(("ADFARM_GIST_ID", "CUSTOMERS_GIST_ID")),
        "GIST_TOKEN": _env(("GIST_TOKEN", "GH_TOKEN", "GH_ADMIN_TOKEN", "GITHUB_PAT")),
        "TOKEN_VAULT_KEY": _env(("TOKEN_VAULT_KEY",)),
    }
    for name, value in (extra or {}).items():          # ids just provisioned in Discord
        if value and not secrets.get(name):
            secrets[name] = value
    secrets = {k: v for k, v in secrets.items() if v}

    if dry_run:
        print(f"[secrets] would set {len(secrets)} repo secret(s) on {core_repo}: {', '.join(secrets)}")
        return

    pub = _api("GET", f"{API}/repos/{core_repo}/actions/secrets/public-key", token)
    for name, value in secrets.items():
        sealed = seal_secret(pub["key"], value)
        _api("PUT", f"{API}/repos/{core_repo}/actions/secrets/{name}", token, {"encrypted_value": sealed, "key_id": pub["key_id"]})
        print(f"[secrets] set {name}")


def init_db(db_path: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[db] would initialise + migrate {db_path}")
        return
    db = Database(db_path)
    version = db.migrate()
    print(f"[db] adfarm.db ready at {db_path} (schema v{version})")


def discord_setup(guild_id: str, *, dry_run: bool, db_path: str, provision: bool = True, sync_commands: bool = True) -> dict[str, str]:
    """Provision the Discord server **and** register the slash commands.

    Creates (idempotently, nothing is ever duplicated or deleted):

      * public channels   #welcome-about #pricing-plans #whats-new #open-ticket #general-chat
      * staff channels    #admin-commands #admin-chat #audit-logs  (hidden from @everyone)
      * the ``🏢 Customer Hub`` category (hidden; per-customer forums land inside it)
      * a ``Bot Admin`` role, granted to every id in OWNER_IDS
      * all permission overwrites, re-applied on every run so a drifted server heals

    Every resulting id is written to the ``meta`` table of adfarm.db (survives restarts;
    ``Settings.with_channel_ids`` merges them at boot) and returned so the caller can also
    push them as repo secrets.
    """
    if dry_run:
        from adfarm.discord.provision import ALL_CHANNELS, HUB_CATEGORY_NAME
        names = ", ".join("#" + c.name for c in ALL_CHANNELS)
        print(f"[discord] would create category {HUB_CATEGORY_NAME}, the AdFarm/AdFarm Staff categories, the 'Bot Admin' role and: {names}")
        print("[discord] would apply permission overwrites, store the ids in the meta table and sync the slash commands")
        return {}

    try:
        import asyncio

        import discord
        from discord import app_commands

        from adfarm.app import build_services
        from adfarm.commands.registry import CommandRegistry
        from adfarm.config import Settings
        from adfarm.db import Database
        from adfarm.discord.adapter import DiscordPyAdapter, DiscordPyGuildAdmin
        from adfarm.discord.channels import ChannelClassifier
        from adfarm.discord.provision import GuildProvisioner
        from adfarm.services.container import Repos
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"[discord] skipped (discord.py not installed: {exc})")
        return {}

    settings = Settings.from_env()
    if not settings.bot_token:
        print("[discord] skipped: BOT_TOKEN is not set")
        return {}
    if guild_id and not settings.guild_id:
        settings = replace(settings, guild_id=guild_id)

    db = Database(db_path)
    db.migrate()
    meta = Repos.for_db(db).meta

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    out: dict[str, str] = {}

    @client.event
    async def on_ready() -> None:
        try:
            if provision:
                admin_api = DiscordPyGuildAdmin(client, settings.guild_id)
                provisioner = GuildProvisioner(admin_api, owner_ids=sorted(settings.owner_ids), store=meta.set)
                report = await provisioner.provision()
                out.update(report.ids)
                for name in report.created:
                    print(f"[discord] created {name}")
                for name in report.reused:
                    print(f"[discord] exists  {name} (skipped, permissions refreshed)")
                for name, err in report.failures:
                    print(f"[discord] FAILED  {name}: {err}")
                print(f"[discord] provisioning: {report.summary()}")
                for key, value in sorted(report.ids.items()):
                    print(f"[discord]   {key}={value}  (stored in {db_path} → meta)")
            if sync_commands:
                merged = Settings.from_env().with_channel_ids(meta.all())
                adapter = DiscordPyAdapter(client, merged.guild_id or settings.guild_id)
                services = build_services(merged, adapter, db=db)
                classifier = ChannelClassifier(services.settings, services.customers.by_forum)
                registry = CommandRegistry(tree, services, classifier, guild_id=services.settings.guild_id)
                registry.register_all()
                n = await registry.sync()
                print(f"[discord] synced {n} commands; guild={client.guilds[0].name if client.guilds else '?'}")
        finally:
            await client.close()

    try:
        asyncio.run(client.start(settings.bot_token))
    except Exception as exc:  # pragma: no cover - network
        print(f"[discord] setup failed (continuing): {exc}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="AdFarm V9 installer (REST only).")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    p.add_argument("--push-workflows", action="store_true", help="upload control_bot.yml + sender files to the core repo")
    p.add_argument("--force", action="store_true", help="overwrite existing workflow files")
    p.add_argument("--discord", action="store_true", help="provision the Discord server (channels, category, role, permissions) and register commands")
    p.add_argument("--discord-provision", action="store_true", help="provision the Discord server layout only (no slash-command sync)")
    p.add_argument("--no-provision", action="store_true", help="with --discord: only sync slash commands, do not touch the server layout")
    p.add_argument("--db", default=_env(("ADFARM_DB", "CUSTOMERS_DB"), "adfarm.db"), help="path to adfarm.db")
    p.add_argument("--core-repo", default=_env(("CORE_REPO", "GITHUB_REPOSITORY")), help="owner/repo of the core repo")
    args = p.parse_args(argv)

    token = _env(("GH_TOKEN", "GH_ADMIN_TOKEN", "GITHUB_PAT"))
    if not token:
        print("error: GH_TOKEN (main-account PAT) is required", file=sys.stderr)
        return 2
    if args.push_workflows and not args.core_repo:
        print("error: --push-workflows requires CORE_REPO (owner/repo)", file=sys.stderr)
        return 2

    print(f"AdFarm V9 setup — dry_run={args.dry_run}")
    do_discord = args.discord or args.discord_provision
    channel_ids: dict[str, str] = {}
    if do_discord:
        # provision first so the freshly created ids can be pushed as repo secrets below
        channel_ids = discord_setup(_env(("GUILD_ID",)), dry_run=args.dry_run, db_path=args.db,
                                    provision=not args.no_provision, sync_commands=not args.discord_provision)
    gist_id = ensure_backup_gist(token, _env(("ADFARM_GIST_ID", "CUSTOMERS_GIST_ID")), args.dry_run)
    if args.push_workflows:
        push_workflows(token, args.core_repo, force=args.force, dry_run=args.dry_run)
    if args.core_repo:
        set_repo_secrets(token, args.core_repo, dry_run=args.dry_run, extra=channel_ids)
    else:
        print("[secrets] skipped (no CORE_REPO configured)")
    init_db(args.db, dry_run=args.dry_run)
    print("Done. Remember to install workflows/control_bot.yml as .github/workflows/control_bot.yml "
          "and set ADFARM_REGISTER_COMMANDS=true on chunk 1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
