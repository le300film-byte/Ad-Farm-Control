"""Settings — parsed once from the environment (optionally merged with TUNING_JSON).

No module-level state: ``Settings.from_env()`` returns an immutable object that the composition
root passes to every component. ``Settings.problems()`` lists misconfiguration so the bot can
refuse to start (fail closed) instead of discovering it at the first command.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from typing import Any, Mapping

from .core.rules import POLICY_VERSION


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip()) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in str(value or "").split(",") if x.strip())


def _ids(value: str | None) -> frozenset[str]:
    return frozenset(x for x in _csv(value) if x.isdigit())


@dataclass(frozen=True)
class WorkerAccount:
    login: str
    token: str

    def redacted(self) -> str:
        return f"{self.login}:***"


@dataclass(frozen=True)
class Settings:
    # Discord
    bot_token: str = ""
    guild_id: str = ""
    owner_ids: frozenset[str] = frozenset()
    admin_alerts_channel_id: str = ""
    admin_commands_channel_id: str = ""
    admin_chat_channel_id: str = ""
    audit_log_channel_id: str = ""
    ticket_channel_id: str = ""
    proofs_channel_id: str = ""
    customer_hub_category_id: str = ""
    customer_hub_marker: str = "customer hub"
    admin_role_id: str = ""      # "Bot Admin" role provisioned by setup.py (BOT_ADMIN_ROLE_ID)
    public_channel_names: tuple[str, ...] = ("welcome-about", "pricing-plans", "announcements", "whats-new", "general-chat")
    ticket_channel_names: tuple[str, ...] = ("open-ticket", "tickets")
    admin_channel_names: tuple[str, ...] = ("admin-commands", "admin-alerts", "admin-chat", "audit-logs")
    register_commands: bool = True
    # GitHub (main account)
    github_token: str = ""
    core_repo: str = ""
    workers: tuple[WorkerAccount, ...] = ()
    workflow_file: str = "send_ads.yml"
    self_check_workflow: str = "self_check.yml"
    # Gists
    control_gist_id: str = ""
    backup_gist_id: str = ""
    gist_token: str = ""
    # Persistence
    db_path: str = "adfarm.db"
    token_vault_key: str = ""
    store_tokens_in_db: bool = True
    # Business
    payment_address: str = ""
    policy_version: str = POLICY_VERSION
    # Timers (seconds)
    expiry_scan_interval: int = 3600
    renewal_scan_interval: int = 3600
    sync_sweep_interval: int = 900
    github_poll_interval: int = 60
    dashboard_refresh_interval: int = 300
    lease_renew_interval: int = 300
    lease_ttl: int = 600
    offline_after: int = 900
    autoreply_cooldown: int = 1800
    multisig_window: int = 120
    http_timeout: int = 20
    # Sender-side defaults pushed as secrets (webhooks are per customer)
    controller_user_ids: frozenset[str] = frozenset()
    run_id: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        tuning = _load_tuning(env.get("TUNING_JSON", ""))

        def get(name: str, default: str = "") -> str:
            raw = env.get(name, "")
            if raw is None or str(raw).strip() == "":
                raw = tuning.get(name, "")
            return str(raw if raw is not None else "").strip() or default

        owner_ids = _ids(get("OWNER_IDS") or get("OWNER_ID"))
        workers = _parse_workers(get)
        github_token = get("GH_TOKEN") or get("GH_ADMIN_TOKEN") or get("GITHUB_PAT")
        return cls(
            bot_token=get("BOT_TOKEN"),
            guild_id=get("GUILD_ID"),
            owner_ids=owner_ids,
            admin_alerts_channel_id=get("ADMIN_ALERTS_CH_ID"),
            admin_commands_channel_id=get("ADMIN_COMMANDS_CH_ID"),
            admin_chat_channel_id=get("ADMIN_CHAT_CH_ID"),
            audit_log_channel_id=get("AUDIT_LOG_CH_ID"),
            ticket_channel_id=get("OPEN_TICKET_CH_ID") or get("TICKET_CH_ID"),
            proofs_channel_id=get("PROOFS_CH_ID"),
            customer_hub_category_id=get("CUSTOMER_HUB_ID"),
            customer_hub_marker=(get("CUSTOMER_HUB_MARKER") or "customer hub").lower(),
            admin_role_id=get("BOT_ADMIN_ROLE_ID") or get("ADMIN_ROLE_ID"),
            public_channel_names=_csv(get("PUBLIC_CHANNELS")) or cls.public_channel_names,
            ticket_channel_names=_csv(get("TICKET_CHANNELS")) or cls.ticket_channel_names,
            admin_channel_names=_csv(get("ADMIN_CHANNELS")) or cls.admin_channel_names,
            register_commands=_truthy(get("ADFARM_REGISTER_COMMANDS"), True),
            github_token=github_token,
            core_repo=get("CORE_REPO") or env.get("GITHUB_REPOSITORY", "").strip(),
            workers=workers,
            workflow_file=get("WORKFLOW_FILE", "send_ads.yml"),
            self_check_workflow=get("SELF_CHECK_WORKFLOW", "self_check.yml"),
            control_gist_id=get("CONTROL_GIST_ID"),
            backup_gist_id=get("ADFARM_GIST_ID") or get("CUSTOMERS_GIST_ID") or get("CONTROL_GIST_ID"),
            gist_token=get("GIST_TOKEN") or github_token,
            db_path=get("ADFARM_DB") or get("CUSTOMERS_DB") or "adfarm.db",
            token_vault_key=get("TOKEN_VAULT_KEY"),
            store_tokens_in_db=_truthy(get("STORE_ALT_TOKENS_IN_DB"), True),
            payment_address=get("PAYMENT_ADDRESS"),
            policy_version=get("POLICY_VERSION", POLICY_VERSION),
            expiry_scan_interval=_int(get("EXPIRY_SCAN_INTERVAL_SEC"), 3600),
            renewal_scan_interval=_int(get("RENEWAL_SCAN_INTERVAL_SEC"), 3600),
            sync_sweep_interval=_int(get("SYNC_SWEEP_INTERVAL_SEC"), 900),
            github_poll_interval=_int(get("GITHUB_POLL_INTERVAL_SEC"), 60),
            dashboard_refresh_interval=_int(get("DASHBOARD_REFRESH_SEC"), 300),
            lease_renew_interval=_int(get("LEASE_RENEW_INTERVAL_SEC"), 300),
            lease_ttl=_int(get("DB_GIST_LEASE_SECONDS"), 600),
            offline_after=_int(get("OFFLINE_AFTER_SEC"), 900),
            autoreply_cooldown=_int(get("AUTOREPLY_COOLDOWN_SEC"), 1800),
            multisig_window=_int(get("MULTISIG_WINDOW_SEC"), 120),
            http_timeout=_int(get("CONTROL_HTTP_TIMEOUT"), 20),
            controller_user_ids=owner_ids | _ids(get("CONTROLLER_USER_IDS")),
            run_id=env.get("GITHUB_RUN_ID", "").strip() or f"local-{os.getpid()}",
            extra={k: v for k, v in tuning.items() if isinstance(v, str)},
        )

    # ── validation ──────────────────────────────────────────────────────────
    def problems(self, *, need_discord: bool = True) -> list[str]:
        out: list[str] = []
        if need_discord and not self.bot_token:
            out.append("BOT_TOKEN is missing.")
        if not self.owner_ids:
            out.append("OWNER_IDS is empty — no admin can operate the bot (fail closed).")
        if not self.github_token:
            out.append("GH_TOKEN is missing — dispatch/cancel and Gist backup are disabled.")
        if not self.workers:
            out.append("No worker accounts configured (WORKER_TOKENS / WORKER_n_USER+TOKEN) — customer provisioning is refused.")
        if not self.control_gist_id:
            out.append("CONTROL_GIST_ID is missing — runtime tuning commands cannot reach alts.")
        if not self.backup_gist_id:
            out.append("ADFARM_GIST_ID is missing — the database has no cross-run durability.")
        return out

    def worker_for(self, login: str) -> WorkerAccount | None:
        wanted = str(login or "").strip().lower()
        for w in self.workers:
            if w.login.lower() == wanted:
                return w
        return None

    # ── runtime overrides (ids provisioned by setup and stored in the meta table) ──
    def with_channel_ids(self, ids: Mapping[str, str]) -> "Settings":
        """Return a copy with any *missing* channel/category ids filled in from ``ids``.

        ``ids`` uses the same keys as the environment (``CUSTOMER_HUB_ID`` …) and is
        normally ``MetaRepo.all()``: setup.py provisions the server, writes the ids to the
        ``meta`` table, and the bot picks them up on the next boot without a single secret
        being copied by hand. Explicit environment values always win.
        """
        mapping = {
            "ADMIN_ALERTS_CH_ID": "admin_alerts_channel_id",
            "ADMIN_COMMANDS_CH_ID": "admin_commands_channel_id",
            "ADMIN_CHAT_CH_ID": "admin_chat_channel_id",
            "AUDIT_LOG_CH_ID": "audit_log_channel_id",
            "OPEN_TICKET_CH_ID": "ticket_channel_id",
            "PROOFS_CH_ID": "proofs_channel_id",
            "CUSTOMER_HUB_ID": "customer_hub_category_id",
            "BOT_ADMIN_ROLE_ID": "admin_role_id",
        }
        patch = {attr: str(ids[key]).strip() for key, attr in mapping.items()
                 if str(ids.get(key, "")).strip() and not getattr(self, attr)}
        return replace(self, **patch) if patch else self

    def redacted(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if "token" in f.name or "key" in f.name:
                out[f.name] = "***" if value else ""
            elif f.name == "workers":
                out[f.name] = [w.redacted() for w in value]
            elif f.name == "extra":
                out[f.name] = sorted(value.keys())
            else:
                out[f.name] = sorted(value) if isinstance(value, frozenset) else value
        return out


def _load_tuning(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): (json.dumps(v) if isinstance(v, (dict, list)) else ("true" if v is True else "false" if v is False else str(v)))
            for k, v in value.items() if v is not None}


def _parse_workers(get) -> tuple[WorkerAccount, ...]:
    seen: dict[str, str] = {}
    for pair in _csv(get("WORKER_TOKENS")):
        if ":" in pair:
            login, token = pair.split(":", 1)
            if login.strip() and token.strip():
                seen.setdefault(login.strip(), token.strip())
    owners = _csv(get("WORKER_GITHUB_OWNERS"))
    tokens = _csv(get("WORKER_TOKENS_LIST"))
    for idx, login in enumerate(owners):
        if idx < len(tokens) and tokens[idx]:
            seen.setdefault(login, tokens[idx])
    for i in range(1, 4):
        login = get(f"WORKER_{i}_USER")
        token = get(f"WORKER_{i}_TOKEN")
        if login and token:
            seen.setdefault(login, token)
    return tuple(WorkerAccount(login, token) for login, token in seen.items())
