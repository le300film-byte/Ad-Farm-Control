#!/usr/bin/env python3
"""Interactive bootstrap for the Ad Farm core repository.

The script deliberately uses only the Python standard library. It uses the
already-authenticated GitHub CLI for repository secrets and the GitHub/Discord
REST APIs for the resources that cannot be created by the CLI.

Interactive use:
    python setup.py

Cloud/CI use (all sensitive values come from environment variables):
    python setup.py --non-interactive

Optional safety-policy flags:
    --force              replace existing GitHub secrets and variables
    --abort-on-failure   stop when an alt self-check fails

The script never prints token values. Treat the terminal and the generated
repository secrets as sensitive even though values are masked by GitHub.
"""
from __future__ import annotations

import argparse
import base64
import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DISCORD_API = "https://discord.com/api/v10"
GITHUB_API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent

# Discord permission bits used for the four private control channels.
MANAGE_CHANNELS = 1 << 4
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
MANAGE_WEBHOOKS = 1 << 29
BOT_CHANNEL_PERMS = (
    MANAGE_CHANNELS
    | VIEW_CHANNEL
    | SEND_MESSAGES
    | MANAGE_MESSAGES
    | EMBED_LINKS
    | READ_MESSAGE_HISTORY
    | MANAGE_WEBHOOKS
)


class SetupError(RuntimeError):
    """A user-actionable bootstrap failure."""


class Bootstrap:
    def __init__(self, non_interactive: bool = False, *, force: bool = False,
                 abort_on_failure: bool = False):
        self.non_interactive = non_interactive
        self.force = force
        self.abort_on_failure = abort_on_failure
        self.self_check_failures: list[str] = []
        self._existing_cache: dict[tuple[str, str], set[str]] = {}
        self.gh_token = ""
        self.bot_token = ""
        self.bot_user: dict[str, Any] = {}
        self.owner_id = ""
        self.owner_ids: list[str] = []
        self.guild_id = ""
        self.github_owner = ""
        self.core_repo = ""
        self.existing_repo_names: set[str] = set()
        self.alts: list[dict[str, str]] = []
        self.channels: dict[str, str] = {}
        self.webhooks: dict[str, str] = {}
        self.gists: dict[str, str] = {}
        self.channel_ids = ""
        self.channel_names = ""
        self.tuning_json = ""

    # ---------- process and prompt helpers ----------
    @staticmethod
    def run_command(args: list[str], *, input_text: str | None = None,
                    check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                args,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise SetupError(f"Could not run {' '.join(args)}: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SetupError(f"Command {' '.join(args)} failed: {detail[:500]}")
        return proc

    def ask(self, prompt: str, *, default: str = "", secret: bool = False,
            required: bool = True, env: str | None = None) -> str:
        if self.non_interactive:
            value = os.environ.get(env or "", "").strip() if env else ""
            if not value:
                value = default
            if required and not value:
                raise SetupError(f"Missing non-interactive value: {env or prompt}")
            return value
        suffix = f" [{default}]" if default else ""
        while True:
            if secret:
                value = getpass.getpass(f"{prompt}{suffix}: ").strip()
            else:
                value = input(f"{prompt}{suffix}: ").strip()
            if not value:
                value = default
            if value or not required:
                return value
            print("  A value is required.")

    def yes_no(self, prompt: str, default: bool = True, env: str | None = None) -> bool:
        default_text = "yes" if default else "no"
        value = self.ask(prompt, default=default_text, required=True, env=env).lower()
        return value in {"1", "y", "yes", "true", "on"}

    # ---------- preflight ----------
    def preflight(self) -> None:
        if not shutil.which("gh"):
            raise SetupError("GitHub CLI 'gh' is not installed or is not on PATH.")
        status = self.run_command(["gh", "auth", "status"], check=False)
        if status.returncode != 0:
            raise SetupError(
                "GitHub CLI is not authenticated. Run 'gh auth login' and retry."
            )
        if not self.non_interactive and os.environ.get("SETUP_SKIP_AUTH_REFRESH") != "1":
            print("Refreshing GitHub CLI scopes (workflow and gist)…")
            self.run_command(["gh", "auth", "refresh", "-s", "workflow,gist"])
        elif self.non_interactive:
            print("Using the masked GitHub CLI token supplied to this workflow.")
        token = self.run_command(["gh", "auth", "token"]).stdout.strip()
        if not token:
            raise SetupError("'gh auth token' returned no token.")
        self.gh_token = token
        self.core_repo = self.run_command(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        ).stdout.strip()
        if "/" not in self.core_repo:
            raise SetupError("Could not determine the current core repository.")
        print(f"✓ GitHub CLI authenticated; core repository: {self.core_repo}")

    # ---------- HTTP helpers ----------
    @staticmethod
    def _json_body(response) -> Any:
        raw = response.read()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: Any = None, timeout: int = 30) -> tuple[int, Any]:
        req_headers = {"User-Agent": "adfarm-bootstrap", "Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = Request(url, data=data, headers=req_headers, method=method.upper())
        try:
            with urlopen(req, timeout=timeout) as response:
                return response.status, self._json_body(response)
        except HTTPError as exc:
            return exc.code, self._json_body(exc)
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise SetupError(f"Network error calling {url}: {reason}") from exc

    def discord(self, method: str, path: str, token: str, *, bot: bool = False,
                body: Any = None) -> tuple[int, Any]:
        auth = f"Bot {token}" if bot else token
        return self.request(
            method,
            f"{DISCORD_API}{path}",
            headers={"Authorization": auth, "Content-Type": "application/json"},
            body=body,
        )

    def github(self, method: str, path: str, *, body: Any = None) -> tuple[int, Any]:
        return self.request(
            method,
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"Bearer {self.gh_token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            body=body,
        )

    # ---------- Discord provisioning ----------
    def prompt_valid_discord_token(self, label: str, *, env: str,
                                   bot: bool = False) -> tuple[str, dict[str, Any]]:
        """Prompt until a token authenticates, or fail clearly in CI.

        An interactive operator can correct a pasted token without restarting
        the bootstrap. Non-interactive jobs cannot safely prompt, so they
        fail once and identify the secret that needs correction.
        """
        while True:
            token = self.ask(label, secret=True, env=env)
            status, body = self.discord("GET", "/users/@me", token, bot=bot)
            if status == 200 and isinstance(body, dict) and body.get("id"):
                return token, body
            print(f"❌ {label} failed GET /users/@me (HTTP {status}).")
            if self.non_interactive:
                raise SetupError(f"Correct {env} and rerun the bootstrap.")
            print("  Please enter the token again. The previous value was not stored.")

    def collect_discord_inputs(self) -> None:
        self.bot_token, self.bot_user = self.prompt_valid_discord_token(
            "Official Discord bot token", env="BOT_TOKEN", bot=True
        )
        bot_name = self.bot_user.get("username") or self.bot_user.get("global_name") or "?"
        print(f"✓ Official bot validated as @{bot_name}")

        owner_default = os.environ.get("OWNER_IDS", "").strip() or os.environ.get("OWNER_ID", "").strip()
        owner_raw = self.ask(
            "Authorized owner Discord IDs (comma-separated)",
            default=owner_default,
            env="OWNER_IDS",
        )
        owner_parts = [part.strip() for part in owner_raw.split(",") if part.strip()]
        if not owner_parts or not all(part.isdigit() for part in owner_parts):
            raise SetupError("OWNER_IDS must contain one or more comma-separated numeric Discord IDs.")
        self.owner_ids = list(dict.fromkeys(owner_parts))
        self.owner_id = self.owner_ids[0]  # legacy compatibility for prompts/logs

        requested = os.environ.get("GUILD_ID", "").strip()
        if requested:
            # A supplied ID is authoritative and avoids relying on the
            # OAuth-only current-user guild listing endpoint, which some
            # Discord bot-token installations do not expose.
            self.guild_id = requested
        else:
            # Best-effort discovery keeps the one-server path beginner-friendly
            # where Discord exposes the list. Fall back to a prompt (or a clear
            # CI error) when the bot token cannot list guilds.
            status, guilds = self.discord("GET", "/users/@me/guilds", self.bot_token, bot=True)
            if status == 200 and isinstance(guilds, list) and guilds:
                if self.non_interactive:
                    self.guild_id = str(guilds[0].get("id", "")) if len(guilds) == 1 else ""
                    if not self.guild_id:
                        raise SetupError("GUILD_ID is required when the bot belongs to multiple servers.")
                elif len(guilds) == 1:
                    self.guild_id = str(guilds[0].get("id", ""))
                else:
                    print("Servers visible to the bot:")
                    for guild in guilds:
                        print(f"  {guild.get('id')}  {guild.get('name', '?')}")
                    self.guild_id = self.ask("Control server ID", required=True)
            elif self.non_interactive:
                raise SetupError(
                    "GUILD_ID is required in non-interactive mode when Discord "
                    "does not expose the bot's server list."
                )
            else:
                print("  Discord did not return a server list; enter the control server ID.")
                self.guild_id = self.ask("Control server ID", required=True)
        if not self.guild_id.isdigit():
            raise SetupError("GUILD_ID must be a numeric Discord ID.")

        self.channel_ids = self.ask(
            "Trading channel IDs (comma-separated; blank if using names)",
            required=False,
            env="CHANNEL_IDS",
        )
        channel_parts = [part.strip() for part in self.channel_ids.split(",") if part.strip()]
        if channel_parts and not all(part.isdigit() for part in channel_parts):
            raise SetupError("CHANNEL_IDS must contain only comma-separated numeric IDs.")
        self.channel_ids = ",".join(channel_parts)
        self.channel_names = self.ask(
            "Trading channel names (same order, optional; e.g. trading,market)",
            required=False,
            env="CHANNEL_NAMES",
        )
        name_parts = [part.strip() for part in self.channel_names.split(",") if part.strip()]
        if name_parts and channel_parts and len(name_parts) != len(channel_parts):
            raise SetupError("CHANNEL_NAMES must be empty or have one name for each CHANNEL_IDS entry, in the same order.")
        if not channel_parts and not name_parts:
            raise SetupError("Provide CHANNEL_IDS or CHANNEL_NAMES so sender targets are not empty.")
        self.channel_names = ",".join(name_parts)

    def ensure_channel(self, name: str) -> str:
        status, existing = self.discord(
            "GET", f"/guilds/{self.guild_id}/channels", self.bot_token, bot=True
        )
        if status != 200 or not isinstance(existing, list):
            raise SetupError(
                f"Could not list channels in guild {self.guild_id} (HTTP {status}). "
                "Give the bot Manage Channels and retry."
            )
        for channel in existing:
            if channel.get("type") == 0 and channel.get("name", "").lower() == name:
                print(f"  ✓ #{name} already exists ({channel.get('id')})")
                return str(channel["id"])

        # Hide the channel from @everyone and explicitly grant the bot enough
        # permissions to post, pin, read history, and manage webhooks.
        overwrites = [
            {"id": self.guild_id, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},
            {
                "id": str(self.bot_user["id"]),
                "type": 1,
                "allow": str(BOT_CHANNEL_PERMS),
                "deny": "0",
            },
        ]
        status, channel = self.discord(
            "POST",
            f"/guilds/{self.guild_id}/channels",
            self.bot_token,
            bot=True,
            body={
                "name": name,
                "type": 0,
                "permission_overwrites": overwrites,
            },
        )
        if status not in (200, 201) or not isinstance(channel, dict) or not channel.get("id"):
            raise SetupError(
                f"Could not create #{name} (HTTP {status}). The bot needs Manage Channels."
            )
        print(f"  ✓ created #{name} ({channel['id']})")
        return str(channel["id"])

    @staticmethod
    def webhook_url(hook: dict[str, Any]) -> str:
        """Build a sender URL from any URL/token fields Discord returns."""
        if hook.get("url"):
            return str(hook["url"])
        if hook.get("id") and hook.get("token"):
            return f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
        return ""

    def ensure_webhook(self, channel_id: str, name: str) -> str:
        status, hooks = self.discord(
            "GET", f"/channels/{channel_id}/webhooks", self.bot_token, bot=True
        )
        if status != 200 or not isinstance(hooks, list):
            raise SetupError(
                f"Could not list webhooks in channel {channel_id} (HTTP {status}); "
                "no webhook was created to avoid a duplicate."
            )
        for hook in hooks:
            if hook.get("name", "").lower() != name.lower():
                continue
            url = self.webhook_url(hook)
            if not url and hook.get("id"):
                # Some Discord responses omit the token from the list
                # endpoint. Ask the individual webhook endpoint before
                # considering creation; never silently duplicate a named
                # webhook on a rerun.
                detail_status, detail = self.discord(
                    "GET", f"/webhooks/{hook['id']}", self.bot_token, bot=True
                )
                if detail_status == 200 and isinstance(detail, dict):
                    url = self.webhook_url(detail)
            if url:
                print(f"  ✓ webhook {name} already exists")
                return url
            raise SetupError(
                f"Webhook '{name}' already exists but Discord did not return its token. "
                "Recover the existing webhook URL or remove that named webhook, then retry; "
                "no duplicate was created."
            )

        status, hook = self.discord(
            "POST",
            f"/channels/{channel_id}/webhooks",
            self.bot_token,
            bot=True,
            body={"name": name},
        )
        if status not in (200, 201) or not isinstance(hook, dict) or not self.webhook_url(hook):
            raise SetupError(
                f"Could not create webhook {name} (HTTP {status}). "
                "The bot needs Manage Webhooks in the target channel."
            )
        print(f"  ✓ created webhook {name}")
        return self.webhook_url(hook)

    def provision_discord(self) -> None:
        print("\nCreating/reusing private control channels and four shared webhooks…")
        for channel_name in ("control", "dashboard", "dm-inbox", "farm-logs", "deals"):
            self.channels[channel_name] = self.ensure_channel(channel_name)
        self.webhooks = {
            "LOG_WEBHOOK_URL": self.ensure_webhook(self.channels["farm-logs"], "Farm Logs"),
            "DASHBOARD_WEBHOOK_URL": self.ensure_webhook(self.channels["dashboard"], "Farm Dashboard"),
            "DM_WEBHOOK_URL": self.ensure_webhook(self.channels["dm-inbox"], "Farm DM Inbox"),
            "DEAL_WEBHOOK_URL": self.ensure_webhook(self.channels["deals"], "Farm Deals"),
        }

    # ---------- alt accounts and GitHub resources ----------
    def discover_existing_repositories(self) -> set[str]:
        """List repository names without assuming the canonical alt names exist."""
        result = self.run_command(
            ["gh", "repo", "list", self.github_owner, "--limit", "100",
             "--json", "name", "--jq", ".[].name"],
            check=False,
        )
        if result.returncode != 0:
            print("⚠️ Could not list repositories for dynamic reuse; explicit ALT_REPO_N values are still honored.")
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def existing_alt_count(self) -> int:
        """Infer 1–4 configured alts from common alt repository naming patterns."""
        indexes = set()
        for name in self.existing_repo_names:
            lowered = name.lower()
            for index in range(1, 5):
                if (f"alt{index}" in lowered or f"alt-{index}" in lowered
                        or f"alt_{index}" in lowered):
                    indexes.add(index)
        return len(indexes)

    def select_alt_repository(self, index: int, ad_type: str) -> str:
        explicit = os.environ.get(f"ALT_REPO_{index}", "").strip()
        default = f"alt{index}-{ad_type}"
        if explicit:
            return explicit
        if default in self.existing_repo_names:
            return default
        candidates = sorted(
            name for name in self.existing_repo_names
            if any(token in name.lower() for token in (f"alt{index}", f"alt-{index}", f"alt_{index}"))
        )
        if len(candidates) == 1:
            print(f"  ✓ reusing detected repository for alt {index}: {candidates[0]}")
            return candidates[0]
        if not self.non_interactive:
            return self.ask(
                f"Alt {index} repository name",
                default=candidates[0] if candidates else default,
                env=f"ALT_REPO_{index}",
            )
        return default

    def collect_alt_inputs(self) -> None:
        inferred_count = self.existing_alt_count()
        if self.non_interactive:
            raw_count = os.environ.get("ALT_COUNT", "").strip() or str(inferred_count or 4)
        else:
            raw_count = self.ask(
                "Number of alts to configure (1-4)",
                default=str(inferred_count or 4), env="ALT_COUNT",
            )
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise SetupError("ALT_COUNT must be between 1 and 4.") from exc
        if not 1 <= count <= 4:
            raise SetupError("ALT_COUNT must be between 1 and 4.")

        defaults = [
            ("sell", "Seller Alpha"),
            ("sell", "Seller Beta"),
            ("buy", "Buyer Gamma"),
            ("buy", "Buyer Delta"),
        ]
        for index in range(1, count + 1):
            token, body = self.prompt_valid_discord_token(
                f"Alt {index} user token", env=f"ALT_TOKEN_{index}"
            )
            suggested_type, suggested_name = defaults[index - 1]
            # Use the validated Discord username as the default label. The
            # operator can still provide a friendlier override at the prompt.
            suggested_name = str(
                body.get("username") or body.get("global_name") or suggested_name
            )
            ad_type = self.ask(
                f"Alt {index} initial ad type (live heartbeats can change it)",
                default=suggested_type, env=f"ALT_TYPE_{index}",
            ).lower()
            if ad_type not in ("sell", "buy"):
                raise SetupError(f"ALT_TYPE_{index} must be sell or buy.")
            name = self.ask(
                f"Alt {index} display name", default=suggested_name,
                env=f"ALT_NAME_{index}",
            )
            self.alts.append({
                "id": str(index),
                "token": token,
                "discord_id": str(body["id"]),
                "name": name,
                "ad_type": ad_type,
                "repo": self.select_alt_repository(index, ad_type),
            })
            alt_username = body.get("username") or body.get("global_name") or "?"
            print(f"✓ Alt {index} validated as @{alt_username} (ID captured)")

        tuning = self.ask(
            "Optional TUNING_JSON object (leave blank for code defaults)",
            required=False, env="TUNING_JSON",
        )
        if tuning:
            try:
                if not isinstance(json.loads(tuning), dict):
                    raise ValueError("not an object")
            except (ValueError, json.JSONDecodeError) as exc:
                raise SetupError(f"TUNING_JSON must be a valid JSON object: {exc}") from exc
            self.tuning_json = tuning

    def determine_github_owner(self) -> None:
        login = self.run_command(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
        self.github_owner = self.ask(
            "GitHub owner or organization for the alt repositories",
            default=login, env="GITHUB_OWNER",
        )
        if "/" in self.github_owner or not self.github_owner:
            raise SetupError("GITHUB_OWNER must be a username or organization name, not owner/repo.")
        self.existing_repo_names = self.discover_existing_repositories()
        if self.existing_repo_names:
            print(f"✓ detected {len(self.existing_repo_names)} existing repositories under {self.github_owner}; reuse will be preferred")

    def ensure_alt_repo(self, repo_name: str) -> str:
        full = f"{self.github_owner}/{repo_name}"
        view = self.run_command(["gh", "repo", "view", full], check=False)
        if view.returncode == 0:
            print(f"  ✓ repository already exists: {full}")
            return full
        self.run_command([
            "gh", "repo", "create", full, "--private", "--add-readme",
            "--description", f"Ad Farm alt {repo_name}",
        ])
        print(f"  ✓ created private repository: {full}")
        return full

    def upload_template(self, repo: str, relative: str) -> None:
        local = ROOT / relative
        if not local.is_file():
            raise SetupError(f"Template file is missing from the core repo: {relative}")
        encoded_path = quote(relative, safe="/")
        status, current = self.github(
            "GET", f"/repos/{repo}/contents/{encoded_path}?ref=main"
        )
        body: dict[str, Any] = {
            "message": f"bootstrap: install {relative}",
            "content": base64.b64encode(local.read_bytes()).decode("ascii"),
            "branch": "main",
        }
        if status == 200 and isinstance(current, dict) and current.get("sha"):
            body["sha"] = current["sha"]
        status, response = self.github("PUT", f"/repos/{repo}/contents/{encoded_path}", body=body)
        if status not in (200, 201):
            message = response.get("message", "") if isinstance(response, dict) else ""
            raise SetupError(f"Could not upload {relative} to {repo} (HTTP {status}): {message}")
        print(f"    ✓ {relative}")

    def create_gist(self, filename: str, content: str, description: str) -> str:
        status, response = self.github(
            "POST", "/gists",
            body={
                "description": description,
                "public": False,
                "files": {filename: {"content": content}},
            },
        )
        if status not in (200, 201) or not isinstance(response, dict) or not response.get("id"):
            message = response.get("message", "") if isinstance(response, dict) else ""
            detail = (result.stderr or result.stdout).strip()
            raise SetupError(f"Could not inspect existing {kind}s on {repo}: {detail[:400]}")
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        self._existing_cache[cache_key] = names
        return names

    def should_write(self, repo: str, name: str, kind: str,
                    *, replace_existing: bool = False) -> bool:
        if name not in self.existing_names(repo, kind):
            return True
        if self.force or replace_existing:
            detail = " (--force)" if self.force else " (mapping refresh)"
            print(f"  ↻ replacing existing {kind} {name} on {repo}{detail}")
            return True
        if self.non_interactive:
            print(f"  ↷ preserving existing {kind} {name} on {repo} (use --force to replace)")
            return False
        return self.yes_no(
            f"Overwrite existing {kind} {name} on {repo}?",
            default=False,
        )

    def set_secret(self, repo: str, name: str, value: str,
                   *, replace_existing: bool = False) -> None:
        if not value or not self.should_write(
            repo, name, "secret", replace_existing=replace_existing
        ):
            return
        result = self.run_command(
            ["gh", "secret", "set", name, "--repo", repo],
            input_text=value + "\n",
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SetupError(f"Could not set secret {name} on {repo}: {detail[:400]}")

    def clear_secret(self, repo: str, name: str) -> None:
        """Remove a stale secret when the operator explicitly selects names-only mode."""
        if name not in self.existing_names(repo, "secret"):
            return
        result = self.run_command(
            ["gh", "secret", "delete", name, "--repo", repo, "--yes"],
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SetupError(f"Could not clear secret {name} on {repo}: {detail[:400]}")
        self._existing_cache[(repo, "secret")].discard(name)
        print(f"  ✓ cleared stale secret {name} on {repo}")

    def configure_repositories(self) -> None:
        repo_map = ",".join(f"{a['id']}:{a['repo']}" for a in self.alts)
        id_map = ",".join(f"{a['id']}:{a['discord_id']}" for a in self.alts)
        name_map = ",".join(f"{a['id']}:{a['name']}" for a in self.alts)
        controller_ids = ",".join(dict.fromkeys([str(self.bot_user["id"]), *self.owner_ids]))

        core_secrets = {
            "BOT_TOKEN": self.bot_token,
            "GUILD_ID": self.guild_id,
            "CONTROL_CH_ID": self.channels["control"],
            "DASHBOARD_CH_ID": self.channels["dashboard"],
            "LOG_CH_ID": self.channels["farm-logs"],
            "DEALS_CH_ID": self.channels["deals"],
            "OWNER_IDS": ",".join(self.owner_ids),
            "GH_TOKEN": self.gh_token,
            # GitHub reserves the GITHUB_ prefix for built-in names, so keep
            # the selected alt-repository owner under an allowed secret name.
            "ALT_GITHUB_OWNER": self.github_owner,
            "ALT_REPOS": repo_map,
            "ALT_DISCORD_IDS": id_map,
            "ALT_NAMES": name_map,
        }
        if self.tuning_json:
            core_secrets["TUNING_JSON"] = self.tuning_json
        print("\nWriting core repository secrets…")
        # These are aggregate maps, not independent credentials. Always refresh
        # them from the current validated alt list so rerunning bootstrap with a
        # larger ALT_COUNT actually registers the new alts with the control bot.
        mapping_names = {"ALT_REPOS", "ALT_DISCORD_IDS", "ALT_NAMES", "OWNER_IDS"}
        for name, value in core_secrets.items():
            self.set_secret(
                self.core_repo,
                name,
                value,
                replace_existing=name in mapping_names,
            )
        print(f"  ✓ configured {len(core_secrets)} core secrets")

        common_alt_secrets = {
            "CHANNEL_IDS": self.channel_ids,
            "CHANNEL_NAMES": self.channel_names,
            "LOG_WEBHOOK_URL": self.webhooks["LOG_WEBHOOK_URL"],
            "DASHBOARD_WEBHOOK_URL": self.webhooks["DASHBOARD_WEBHOOK_URL"],
            "DM_WEBHOOK_URL": self.webhooks["DM_WEBHOOK_URL"],
            "DEAL_WEBHOOK_URL": self.webhooks["DEAL_WEBHOOK_URL"],
            "GIST_TOKEN": self.gh_token,
            "GIST_ID": self.gists["GIST_ID"],
            "CONTROL_GIST_ID": self.gists["CONTROL_GIST_ID"],
            "CONTROLLER_USER_IDS": controller_ids,
            "PANIC_TRUSTED_IDS": ",".join(self.owner_ids),
            "CONFIRM_USER_IDS": ",".join(self.owner_ids),
        }
        if self.tuning_json:
            common_alt_secrets["TUNING_JSON"] = self.tuning_json
        for alt in self.alts:
            repo = alt["full_repo"]
            print(f"  configuring {repo}…")
            # CHANNEL_IDS and CHANNEL_NAMES are mutually optional. Clear the
            # opposite stale secret so a deliberate names-only (or IDs-only)
            # rerun is actually honored instead of leaving the sender with
            # an old preferred target list.
            if not self.channel_ids:
                self.clear_secret(repo, "CHANNEL_IDS")
            if not self.channel_names:
                self.clear_secret(repo, "CHANNEL_NAMES")
            self.set_variable(repo, "ALT_ID", alt["id"])
            self.set_variable(repo, "ALT_NAME", alt["name"])
            self.set_secret(repo, "USER_TOKEN", alt["token"])
            for name, value in common_alt_secrets.items():
                self.set_secret(
                    repo,
                    name,
                    value,
                    replace_existing=name in {"CONTROLLER_USER_IDS", "PANIC_TRUSTED_IDS", "CONFIRM_USER_IDS"},
                )
            # Upload last so the workflow's push trigger sees a fully
            # configured repository on its first automatic run.
            self.upload_template(repo, ".github/workflows/self_check.yml")
        print("  ✓ configured alt variables and secrets")

    # ---------- self-check and final output ----------
    def latest_workflow_id(self, repo: str, workflow: str) -> int:
        status, data = self.github(
            "GET", f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page=1"
        )
        if status == 200 and isinstance(data, dict) and data.get("workflow_runs"):
            try:
                return int(data["workflow_runs"][0].get("id") or 0)
            except (TypeError, ValueError):
                pass
        return 0

    def dispatch_workflow(self, repo: str, workflow: str) -> int:
        previous_id = self.latest_workflow_id(repo, workflow)
        status, response = self.github(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            body={"ref": "main"},
        )
        if status != 204:
            message = response.get("message", "") if isinstance(response, dict) else ""
            raise SetupError(
                f"Could not dispatch {workflow} in {repo} (HTTP {status}): {message}"
            )
        return previous_id

    def wait_for_workflow(self, repo: str, workflow: str, previous_id: int = 0,
                          timeout: int = 420) -> bool:
        started = time.time()
        endpoint = f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page=10"
        while time.time() - started < timeout:
            status, data = self.github("GET", endpoint)
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
            candidates = [
                r for r in runs
                if r.get("event") == "workflow_dispatch"
                and int(r.get("id") or 0) > previous_id
            ]
            if candidates:
                run = candidates[0]
                state = run.get("status")
                conclusion = run.get("conclusion") or ""
                if state == "completed":
                    print(f"    self-check: {conclusion or 'unknown'}")
                    return conclusion == "success"
                print(f"    self-check: {state or 'waiting'}…")
            else:
                print("    self-check: waiting for GitHub to create the run…")
            time.sleep(10)
        print("    self-check: timed out")
        return False

    def run_self_checks(self) -> None:
        print("\nDispatching self-checks and waiting for green (in parallel)…")
        failures: list[str] = []

        def check_one(alt: dict[str, str]) -> tuple[str, bool, str | None]:
            repo = alt["full_repo"]
            try:
                previous_id = self.dispatch_workflow(repo, "self_check.yml")
                passed = self.wait_for_workflow(
                    repo, "self_check.yml", previous_id=previous_id
                )
                return repo, passed, None
            except SetupError as exc:
                return repo, False, str(exc)

        # A self-check can wait up to seven minutes. Running the independent
        # alt checks concurrently keeps the normal four-alt bootstrap within
        # the cloud workflow timeout instead of multiplying that wait by four.
        with ThreadPoolExecutor(max_workers=max(1, len(self.alts))) as pool:
            futures = [pool.submit(check_one, alt) for alt in self.alts]
            for future in as_completed(futures):
                repo, passed, error = future.result()
                if error:
                    print(f"  ❌ {repo}: {error}")
                    failures.append(f"{repo} ({error})")
                elif passed:
                    print(f"  ✅ {repo}: self-check passed")
                else:
                    print(f"  ❌ {repo}: self-check failed or timed out")
                    failures.append(repo)

        self.self_check_failures = failures
        if not failures:
            print("✓ all requested alt self-checks are green")
            return

        print("\n⚠️ Self-check summary: the following alt(s) need attention:")
        for failure in failures:
            print(f"  - {failure}")
        if self.abort_on_failure:
            raise SetupError("Self-check failure (--abort-on-failure was supplied).")
        print("⚠️ Continuing because --abort-on-failure was not supplied. "
              "Fix the listed repositories before starting an alt run.")

    def print_summary(self) -> None:
        bot_id = str(self.bot_user["id"])
        permissions = BOT_CHANNEL_PERMS
        invite = (
            "https://discord.com/oauth2/authorize?client_id="
            f"{bot_id}&scope=bot%20applications.commands&permissions={permissions}"
        )
        print("\n" + "=" * 70)
        if self.self_check_failures:
            print("⚠️ AD FARM BOOTSTRAP COMPLETED WITH SELF-CHECK WARNINGS")
        else:
            print("✅ AD FARM BOOTSTRAP COMPLETE")
        print("=" * 70)
        print(f"Control server: {self.guild_id}")
        print("Channels: #control, #dashboard, #dm-inbox, #farm-logs, #deals")
        print(f"Alt repositories: {len(self.alts)}")
        print("Four webhooks configured: DMs, dashboard, consolidated logs, and separate deals")
        print("\nInvite link (if the bot still needs to be added to another server):")
        print(invite)
        print("\nNext steps:")
        print("  1. Start the '🤖 Control Bot' workflow in the core repository.")
        print("  2. Start the V6 control bot; it chains six-hour jobs for 24/7 operation.")
        print("  3. Use /run in #control; it opens the private settings form.")
        print("  4. Check #dashboard, #farm-logs, and #deals for live state and typed events.")
        if not self.self_check_failures:
            print("✅ Farm ready! Start the control bot from Actions or run /run in Discord.")
        else:
            print("⚠️ Farm resources are provisioned, but self-check warnings must be fixed before deployment.")

    def run(self) -> None:
        self.preflight()
        self.collect_discord_inputs()
        self.determine_github_owner()
        self.collect_alt_inputs()
        self.provision_discord()
        self.provision_github()
        self.configure_repositories()
        self.run_self_checks()
        self.print_summary()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Ad Farm resources")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="read sensitive inputs from environment variables for CI",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing GitHub secrets and variables without prompting",
    )
    parser.add_argument(
        "--abort-on-failure",
        action="store_true",
        help="stop with an error if any alt self-check fails",
    )
    args = parser.parse_args()
    force = args.force or os.environ.get("SETUP_FORCE", "").lower() in {"1", "true", "yes"}
    abort_on_failure = (
        args.abort_on_failure
        or os.environ.get("SETUP_ABORT_ON_FAILURE", "").lower() in {"1", "true", "yes"}
    )
    setup = Bootstrap(
        non_interactive=args.non_interactive,
        force=force,
        abort_on_failure=abort_on_failure,
    )
    try:
        setup.run()
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
    except SetupError as exc:
        print(f"\n❌ Setup stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())