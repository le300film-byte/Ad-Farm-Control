"""GuildProvisioner — creates the whole Discord server layout, idempotently.

This is the piece that removes the last manual step of the install: before V9.1 the
operator had to hand-create ``#welcome-about``, ``#admin-commands``, the
``🏢 Customer Hub`` category … and then copy every id into a GitHub secret.

Design mirrors the rest of the package: the logic here is pure and framework free and
talks to a tiny port (:class:`GuildAdminPort`, implemented by
``adapter.DiscordPyGuildAdmin`` for real and by ``tests.fakes.FakeGuildAdmin`` for the
unit tests). Nothing in this module imports discord.py.

Guarantees
----------
* **Idempotent** — an existing channel/category/role is reused (permissions are
  re-applied so a drifted server heals); nothing is ever duplicated or deleted.
* **Fail soft** — one channel failing (missing permission, rate limit) is logged and
  reported in :class:`ProvisionReport`; the rest of the layout still gets created.
* **Classifier-compatible** — every name created here is present in
  ``Settings.public_channel_names`` / ``ticket_channel_names`` / ``admin_channel_names``
  so ``ChannelClassifier`` tags them correctly even before the ids are configured.
* **Self-recording** — the resulting ids are written to the ``meta`` table
  (``MetaRepo``) under the same names as the environment variables, so a restart picks
  them up without the operator touching a secret.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional, Protocol, Sequence

log = logging.getLogger(__name__)

HUB_CATEGORY_NAME = "🏢 Customer Hub"
PUBLIC_CATEGORY_NAME = "📣 AdFarm"
STAFF_CATEGORY_NAME = "🛡️ AdFarm Staff"
ADMIN_ROLE_NAME = "Bot Admin"


# ─────────────────────────────────────────────────────────────────────────────
# Specs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ChannelSpec:
    name: str
    topic: str = ""
    staff: bool = False          # staff channels are hidden from @everyone
    meta_key: str = ""           # meta/env key the resulting id is stored under
    extra_keys: tuple[str, ...] = ()   # additional meta/env keys pointing at the same id


PUBLIC_CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec("welcome-about", "What AdFarm is and how to get started.", meta_key="WELCOME_CH_ID"),
    ChannelSpec("pricing-plans", "Plans, prices and payment instructions.", meta_key="PRICING_CH_ID"),
    ChannelSpec("whats-new", "Changelog and service announcements.", meta_key="WHATS_NEW_CH_ID"),
    ChannelSpec("open-ticket", "Open a ticket with /ticket — staff answer here.", meta_key="OPEN_TICKET_CH_ID"),
    ChannelSpec("general-chat", "Community chat.", meta_key="GENERAL_CHAT_CH_ID"),
)

STAFF_CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec("admin-commands", "Admin-only slash commands.", staff=True, meta_key="ADMIN_COMMANDS_CH_ID"),
    ChannelSpec("admin-chat", "Operator chat + bot alerts.", staff=True, meta_key="ADMIN_CHAT_CH_ID", extra_keys=("ADMIN_ALERTS_CH_ID",)),
    ChannelSpec("audit-logs", "Immutable audit trail of every privileged action.", staff=True, meta_key="AUDIT_LOG_CH_ID"),
)

ALL_CHANNELS: tuple[ChannelSpec, ...] = PUBLIC_CHANNELS + STAFF_CHANNELS
HUB_META_KEY = "CUSTOMER_HUB_ID"
ADMIN_ROLE_META_KEY = "BOT_ADMIN_ROLE_ID"


# ─────────────────────────────────────────────────────────────────────────────
# Permission model (framework neutral) — defined once in ``permissions.py`` and
# re-exported here so both the guild provisioner and the per-customer forums
# build their overrides from the same source of truth.
# ─────────────────────────────────────────────────────────────────────────────
from .permissions import (  # noqa: F401  (re-exported for callers importing from provision)
    ATTACH,
    BOT_FULL,
    CMDS,
    HISTORY,
    MANAGE,
    MANAGE_HOOKS,
    MANAGE_MSG,
    SEND,
    THREADS_IN,
    VIEW,
    Overwrite,
    forum_overwrites,
    hub_overwrites,
    public_overwrites,
    staff_overwrites,
)


# ─────────────────────────────────────────────────────────────────────────────
# Port
# ─────────────────────────────────────────────────────────────────────────────
class GuildAdminPort(Protocol):
    async def find_category(self, name: str) -> Optional[str]: ...
    async def create_category(self, name: str, overwrites: Sequence[Overwrite]) -> str: ...
    async def find_text_channel(self, name: str) -> Optional[str]: ...
    async def create_text_channel(self, name: str, *, category_id: str, topic: str, overwrites: Sequence[Overwrite]) -> str: ...
    async def apply_overwrites(self, channel_id: str, overwrites: Sequence[Overwrite]) -> bool: ...
    async def move_to_category(self, channel_id: str, category_id: str) -> bool: ...
    async def ensure_role(self, name: str) -> str: ...
    async def assign_role(self, role_id: str, user_id: str) -> bool: ...


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ProvisionReport:
    ids: dict[str, str] = field(default_factory=dict)       # meta key → id
    created: list[str] = field(default_factory=list)        # names created this run
    reused: list[str] = field(default_factory=list)         # names that already existed
    failures: list[tuple[str, str]] = field(default_factory=list)   # (name, error)
    admin_role_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        parts = [f"{len(self.created)} created", f"{len(self.reused)} reused"]
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return ", ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Provisioner
# ─────────────────────────────────────────────────────────────────────────────
class GuildProvisioner:
    """Creates categories, channels, the Bot Admin role and all permission overrides.

    ``store`` is any ``callable(key, value)`` (typically ``MetaRepo.set``) used to persist
    the resulting ids; pass ``None`` to only get them back in the report.
    """

    def __init__(self, api: GuildAdminPort, *, owner_ids: Iterable[str] = (), store: Optional[Callable[[str, str], None]] = None,
                 create_admin_role: bool = True, dry_run: bool = False):
        self.api = api
        self.owner_ids = tuple(str(x) for x in owner_ids if str(x).strip())
        self.store = store
        self.create_admin_role = create_admin_role
        self.dry_run = dry_run

    # ── public API ──────────────────────────────────────────────────────────
    async def provision(self) -> ProvisionReport:
        report = ProvisionReport()
        if self.dry_run:
            for spec in ALL_CHANNELS:
                report.created.append(spec.name)
            report.created.append(HUB_CATEGORY_NAME)
            return report

        report.admin_role_id = await self._admin_role(report)
        if report.admin_role_id:
            self._record(report, ADMIN_ROLE_META_KEY, report.admin_role_id)
            for uid in self.owner_ids:
                await self._safe(f"role:{uid}", lambda uid=uid: self.api.assign_role(report.admin_role_id, uid), report)

        public_cat = await self._category(PUBLIC_CATEGORY_NAME, public_overwrites(report.admin_role_id), report)
        staff_cat = await self._category(STAFF_CATEGORY_NAME, staff_overwrites(report.admin_role_id, self.owner_ids), report)
        hub_cat = await self._category(HUB_CATEGORY_NAME, hub_overwrites(report.admin_role_id, self.owner_ids), report)
        if hub_cat:
            self._record(report, HUB_META_KEY, hub_cat)

        for spec in ALL_CHANNELS:
            overwrites = (staff_overwrites(report.admin_role_id, self.owner_ids) if spec.staff
                          else public_overwrites(report.admin_role_id))
            parent = (staff_cat if spec.staff else public_cat) or ""
            cid = await self._channel(spec, parent, overwrites, report)
            if cid and spec.meta_key:
                self._record(report, spec.meta_key, cid)
                for key in spec.extra_keys:
                    self._record(report, key, cid)
        return report

    # ── steps ───────────────────────────────────────────────────────────────
    async def _admin_role(self, report: ProvisionReport) -> str:
        if not self.create_admin_role:
            return ""
        return await self._safe(ADMIN_ROLE_NAME, lambda: self.api.ensure_role(ADMIN_ROLE_NAME), report) or ""

    async def _category(self, name: str, overwrites: Sequence[Overwrite], report: ProvisionReport) -> str:
        existing = await self._safe(name, lambda: self.api.find_category(name), report)
        if existing:
            await self._safe(name, lambda: self.api.apply_overwrites(existing, overwrites), report)
            report.reused.append(name)
            return existing
        created = await self._safe(name, lambda: self.api.create_category(name, overwrites), report)
        if created:
            report.created.append(name)
        return created or ""

    async def _channel(self, spec: ChannelSpec, category_id: str, overwrites: Sequence[Overwrite], report: ProvisionReport) -> str:
        existing = await self._safe(spec.name, lambda: self.api.find_text_channel(spec.name), report)
        if existing:
            # heal drift: re-apply permissions and (re)parent, never duplicate
            await self._safe(spec.name, lambda: self.api.apply_overwrites(existing, overwrites), report)
            if category_id:
                await self._safe(spec.name, lambda: self.api.move_to_category(existing, category_id), report)
            report.reused.append(spec.name)
            log.info("[provision] #%s already exists (%s) — permissions refreshed", spec.name, existing)
            return existing
        created = await self._safe(
            spec.name,
            lambda: self.api.create_text_channel(spec.name, category_id=category_id, topic=spec.topic, overwrites=overwrites),
            report,
        )
        if created:
            report.created.append(spec.name)
            log.info("[provision] created #%s (%s)", spec.name, created)
        return created or ""

    # ── helpers ─────────────────────────────────────────────────────────────
    def _record(self, report: ProvisionReport, key: str, value: str) -> None:
        report.ids[key] = value
        if self.store is not None:
            try:
                self.store(key, value)
            except Exception as exc:  # pragma: no cover - storage is best effort
                log.warning("[provision] could not persist %s: %s", key, exc)

    async def _safe(self, what: str, call: Callable[[], Awaitable], report: ProvisionReport):
        try:
            return await call()
        except Exception as exc:
            log.warning("[provision] %s failed: %s", what, exc)
            report.failures.append((what, f"{type(exc).__name__}: {exc}"))
            return None
