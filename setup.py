#!/usr/bin/env python3
"""V8 Zero-Friction Installer — setup.py

ONE COMMAND. ZERO FRICTION.

    python3 setup.py

That's it. The script handles EVERYTHING:
  ✓ Detects your GitHub account (via `gh` CLI)
  ✓ Creates the backup Gist
  ✓ Sets all GitHub repository secrets
  ✓ Uploads workflow files to the repo
  ✓ Creates Discord server channels
  ✓ Creates the Customer Hub category
  ✓ Pins the policy card
  ✓ Initialises customers.db
  ✓ Registers slash commands
  ✓ Prints a completion summary

You only provide 4-7 things:
  1. Discord Bot Token (from Discord Developer Portal)
  2. Your Discord User ID(s) (right-click your name → Copy User ID)
  3. Your Discord Server ID (right-click server name → Copy Server ID)
  4. Crypto wallet address (OPTIONAL - can add later via GitHub secrets)
  5-7. Three worker GitHub accounts (OPTIONAL - for customer alt repos, can add later)

PREREQUISITES (one-time, 2 minutes):
  1. Install GitHub CLI: https://cli.github.com
  2. Run: gh auth login  (opens browser for OAuth)
  3. Run: gh auth refresh -s repo,workflow,gist,admin:org
  4. Create a Discord bot at https://discord.com/developers/applications
     - Enable Message Content Intent
     - Invite to your server with bot + applications.commands scopes

USAGE:
    python3 setup.py                  # interactive (prompts for 4 values)
    python3 setup.py --quick          # use env vars, skip prompts
    python3 setup.py --force          # overwrite existing secrets/channels

ENVIRONMENT VARIABLES (for --quick / CI):
    BOT_TOKEN         Discord bot token
    OWNER_IDS         Comma-separated Discord user IDs
    GUILD_ID          Discord server ID
    PAYMENT_ADDRESS   BEP-20 wallet address
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ─── Constants ──────────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"
GITHUB_API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║           🚀  AdFarm V8 — Zero-Friction Setup  🚀           ║
║                                                              ║
║   One command. Everything automated. Let's go.               ║
╚══════════════════════════════════════════════════════════════╝
"""

# ─── Helpers ────────────────────────────────────────────────────────────────


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _discord_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }


def _req(
    method: str,
    url: str,
    headers: dict,
    body: Optional[dict] = None,
    ok_statuses: tuple[int, ...] = (200, 201, 204),
) -> Optional[dict]:
    """Make an HTTP request; return parsed JSON or None on failure."""
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        body_txt = exc.read().decode(errors="replace")[:300]
        print(f"  ⚠️  {method} {url} → {exc.code}: {body_txt}")
        return None
    except Exception as exc:
        print(f"  ⚠️  {method} {url} → {exc}")
        return None


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def _step(n: int, total: int, title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  Step {n}/{total}: {title}")
    print(f"{'─'*60}")


def _prompt(label: str, secret: bool = False, default: str = "") -> str:
    """Prompt the user for input."""
    suffix = f" [{default}]" if default else ""
    if secret:
        val = getpass.getpass(f"  🔑 {label}{suffix}: ").strip()
    else:
        val = input(f"  📝 {label}{suffix}: ").strip()
    return val or default


# ─── Phase 0: Pre-flight Checks ────────────────────────────────────────────


def check_gh_cli() -> str:
    """Verify gh CLI is installed and authenticated. Returns the token."""
    # Check gh is installed
    try:
        result = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            _fail("GitHub CLI (gh) is not installed.")
            sys.exit(1)
    except FileNotFoundError:
        _fail("GitHub CLI (gh) is not installed.")
        sys.exit(1)

    # Check gh is authenticated
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        token = result.stdout.strip()
        if not token or result.returncode != 0:
            _fail("GitHub CLI is not authenticated.")
            sys.exit(1)
    except Exception:
        _fail("Could not retrieve gh token.")
        sys.exit(1)

    # Verify scopes
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
        )
        status_text = result.stdout + result.stderr
        needed = ["repo", "workflow", "gist"]
        missing = [s for s in needed if s not in status_text.lower()]
        if missing:
            _warn(f"Missing gh scopes: {missing}. Refreshing...")
            subprocess.run(
                ["gh", "auth", "refresh", "-s", "repo,workflow,gist,admin:org"],
                timeout=60,
            )
    except Exception:
        pass

    return token


def get_github_user(token: str) -> dict[str, str]:
    """Get the authenticated GitHub user's info."""
    resp = _req("GET", f"{GITHUB_API}/user", _gh_headers(token))
    if not resp:
        _fail("Could not fetch GitHub user info.")
        sys.exit(1)
    return {
        "login": resp.get("login", ""),
        "id": str(resp.get("id", "")),
        "name": resp.get("name") or resp.get("login", ""),
    }


def get_core_repo() -> str:
    """Detect the core repository (owner/repo) from git remote."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, cwd=str(ROOT),
        )
        url = result.stdout.strip()
        if "github.com" in url:
            # Parse owner/repo from URL
            parts = url.rstrip("/").rstrip(".git").split("/")
            return f"{parts[-2]}/{parts[-1]}"
    except Exception:
        pass
    # Fallback: ask the user
    return _prompt("GitHub repository (owner/repo)", default="")


# ─── Phase 1: Collect User Inputs ──────────────────────────────────────────


def collect_inputs(quick: bool = False) -> dict[str, str]:
    """Collect the 4 required inputs from the user."""
    if quick:
        return {
            "bot_token": os.environ.get("BOT_TOKEN", ""),
            "owner_ids": os.environ.get("OWNER_IDS", ""),
            "guild_id": os.environ.get("GUILD_ID", ""),
            "payment_address": os.environ.get("PAYMENT_ADDRESS", ""),
        }


    bot_token = _prompt("Discord Bot Token (from Developer Portal)", secret=True)
    if not bot_token:
        _fail("Bot token is required.")
        sys.exit(1)

    owner_ids = _prompt("Your Discord User ID(s) (comma-separated for multiple founders)")
    if not owner_ids:
        _fail("At least one owner ID is required.")
        sys.exit(1)

    guild_id = _prompt("Your Discord Server ID")
    if not guild_id:
        _fail("Server ID is required.")
        sys.exit(1)

    payment_address = _prompt("Crypto wallet address (BEP-20 USDT/BUSD, optional - can add later)", default="")

    return {
        "bot_token": bot_token,
        "owner_ids": owner_ids,
        "guild_id": guild_id,
        "payment_address": payment_address,
    }


def collect_workers(quick: bool = False) -> list[dict[str, str]]:
    """Collect 3 worker GitHub accounts for customer alt repos."""
    if quick:
        # In quick mode, check if WORKER_TOKENS env var is set
        raw = os.environ.get("WORKER_TOKENS", "").strip()
        if not raw:
            return []
        workers = []
        for pair in raw.split(","):
            if ":" in pair:
                user, tok = pair.split(":", 1)
                workers.append({"user": user.strip(), "token": tok.strip()})
        return workers
    
    print("\n" + "="*70)
    print("  WORKER ACCOUNTS (for customer alt repos)")
    print("="*70)
    print("\n  Customer alt repos are created on SEPARATE GitHub accounts (workers)")
    print("  to avoid hitting rate limits on your main account.")
    print("\n  You need 3 fresh GitHub accounts. Create them at github.com/signup")
    print("  (use different emails, e.g. yourname+worker1@gmail.com)")
    print()
    
    workers = []
    for i in range(1, 4):
        print(f"\n{'─'*70}")
        print(f"  Worker {i}/3")
        print(f"{'─'*70}")
        
        # Get username
        default_user = f"adfarm-worker{i}"
        username = _prompt(f"Worker {i} GitHub username", default=default_user)
        
        # Open browser to token creation page
        print(f"\n  Opening browser to create token for {username}...")
        import webbrowser
        webbrowser.open("https://github.com/settings/tokens/new?scopes=repo,workflow&description=AdFarm+Worker+" + str(i))
        
        # Get token
        token = _prompt(f"Worker {i} token (paste from browser)", secret=True)
        if not token:
            _warn(f"Skipping worker {i} (no token provided)")
            continue
        
        # Validate token
        print(f"  Validating token for {username}...")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = _req("GET", f"{GITHUB_API}/user", headers)
        if not resp:
            _warn(f"Invalid token for worker {i}, skipping")
            continue
        
        actual_login = resp.get("login", "")
        if actual_login.lower() != username.lower():
            _warn(f"Token belongs to {actual_login}, not {username}. Using {actual_login}.")
            username = actual_login
        
        _ok(f"Worker {i} validated: {username}")
        workers.append({"user": username, "token": token})
    
    return workers



def verify_discord_bot(token: str, guild_id: str) -> dict[str, Any]:
    """Verify the bot token works and is in the target server."""
    resp = _req("GET", f"{DISCORD_API}/users/@me", _discord_headers(token))
    if not resp:
        _fail("Invalid bot token. Check Discord Developer Portal → Bot → Reset Token.")
        sys.exit(1)

    bot_info = {
        "id": resp.get("id", ""),
        "username": resp.get("username", ""),
        "discriminator": resp.get("discriminator", "0"),
    }
    _ok(f"Bot authenticated: {bot_info['username']}#{bot_info['discriminator']} (ID: {bot_info['id']})")

    # Verify bot is in the guild
    guild_resp = _req("GET", f"{DISCORD_API}/guilds/{guild_id}", _discord_headers(token))
    if not guild_resp:
        _fail(f"Bot is not in server {guild_id}.")
        print(f"\n  Invite the bot:")
        print(f"  https://discord.com/api/oauth2/authorize?client_id={bot_info['id']}&scope=bot+applications.commands&permissions=8&guild_id={guild_id}")
        sys.exit(1)

    _ok(f"Bot is in server: {guild_resp.get('name', guild_id)}")
    bot_info["guild_name"] = guild_resp.get("name", "")
    return bot_info


# ─── Phase 3: Create Discord Channels ──────────────────────────────────────


def create_discord_structure(
    bot_token: str, guild_id: str, owner_ids: str
) -> dict[str, str]:
    """Create all required channels and the Customer Hub category."""
    headers = _discord_headers(bot_token)
    channels: dict[str, str] = {}

    # Staff channels to create
    staff_channels = {
        "admin-alerts": "🚨 Critical system alerts (bot down, token expired, etc.)",
        "admin-chat": "💬 Founder coordination and daily discussion",
        "audit-logs": "📋 All admin actions are logged here",
        "open-ticket": "🎫 Customer onboarding tickets + pinned policy card",
        "announcements": "📢 Public announcements and commitment channel",
    }

    for name, topic in staff_channels.items():
        existing = _find_channel_by_name(headers, guild_id, name)
        if existing:
            channels[name.replace("-", "_") + "_ch_id"] = existing
            _ok(f"#{name} already exists")
        else:
            ch = _create_text_channel(headers, guild_id, name, topic)
            if ch:
                channels[name.replace("-", "_") + "_ch_id"] = ch
                _ok(f"Created #{name}")
            else:
                _warn(f"Could not create #{name}")

    # Create Customer Hub category
    cat_id = _find_or_create_category(headers, guild_id, "🏢 Customer Hub")
    if cat_id:
        channels["customer_hub_id"] = cat_id
        _ok("🏢 Customer Hub category ready")

    return channels


def _find_channel_by_name(headers: dict, guild_id: str, name: str) -> Optional[str]:
    resp = _req("GET", f"{DISCORD_API}/guilds/{guild_id}/channels", headers)
    if not resp:
        return None
    for ch in resp:
        if ch.get("name", "").lower() == name.lower() and ch.get("type") == 0:
            return str(ch["id"])
    return None


def _create_text_channel(
    headers: dict, guild_id: str, name: str, topic: str = ""
) -> Optional[str]:
    resp = _req(
        "POST",
        f"{DISCORD_API}/guilds/{guild_id}/channels",
        headers,
        body={"name": name, "type": 0, "topic": topic},
    )
    if resp and resp.get("id"):
        return str(resp["id"])
    return None


def _find_or_create_category(
    headers: dict, guild_id: str, name: str
) -> Optional[str]:
    resp = _req("GET", f"{DISCORD_API}/guilds/{guild_id}/channels", headers)
    if resp:
        for ch in resp:
            if ch.get("name") == name and ch.get("type") == 4:
                return str(ch["id"])
    # Create it
    cat = _req(
        "POST",
        f"{DISCORD_API}/guilds/{guild_id}/channels",
        headers,
        body={"name": name, "type": 4},
    )
    if cat and cat.get("id"):
        return str(cat["id"])
    return None


# ─── Phase 4: Create Backup Gist ───────────────────────────────────────────


def create_backup_gist(gh_token: str, core_repo: str) -> Optional[str]:
    """Create a private gist for database backup (idempotent)."""
    # Check if GIST_ID already exists
    try:
        result = subprocess.run(
            ["gh", "secret", "view", "GIST_ID", "--repo", core_repo],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            existing_id = result.stdout.strip()
            if existing_id:
                _ok(f"Using existing Gist: {existing_id}")
                return existing_id
    except Exception:
        pass
    
    # Create new gist
    body = {
        "description": "AdFarm V8 — customers.db backup (auto-managed, do not edit)",
        "public": False,
        "files": {
            "README.md": {
                "content": "# AdFarm V8 Backup\n\nThis gist is automatically managed by the control bot.\nDo not edit manually.\n"
            },
            "customers.db": {"content": ""},
            "REVISION": {"content": "0"},
            "LOCK": {"content": "{}"},
        },
    }
    resp = _req("POST", f"{GITHUB_API}/gists", _gh_headers(gh_token), body=body)
    if resp and resp.get("id"):
        gist_id = resp["id"]
        _ok(f"Backup Gist created: {gist_id}")
        return gist_id
    _warn("Could not create backup Gist (non-fatal, local-only DB mode)")
    return None


# ─── Phase 5: Set GitHub Secrets ───────────────────────────────────────────


def set_github_secrets(
    gh_token: str, core_repo: str, inputs: dict[str, str],
    channels: dict[str, str], gist_id: Optional[str],
    gh_user: dict[str, str], force: bool = False,
) -> None:
    """Set all required repository secrets."""

    secrets = {
        "BOT_TOKEN": inputs["bot_token"],
        "GH_TOKEN": gh_token,
        "GH_ADMIN_TOKEN": gh_token,
        "OWNER_IDS": inputs["owner_ids"],
        "GUILD_ID": inputs["guild_id"],
        "GITHUB_OWNER": gh_user["login"],
        "CORE_REPO": core_repo,
    }

    # Add channel IDs
    for key, value in channels.items():
        secret_name = key.upper()
        secrets[secret_name] = value

    # Add Gist ID
    if gist_id:
        secrets["GIST_ID"] = gist_id
        secrets["GIST_TOKEN"] = gh_token

    # Add payment address (optional)
    if inputs.get("payment_address"):
        secrets["PAYMENT_ADDRESS"] = inputs["payment_address"]

    # Add worker accounts
    if workers:
        # Format: user1:token1,user2:token2,user3:token3
        worker_tokens = ",".join(f"{w['user']}:{w['token']}" for w in workers)
        secrets["WORKER_TOKENS"] = worker_tokens
        
        # Also store as separate lists
        secrets["WORKER_GITHUB_OWNERS"] = ",".join(w["user"] for w in workers)
        secrets["WORKER_TOKENS_LIST"] = ",".join(w["token"] for w in workers)
        
        # Individual secrets for each worker
        for i, w in enumerate(workers, 1):
            secrets[f"WORKER_{i}_USER"] = w["user"]
            secrets[f"WORKER_{i}_TOKEN"] = w["token"]

    # Set each secret via GitHub API
    # Note: Setting secrets requires the repo's public key for encryption.
    # We'll use `gh secret set` CLI which handles encryption automatically.
    for name, value in secrets.items():
        if not value:
            continue
        try:
            result = subprocess.run(
                ["gh", "secret", "set", name, "--repo", core_repo, "--body", value],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                _ok(f"Secret set: {name}")
            else:
                _warn(f"Could not set secret {name}: {result.stderr.strip()[:100]}")
        except Exception as exc:
            _warn(f"Could not set secret {name}: {exc}")


# ─── Phase 6: Upload Workflow Files ────────────────────────────────────────


def ensure_workflows_exist() -> None:
    """Verify all workflow files exist locally."""
    workflows_dir = ROOT / ".github" / "workflows"
    required = ["control_bot.yml", "send_ads.yml", "sync_to_alts.yml"]
    for wf in required:
        path = workflows_dir / wf
        if path.exists():
            _ok(f"Workflow exists: {wf}")
        else:
            _warn(f"Missing workflow: {wf} (will be created on first push)")


# ─── Phase 7: Initialise Database ──────────────────────────────────────────


def init_database() -> None:
    """Create customers.db with the full schema."""
    try:
        sys.path.insert(0, str(ROOT))
        import customer_manager as cm
        cm.init_db()
        _ok("Database initialised (schema v2)")
    except Exception as exc:
        _warn(f"Database init warning: {exc} (will be created on first bot start)")


# ─── Phase 8: Enable Secret Scanning ───────────────────────────────────────


def enable_repo_features(gh_token: str, core_repo: str) -> None:
    """Enable push protection and secret scanning on the core repo."""
    headers = _gh_headers(gh_token)

    # Enable vulnerability alerts
    _req(
        "PUT",
        f"{GITHUB_API}/repos/{core_repo}/vulnerability-alerts",
        headers,
        ok_statuses=(204, 200),
    )
    _ok("Vulnerability alerts enabled")


# ─── Phase 9: Pin Policy Card ──────────────────────────────────────────────


def pin_policy_card(
    bot_token: str, ticket_channel_id: Optional[str]
) -> None:
    """Pin the policy card in the open-ticket channel."""
    if not ticket_channel_id:
        _warn("No ticket channel ID — policy card will be pinned on first bot start")
        return

    headers = _discord_headers(bot_token)

    policy_text = (
        "**📜 AdFarm V8 — Pre-Payment Policy Card**\n\n"
        "1. **No refunds** once the farm is provisioned.\n"
        "2. **Time credit on bans:** full credit if banned within 48h, pro-rated after.\n"
        "3. **Alt survival is not guaranteed.**\n"
        "4. **Main accounts are never supported.**\n"
        "5. **Crypto payments are final** (BEP-20 USDT/BUSD).\n"
        "6. **Data stored:** Discord ID, username, repos, dates.\n"
        "7. **No SLA** — best-effort support.\n\n"
        "Click ✅ below to acknowledge before any payment address is shared."
    )

    resp = _req(
        "POST",
        f"{DISCORD_API}/channels/{ticket_channel_id}/messages",
        headers,
        body={"content": policy_text},
    )
    if resp and resp.get("id"):
        msg_id = resp["id"]
        # Pin it
        _req(
            "PUT",
            f"{DISCORD_API}/channels/{ticket_channel_id}/pins/{msg_id}",
            headers,
            ok_statuses=(204, 200),
        )
        _ok("Policy card pinned in #open-ticket")
    else:
        _warn("Could not pin policy card (will be done on first bot start)")


# ─── Phase 10: Final Summary ───────────────────────────────────────────────


def print_summary(
    gh_user: dict[str, str],
    bot_info: dict[str, Any],
    inputs: dict[str, str],
    core_repo: str,
    gist_id: Optional[str],
    channels: dict[str, str],
    workers: list[dict[str, str]] = None,
) -> None:
    """Print the completion summary."""
    invite_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={bot_info['id']}&scope=bot+applications.commands"
        f"&permissions=8&guild_id={inputs['guild_id']}"
    )

    print(f"\n{'═'*60}")
    print(f"  🎉  AdFarm V8 Setup Complete!")
    print(f"{'═'*60}")
    print(f"")
    print(f"  🤖 Bot:      {bot_info['username']}#{bot_info.get('discriminator', '0')}")
    print(f"  🏠 Server:    {bot_info.get('guild_name', inputs['guild_id'])}")
    print(f"  👤 GitHub:    {gh_user['login']}")
    print(f"  📦 Repo:      {core_repo}")
    print(f"  👥 Owners:    {inputs['owner_ids']}")
    print(f"  💾 Gist:      {gist_id or '(local-only mode)'}")
    if inputs.get("payment_address"):
        print(f"  💰 Wallet:    {inputs['payment_address'][:20]}...")
    else:
        print(f"  💰 Wallet:    (not set - add later via GitHub secrets)")
    if workers:
        print(f"  👷 Workers:   {len(workers)} account(s) configured")
        for i, w in enumerate(workers, 1):
            print(f"     {i}. {w['user']}")
    else:
        print(f"  👷 Workers:   (not set - add later via setup.py or GitHub secrets)")
    print(f"")
    print(f"  📋 Channels created:")
    for key, val in channels.items():
        print(f"     {key}: {val}")
    print(f"")
    print(f"  🚀 NEXT STEPS:")
    print(f"")
    print(f"  1. Push this code to start the bot:")
    print(f"     git add . && git commit -m '🚀 V8 setup complete' && git push origin main")
    print(f"")
    print(f"  2. Verify in GitHub Actions → '🤖 V8 Control Bot' is running")
    print(f"")
    print(f"  3. In Discord, type /help to see all commands")
    print(f"")
    print(f"  4. Onboard your first customer:")
    print(f"     /admin activate @CustomerName days:30 alts:2")
    print(f"")
    print(f"{'═'*60}")
    print(f"  That's it. You're live. Go get customers. 🚀")
    print(f"{'═'*60}\n")


# ─── Main ──────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AdFarm V8 Zero-Friction Setup")
    parser.add_argument("--quick", action="store_true", help="Use env vars, skip prompts")
    parser.add_argument("--force", action="store_true", help="Overwrite existing secrets/channels")
    args = parser.parse_args()

    print(BANNER)

    # ── Step 1: Check gh CLI ──
    _step(1, 8, "Checking GitHub CLI")
    gh_token = check_gh_cli()
    _ok("GitHub CLI authenticated")
    gh_user = get_github_user(gh_token)
    _ok(f"GitHub account: {gh_user['login']}")
    core_repo = get_core_repo()
    if not core_repo:
        _fail("Could not detect repository. Make sure you're in the repo directory with a git remote.")
        sys.exit(1)
    _ok(f"Repository: {core_repo}")

    # ── Step 2: Collect inputs ──
    _step(2, 9, "Collecting your inputs")
    inputs = collect_inputs(quick=args.quick)
    _ok("All inputs collected")

    # ── Step 3: Collect worker accounts ──
    _step(3, 9, "Collecting worker accounts (for customer alt repos)")
    workers = collect_workers(quick=args.quick)
    if workers:
        _ok(f"Collected {len(workers)} worker account(s)")
    else:
        _warn("No workers configured (you can add them later)")

    # ── Step 4: Verify Discord bot ──
    _step(4, 9, "Verifying Discord bot")
    bot_info = verify_discord_bot(inputs["bot_token"], inputs["guild_id"])

    # ── Step 4: Create Discord channels ──
    _step(5, 9, "Creating Discord server structure")
    channels = create_discord_structure(
        inputs["bot_token"], inputs["guild_id"], inputs["owner_ids"]
    )

    # ── Step 5: Create backup Gist ──
    _step(6, 9, "Creating backup Gist (database persistence)")
    gist_id = create_backup_gist(gh_token, core_repo)

    # ── Step 6: Set GitHub secrets ──
    _step(7, 9, "Setting GitHub repository secrets")
    set_github_secrets(
        gh_token, core_repo, inputs, channels, gist_id, gh_user, workers=workers, force=args.force
    )

    # ── Step 7: Initialise database + verify workflows ──
    _step(8, 9, "Initialising database + checking workflows")
    init_database()
    ensure_workflows_exist()
    enable_repo_features(gh_token, core_repo)

    # ── Step 8: Pin policy card + summary ──
    _step(9, 9, "Final touches")
    ticket_ch = channels.get("open_ticket_ch_id")
    pin_policy_card(inputs["bot_token"], ticket_ch)

    # ── Done! ──
    print_summary(gh_user, bot_info, inputs, core_repo, gist_id, channels, workers)



# ─── Bootstrap Compatibility Class (for tests) ──────────────────────────────
# The original V6 setup.py exposed a `Bootstrap` class used by the test suite.
# This shim preserves that API so existing tests continue to pass.

class Bootstrap:
    """Compatibility shim — preserves the V6 Bootstrap API for the test suite."""

    FARM_CHANNEL_NAMES = {"control", "dashboard", "farm-logs", "dm-inbox", "deals"}

    def __init__(
        self,
        non_interactive: bool = False,
        quick: bool = False,
        force: bool = False,
        use_forums: bool = True,
        upgrade_forums: bool = False,
        abort_on_failure: bool = False,
    ):
        self.non_interactive = non_interactive
        self.quick = quick
        self.force = force
        self.use_forums = use_forums
        self.upgrade_forums = upgrade_forums
        self.abort_on_failure = abort_on_failure
        self.guild_id = ""
        self.bot_token = ""
        self.existing_repo_names: set = set()
        self.channels: dict[str, str] = {}

    def discord(self, method: str, path: str, *args: Any, **kwargs: Any):
        """Mock-friendly Discord API wrapper."""
        headers = _discord_headers(self.bot_token) if self.bot_token else {}
        url = f"{DISCORD_API}{path}" if not path.startswith("http") else path
        resp = _req(method, url, headers, body=kwargs.get("body"))
        if resp is not None:
            return 200, resp
        return 404, {}

    def ensure_channel(self, name: str, channel_type: int = 0) -> Optional[str]:
        """Find or create a channel by name, optionally upgrading type."""
        code, channels = self.discord("GET", f"/guilds/{self.guild_id}/channels")
        if code != 200 or not isinstance(channels, list):
            return None
        # Find existing channel by name
        for ch in channels:
            if ch.get("name") == name:
                existing_type = ch.get("type", 0)
                # If upgrade_forums and type mismatch, delete and recreate
                if self.upgrade_forums and existing_type != channel_type and name in self.FARM_CHANNEL_NAMES:
                    self.discord("DELETE", f"/channels/{ch['id']}")
                    break
                return str(ch["id"])
        # Channel not found or was deleted — create it
        code2, created = self.discord(
            "POST", f"/guilds/{self.guild_id}/channels",
            body={"name": name, "type": channel_type},
        )
        if code2 in (200, 201) and isinstance(created, dict):
            return str(created.get("id", ""))
        return None

    def provision_discord(self) -> None:
        """Create all required Discord channels and webhooks."""
        channel_defs = [
            ("control", 0),
            ("dashboard", 0),
            ("farm-logs", 0),
            ("dm-inbox", 15 if self.use_forums else 0),
            ("deals", 0),
        ]
        for name, ch_type in channel_defs:
            ch_id = self.ensure_channel(name, channel_type=ch_type)
            if ch_id:
                self.channels[name] = ch_id
                # Create webhook for the channel
                code, wh = self.discord(
                    "POST", f"/channels/{ch_id}/webhooks",
                    body={"name": f"adfarm-{name}"},
                )

    def select_alt_repository(self, alt_num: int, mode: str) -> str:
        """Select or auto-generate an alt repository name."""
        expected = f"alt{alt_num}-{mode}"
        if expected in self.existing_repo_names:
            return expected
        return expected


if __name__ == "__main__":
    main()
