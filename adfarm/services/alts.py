"""AltService — the alt registry (single source of truth) and its GitHub projection.

Replaces three legacy mechanisms (ALT_REPOS/ALT_DISCORD_IDS/ALT_NAMES core secrets, the
``alt_credentials`` table and ``customers.repos``) with one ``alts`` table (L-1, L-2, L-5).
Every alt-targeting command goes through ``resolve()`` which enforces ownership (L-4).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ..core.errors import ConfigurationError, ConflictError, ExternalServiceError, NotAuthorized, NotFound, ValidationError
from ..core.models import Actor, Alt, AltStatus, SyncState, Webhooks
from ..core.rules import MAX_CHANNELS_PER_ALT, channel_limit_message, repo_name_for, validate_alt_index, validate_channel_ids
from ..github.secrets import looks_like_token
from .container import Services

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenCheck:
    ok: bool
    user_id: str = ""
    username: str = ""
    display_name: str = ""
    detail: str = ""


class AltService:
    def __init__(self, s: Services, *, token_checker=None):
        self.s = s
        self._token_checker = token_checker or _default_token_checker

    # ── ownership ───────────────────────────────────────────────────────────
    def resolve(self, actor: Actor, alt_index: int | None, *, customer_id: str | None = None) -> Alt:
        """Return the alt the actor may operate on. Customers can only address their own alts."""
        if actor.is_admin and customer_id:
            owner = customer_id
        else:
            if customer_id and customer_id != actor.user_id:
                raise NotAuthorized()
            if actor.customer is None:
                raise NotAuthorized()
            owner = actor.user_id
        alts = self.s.repos.alts.for_customer(owner)
        if not alts:
            raise NotFound("❓ No alt registered yet. Run `/setup` first.")
        if alt_index is None:
            if len(alts) == 1:
                return alts[0]
            raise ValidationError("❌ You have several alts — pass the `alt` number (see /account).")
        customer = self.s.repos.customers.get(owner)
        idx = validate_alt_index(alt_index, customer.alt_count if customer else len(alts))
        for alt in alts:
            if alt.alt_index == idx:
                return alt
        raise NotFound(f"❓ Alt {idx} is not registered. Run `/setup` for it first.")

    def list_for(self, customer_id: str) -> list[Alt]:
        return self.s.repos.alts.for_customer(customer_id)

    # ── registration ────────────────────────────────────────────────────────
    async def register(self, customer_id: str, alt_index: int, *, actor_id: str, owner: str | None = None) -> Alt:
        """Create the alt row and its repository on a worker account (no credentials yet)."""
        customer = self.s.repos.customers.get(customer_id)
        if customer is None:
            raise NotFound("❓ No customer with that Discord ID.")
        idx = validate_alt_index(alt_index, customer.alt_count)
        existing = self.s.repos.alts.get(customer_id, idx)
        if existing and existing.status is not AltStatus.REMOVED:
            return existing
        if not self.s.settings.workers:
            raise ConfigurationError("⚠️ No worker GitHub accounts are configured. An admin must add WORKER tokens.")
        repo_name = repo_name_for(customer.username or customer_id, idx)
        # Worker failover: a worker whose API is down (or whose token died) must not block onboarding.
        tried: set[str] = set()
        attempts = 1 if owner else max(1, len(self.s.settings.workers))
        result = None
        worker_login = owner or ""
        for _ in range(attempts):
            worker_login = owner or self.s.workers.pick(exclude=tried).login
            try:
                result = await asyncio.to_thread(self.s.provisioner.ensure_repo, worker_login, repo_name)
                break
            except ExternalServiceError as exc:
                tried.add(worker_login)
                log.warning("provision on %s failed (%s) — trying another worker", worker_login, exc)
                if self.s.alerts:
                    await self.s.alerts.admin(f"worker-down:{worker_login}", f"Worker `{worker_login}` failed while provisioning `{repo_name}`: {exc}")
                if len(tried) >= attempts:
                    raise
        now = self.s.now()
        sender_id = existing.sender_alt_id if existing else self.s.repos.alts.next_sender_alt_id()
        alt = Alt(customer_id=customer_id, alt_index=idx, sender_alt_id=sender_id, repo_owner=worker_login, repo_name=repo_name,
                  status=AltStatus.PENDING, channel_ids=existing.channel_ids if existing else (), created_at=now)
        alt = self.s.repos.alts.save(alt, now=now)
        try:
            assert result is not None
            base_secrets = self._base_secrets(customer_id, alt)
            await asyncio.to_thread(self.s.provisioner.set_secrets, worker_login, repo_name, base_secrets)
            await asyncio.to_thread(self.s.provisioner.set_variables, worker_login, repo_name, {"ALT_ID": str(sender_id), "ALT_NAME": alt.label})
            alt = self.s.repos.alts.save(alt.with_(sync_state=SyncState.CLEAN), now=self.s.now())
            for warning in result.warnings:
                log.warning("provision %s: %s", result.slug, warning)
        except (ExternalServiceError, ConfigurationError) as exc:
            alt = self.s.repos.alts.save(alt.with_(sync_state=SyncState.DIRTY), now=self.s.now())
            if self.s.alerts:
                await self.s.alerts.admin(f"provision:{customer_id}:{idx}", f"Repo provisioning failed for {alt.repo_slug}: {exc}")
            raise
        self.s.fleet.register((customer_id, idx), sender_id)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "alt.register", customer_id=customer_id, alt=idx, repo=alt.repo_slug, sender_alt_id=sender_id)
        return alt

    async def store_credentials(self, actor: Actor, alt_index: int, *, token: str, channel_ids: str | tuple[str, ...], display_name: str = "",
                                customer_id: str | None = None) -> Alt:
        """/setup: validate the token live, write USER_TOKEN + CHANNEL_IDS to the alt repo, persist the mapping."""
        owner = customer_id if (actor.is_admin and customer_id) else actor.user_id
        customer = self.s.repos.customers.get(owner)
        if customer is None:
            raise NotAuthorized()
        idx = validate_alt_index(alt_index, customer.alt_count)
        channels = validate_channel_ids(channel_ids)
        token = str(token or "").strip().strip('"').strip("'")
        if not looks_like_token(token):
            raise ValidationError("❌ That does not look like a Discord user token. Paste the raw token without quotes.")
        check = await asyncio.to_thread(self._token_checker, token)
        if not check.ok:
            raise ValidationError(f"❌ Token rejected by Discord ({check.detail or 'invalid'}). Copy it again from a fresh login.")

        alt = self.s.repos.alts.get(owner, idx)
        if alt is None or alt.status is AltStatus.REMOVED:
            alt = await self.register(owner, idx, actor_id=actor.user_id)
        # A token can only be bound to one alt (prevents two customers sharing an account).
        for other in self.s.repos.alts.all():
            if other.discord_user_id == check.user_id and (other.customer_id, other.alt_index) != (owner, idx):
                raise ConflictError("❌ This Discord account is already registered as another alt.")

        ciphertext = ""
        if self.s.settings.store_tokens_in_db and self.s.vault.available:
            ciphertext = self.s.vault.seal(token)
        now = self.s.now()
        alt = alt.with_(discord_user_id=check.user_id, username=check.username, display_name=display_name or check.display_name or check.username,
                        channel_ids=channels, token_ciphertext=ciphertext, status=AltStatus.READY, sync_state=SyncState.DIRTY)
        alt = self.s.repos.alts.save(alt, now=now)
        await self.push_secrets(alt, token=token)
        if self.s.alerts:
            await self.s.alerts.audit(actor.user_id, "alt.setup", customer_id=owner, alt=idx, channels=len(channels), account=check.username)
        return self.s.repos.alts.get(owner, idx) or alt

    # ── GitHub projection ───────────────────────────────────────────────────
    def _base_secrets(self, customer_id: str, alt: Alt) -> dict[str, str]:
        st = self.s.settings
        hooks = self.s.customers.webhooks(customer_id) if self.s.customers else None
        secrets = {
            "GIST_ID": st.control_gist_id, "GIST_TOKEN": st.gist_token, "CONTROL_GIST_ID": st.control_gist_id,
            "CONTROLLER_USER_IDS": ",".join(sorted(st.controller_user_ids | {customer_id})),
            "ALT_ID": str(alt.sender_alt_id), "ALT_NAME": alt.label,
        }
        if hooks:
            secrets.update(hooks.as_secrets())
        return secrets

    async def push_secrets(self, alt: Alt, *, token: str | None = None) -> Alt:
        """Project DB truth onto the alt repo: token (if known), channels, webhooks, gist ids, variables."""
        values = self._base_secrets(alt.customer_id, alt)
        values["CHANNEL_IDS"] = ",".join(alt.channel_ids)
        if token is None and alt.token_ciphertext and self.s.vault.available:
            token = self.s.vault.try_open(alt.token_ciphertext)
        if token:
            values["USER_TOKEN"] = token
        try:
            await asyncio.to_thread(self.s.provisioner.set_secrets, alt.repo_owner, alt.repo_name, values)
            await asyncio.to_thread(self.s.provisioner.set_variables, alt.repo_owner, alt.repo_name, {"ALT_ID": str(alt.sender_alt_id), "ALT_NAME": alt.label})
            alt = self.s.repos.alts.save(alt.with_(sync_state=SyncState.CLEAN), now=self.s.now())
        except (ExternalServiceError, ConfigurationError) as exc:
            alt = self.s.repos.alts.save(alt.with_(sync_state=SyncState.DIRTY), now=self.s.now())
            if self.s.alerts:
                await self.s.alerts.admin(f"sync:{alt.customer_id}:{alt.alt_index}", f"Secret sync failed for {alt.repo_slug}: {exc}")
            raise
        return alt

    async def resync_customer(self, customer_id: str) -> int:
        count = 0
        for alt in self.s.repos.alts.for_customer(customer_id):
            if alt.status in (AltStatus.READY, AltStatus.PENDING):
                try:
                    await self.push_secrets(alt)
                    count += 1
                except (ExternalServiceError, ConfigurationError):
                    continue
        return count

    async def sweep_dirty(self) -> int:
        """Retry failed projections and detect missing repos (scheduler job)."""
        fixed = 0
        for alt in self.s.repos.alts.dirty():
            try:
                if not await asyncio.to_thread(self.s.provisioner.exists, alt.repo_owner, alt.repo_name):
                    await asyncio.to_thread(self.s.provisioner.ensure_repo, alt.repo_owner, alt.repo_name)
                await self.push_secrets(alt)
                fixed += 1
            except Exception as exc:
                log.warning("sweep: %s still dirty: %s", alt.repo_slug, exc)
        for alt in self.s.repos.alts.all(statuses=[AltStatus.READY]):
            try:
                if not await asyncio.to_thread(self.s.provisioner.exists, alt.repo_owner, alt.repo_name):
                    self.s.repos.alts.save(alt.with_(status=AltStatus.MISSING), now=self.s.now())
                    if self.s.alerts:
                        await self.s.alerts.admin(f"missing:{alt.repo_slug}", f"Alt repo {alt.repo_slug} no longer exists (customer {alt.customer_id}, alt {alt.alt_index}).")
            except Exception:
                continue
        return fixed

    # ── channels ────────────────────────────────────────────────────────────
    async def set_channels(self, alt: Alt, channel_ids: tuple[str, ...], *, actor_id: str, push_live: bool = True) -> Alt:
        channels = validate_channel_ids(channel_ids)
        alt = self.s.repos.alts.save(alt.with_(channel_ids=channels, sync_state=SyncState.DIRTY), now=self.s.now())
        alt = await self.push_secrets(alt)
        if push_live and self.s.queue.enabled and self._is_live(alt):
            try:
                await asyncio.to_thread(self.s.queue.set_channels, alt.sender_alt_id, channels)
            except Exception as exc:
                log.warning("live channel push failed for %s: %s", alt.repo_slug, exc)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "alt.channels", customer_id=alt.customer_id, alt=alt.alt_index, count=len(channels))
        return alt

    async def add_channel(self, alt: Alt, channel_id: str, *, actor_id: str) -> Alt:
        if channel_id in alt.channel_ids:
            return alt
        if len(alt.channel_ids) >= MAX_CHANNELS_PER_ALT:
            raise ValidationError(channel_limit_message())
        return await self.set_channels(alt, alt.channel_ids + (channel_id,), actor_id=actor_id)

    async def replace_channel(self, alt: Alt, old: str, new: str, *, actor_id: str) -> Alt:
        if old not in alt.channel_ids:
            raise NotFound(f"❓ Channel `{old}` is not configured on alt {alt.alt_index}.")
        channels = tuple(new if c == old else c for c in alt.channel_ids)
        return await self.set_channels(alt, channels, actor_id=actor_id)

    async def remove_channel(self, alt: Alt, channel_id: str, *, actor_id: str) -> Alt:
        if channel_id not in alt.channel_ids:
            raise NotFound(f"❓ Channel `{channel_id}` is not configured on alt {alt.alt_index}.")
        remaining = tuple(c for c in alt.channel_ids if c != channel_id)
        if not remaining:
            raise ValidationError("❌ An alt needs at least one channel; use `/channels action:replace` instead.")
        return await self.set_channels(alt, remaining, actor_id=actor_id)

    def _is_live(self, alt: Alt) -> bool:
        live = self.s.fleet.get((alt.customer_id, alt.alt_index))
        return bool(live and live.online)

    # ── removal / bans ──────────────────────────────────────────────────────
    async def remove(self, customer_id: str, alt_index: int, *, actor_id: str, soft: bool = True) -> Alt:
        alt = self.s.repos.alts.get(customer_id, alt_index)
        if alt is None:
            raise NotFound("❓ That alt is not registered.")
        if self.s.runs:
            await self.s.runs.stop(alt, reason="alt removed", actor_id=actor_id, quiet=True)
        try:
            await asyncio.to_thread(self.s.provisioner.scrub_secrets, alt.repo_owner, alt.repo_name, ["USER_TOKEN", "CHANNEL_IDS"])
            if soft:
                await asyncio.to_thread(self.s.provisioner.soft_delete, alt.repo_owner, alt.repo_name)
            else:
                await asyncio.to_thread(self.s.provisioner.hard_delete, alt.repo_owner, alt.repo_name)
        except (ExternalServiceError, ConfigurationError) as exc:
            log.warning("repo cleanup for %s failed: %s", alt.repo_slug, exc)
        self.s.queue.enabled and self._safe_clear_queue(alt.sender_alt_id)
        alt = self.s.repos.alts.save(alt.with_(status=AltStatus.REMOVED, token_ciphertext="", sync_state=SyncState.CLEAN), now=self.s.now())
        self.s.repos.runs.delete(customer_id, alt_index)
        self.s.fleet.forget((customer_id, alt_index))
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "alt.remove", customer_id=customer_id, alt=alt_index, repo=alt.repo_slug, soft=soft)
        return alt

    def _safe_clear_queue(self, sender_alt_id: int) -> None:
        try:
            self.s.queue.clear(sender_alt_id)
        except Exception as exc:  # pragma: no cover
            log.warning("queue clear failed: %s", exc)

    async def mark_banned(self, alt: Alt, *, reason: str) -> Alt:
        """Rename the repo to _BANNED_<name>, drop the token, keep channels for the replacement (same alt_index)."""
        try:
            await asyncio.to_thread(self.s.provisioner.scrub_secrets, alt.repo_owner, alt.repo_name, ["USER_TOKEN"])
            await asyncio.to_thread(self.s.provisioner.mark_banned, alt.repo_owner, alt.repo_name)
        except (ExternalServiceError, ConfigurationError) as exc:
            log.warning("ban rename for %s failed: %s", alt.repo_slug, exc)
        alt = self.s.repos.alts.save(alt.with_(status=AltStatus.BANNED, token_ciphertext="", discord_user_id="", sync_state=SyncState.CLEAN), now=self.s.now())
        self.s.repos.runs.delete(alt.customer_id, alt.alt_index)
        self.s.fleet.set_status((alt.customer_id, alt.alt_index), "stopped")
        if self.s.alerts:
            self.s.alerts.event(alt.customer_id, "alt_banned", alt=alt.alt_index, repo=alt.repo_slug, reason=reason[:200])
        return alt

    async def prepare_replacement(self, alt: Alt, *, actor_id: str = "system") -> Alt:
        """Fresh repo (same index) for a banned alt; channels are inherited; awaits /setup for the new token."""
        if alt.status is not AltStatus.BANNED:
            raise ConflictError("⚠️ Only banned alts can be replaced.")
        customer = self.s.repos.customers.get(alt.customer_id)
        worker = self.s.workers.pick(exclude={alt.repo_owner}).login if len(self.s.settings.workers) > 1 else alt.repo_owner
        suffix = int(self.s.now()) % 100000
        repo_name = f"{repo_name_for(customer.username if customer else alt.customer_id, alt.alt_index)}_r{suffix}"
        fresh = alt.with_(repo_owner=worker, repo_name=repo_name, status=AltStatus.PENDING, sync_state=SyncState.DIRTY, username="", display_name="")
        fresh = self.s.repos.alts.save(fresh, now=self.s.now())
        try:
            await asyncio.to_thread(self.s.provisioner.ensure_repo, worker, repo_name)
            await asyncio.to_thread(self.s.provisioner.set_secrets, worker, repo_name, self._base_secrets(alt.customer_id, fresh) | {"CHANNEL_IDS": ",".join(fresh.channel_ids)})
            await asyncio.to_thread(self.s.provisioner.set_variables, worker, repo_name, {"ALT_ID": str(fresh.sender_alt_id), "ALT_NAME": fresh.label})
            fresh = self.s.repos.alts.save(fresh.with_(sync_state=SyncState.CLEAN), now=self.s.now())
        except (ExternalServiceError, ConfigurationError) as exc:
            if self.s.alerts:
                await self.s.alerts.admin(f"replace:{alt.customer_id}:{alt.alt_index}", f"Replacement repo failed for customer {alt.customer_id}: {exc}")
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "alt.replace", customer_id=alt.customer_id, alt=alt.alt_index, repo=fresh.repo_slug)
        return fresh


def _default_token_checker(token: str) -> TokenCheck:
    """Live check against Discord (``GET /users/@me``). Imported lazily; tests inject a fake."""
    try:
        import requests
    except Exception:  # pragma: no cover
        return TokenCheck(False, detail="requests not installed")
    try:
        resp = requests.get("https://discord.com/api/v9/users/@me", headers={"Authorization": token, "User-Agent": "Mozilla/5.0"}, timeout=10)
    except Exception as exc:  # pragma: no cover - network
        return TokenCheck(False, detail=f"{type(exc).__name__}")
    if resp.status_code != 200:
        return TokenCheck(False, detail=f"HTTP {resp.status_code}")
    data = resp.json()
    return TokenCheck(True, user_id=str(data.get("id") or ""), username=str(data.get("username") or ""), display_name=str(data.get("global_name") or ""))
