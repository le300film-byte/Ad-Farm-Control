"""RunService — validate → dispatch / stop / pause / resume / tune for one alt.

Every method takes an ``Alt`` that was already resolved (and therefore owned) by ``AltService``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..core.errors import ConfigurationError, ConflictError, ExternalServiceError, ValidationError
from ..core.models import Alt, AltStatus, RunMode, RunState
from ..core.rules import (policy_defaults, validate_ad_type, validate_autoreply, validate_deal_delta, validate_interval, validate_keywords, validate_message, validate_policy, validate_price, validate_runtime)
from ..github.workflows import build_inputs
from .container import Services

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunRequest:
    ad_type: str
    message: str
    rate: float
    interval_min: int
    total_hours: int              # 0 = limitless
    attach_image: bool = False
    buy_style: str = "simple"
    buy_items: str = ""
    buy_items_price: str = ""
    policy: str = ""

    @classmethod
    def validated(cls, *, mode: str, rate: str, message: str, interval: int | str, hours: int | str, attach_image: bool = False,
                  buy_style: str = "simple", buy_items: str = "", buy_items_price: str = "", policy: str = "") -> "RunRequest":
        ad_type = validate_ad_type(mode)
        price = validate_price(rate)
        text = validate_message(message)
        if policy:
            policy = validate_policy(policy)
        return cls(ad_type=ad_type, message=text, rate=price, interval_min=validate_interval(interval), total_hours=validate_runtime(hours),
                   attach_image=bool(attach_image), buy_style=buy_style if buy_style in ("simple", "detailed") else "simple",
                   buy_items=buy_items.strip(), buy_items_price=buy_items_price.strip(), policy=policy)

    @property
    def limitless(self) -> bool:
        return self.total_hours == 0

    def payload(self) -> dict[str, Any]:
        return {"ad_type": self.ad_type, "message": self.message, "rate": self.rate, "interval_min": self.interval_min, "total_hours": self.total_hours,
                "attach_image": self.attach_image, "buy_style": self.buy_style, "buy_items": self.buy_items, "buy_items_price": self.buy_items_price, "policy": self.policy}

    def workflow_inputs(self, channel_ids: tuple[str, ...]) -> dict[str, str]:
        rate_text = f"{self.rate:.2f}"
        return build_inputs(ad_type=self.ad_type, message=self.message, sell_rate=rate_text if self.ad_type == "sell" else "",
                            buy_rate=rate_text if self.ad_type == "buy" else "", buy_style=self.buy_style, buy_items=self.buy_items,
                            buy_items_price=self.buy_items_price, interval_min=self.interval_min, total_hours=self.total_hours or 48,
                            limitless=self.limitless, attach_image=self.attach_image, channel_ids=channel_ids)


@dataclass(frozen=True)
class DispatchResult:
    run: RunState
    run_url: str
    renewed: bool = False


class RunService:
    def __init__(self, s: Services):
        self.s = s

    # ── start ───────────────────────────────────────────────────────────────
    async def start(self, alt: Alt, req: RunRequest, *, actor_id: str, renewal_of: RunState | None = None) -> DispatchResult:
        if alt.status is not AltStatus.READY:
            raise ConflictError({
                AltStatus.PENDING: "❌ This alt has no credentials yet — run `/setup` first.",
                AltStatus.BANNED: "❌ This alt is banned. Run `/setup` with a fresh account to replace it.",
                AltStatus.MISSING: "⚠️ This alt's repository is missing. An admin has been notified.",
            }.get(alt.status, "❌ This alt cannot run right now."))
        if not alt.channel_ids:
            raise ValidationError("❌ No target channels configured. Use `/channels action:overwrite` first.")
        customer = self.s.repos.customers.get(alt.customer_id)
        now = self.s.now()
        if customer is None or not customer.is_active(now):
            raise ConflictError("❌ Subscription inactive — renew before starting runs.")
        if renewal_of is None:
            current = self.s.repos.runs.get(alt.customer_id, alt.alt_index)
            if current and current.status in ("queued", "in_progress"):
                live = await asyncio.to_thread(self.s.dispatcher.active_run, alt.repo_owner, alt.repo_name)
                if live is not None:
                    raise ConflictError("⚠️ This alt already has a live run. Use `/stop` first or `/tune` to change settings.")
        if not self.s.settings.workers:
            raise ConfigurationError()

        inputs = req.workflow_inputs(alt.channel_ids)
        try:
            info = await asyncio.to_thread(self.s.dispatcher.dispatch, alt.repo_owner, alt.repo_name, inputs)
        except ExternalServiceError as exc:
            if self.s.alerts:
                await self.s.alerts.admin(f"dispatch:{alt.repo_slug}", f"Dispatch failed for {alt.repo_slug}: {exc}")
            raise
        run = RunState(
            customer_id=alt.customer_id, alt_index=alt.alt_index, mode=RunMode.LIMITLESS if req.limitless else RunMode.TIMED,
            runtime_hours=req.total_hours, started_at=renewal_of.started_at if renewal_of else now, last_dispatch_at=now,
            renewals=(renewal_of.renewals + 1) if renewal_of else 0, payload=req.payload(), run_id=info.run_id if info else None,
            status=info.status if info else "queued",
        )
        self.s.repos.runs.save(run)
        self.s.fleet.register((alt.customer_id, alt.alt_index), alt.sender_alt_id)
        self.s.fleet.set_status((alt.customer_id, alt.alt_index), "queued")
        # Seed runtime overrides so a mid-run restart keeps the last tuning; policy defaults apply immediately.
        if self.s.queue.enabled:
            overrides = {"paused": False, "rate": req.rate, "ad_type": req.ad_type, "message": req.message, "interval_min": req.interval_min}
            if req.policy:
                overrides.update(policy_defaults(req.policy))
            try:
                await asyncio.to_thread(self.s.queue.set_overrides, alt.sender_alt_id, **overrides)
            except Exception as exc:
                log.warning("seed overrides failed for %s: %s", alt.repo_slug, exc)
        url = info.html_url if info and info.html_url else f"https://github.com/{alt.repo_slug}/actions"
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.start" if not renewal_of else "run.renew", customer_id=alt.customer_id, alt=alt.alt_index,
                                      mode=req.ad_type, rate=req.rate, hours=req.total_hours, run_id=run.run_id)
        return DispatchResult(run=run, run_url=url, renewed=renewal_of is not None)

    async def renew(self, run: RunState) -> Optional[DispatchResult]:
        alt = self.s.repos.alts.get(run.customer_id, run.alt_index)
        if alt is None or alt.status is not AltStatus.READY:
            return None
        payload = run.payload
        req = RunRequest(ad_type=payload.get("ad_type", "sell"), message=payload.get("message", ""), rate=float(payload.get("rate", 1.0)),
                         interval_min=int(payload.get("interval_min", 5)), total_hours=0, attach_image=bool(payload.get("attach_image")),
                         buy_style=payload.get("buy_style", "simple"), buy_items=payload.get("buy_items", ""), buy_items_price=payload.get("buy_items_price", ""),
                         policy=payload.get("policy", ""))
        try:
            await asyncio.to_thread(self.s.dispatcher.cancel, alt.repo_owner, alt.repo_name, run.run_id)
        except ExternalServiceError:
            pass
        return await self.start(alt, req, actor_id="system", renewal_of=run)

    # ── stop / pause / resume ───────────────────────────────────────────────
    async def stop(self, alt: Alt, *, reason: str, actor_id: str, quiet: bool = False) -> int:
        run = self.s.repos.runs.get(alt.customer_id, alt.alt_index)
        cancelled: list[int] = []
        if self.s.queue.enabled:
            try:
                await asyncio.to_thread(self.s.queue.stop, alt.sender_alt_id)   # graceful (sender exits within ~30-45 s)
            except Exception as exc:
                log.warning("queue stop failed for %s: %s", alt.repo_slug, exc)
        try:
            cancelled = await asyncio.to_thread(self.s.dispatcher.cancel, alt.repo_owner, alt.repo_name, run.run_id if run else None)
        except (ExternalServiceError, ConfigurationError) as exc:
            log.warning("cancel failed for %s: %s", alt.repo_slug, exc)
        if run:
            self.s.repos.runs.save(run.with_(status="cancelled", conclusion=reason))
        self.s.fleet.set_status((alt.customer_id, alt.alt_index), "stopped")
        if self.s.alerts and not quiet:
            await self.s.alerts.audit(actor_id, "run.stop", customer_id=alt.customer_id, alt=alt.alt_index, reason=reason, cancelled=cancelled)
        return len(cancelled)

    async def stop_all_for(self, customer_id: str, *, reason: str) -> int:
        total = 0
        for alt in self.s.repos.alts.for_customer(customer_id):
            total += await self.stop(alt, reason=reason, actor_id="system", quiet=True)
        return total

    async def pause(self, alt: Alt, *, actor_id: str) -> str:
        cmd = await asyncio.to_thread(self.s.queue.pause, alt.sender_alt_id)
        self._remember(alt, paused=True)
        self.s.fleet.set_status((alt.customer_id, alt.alt_index), "paused")
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.pause", customer_id=alt.customer_id, alt=alt.alt_index)
        return cmd.command_id

    async def resume(self, alt: Alt, *, actor_id: str) -> str:
        cmd = await asyncio.to_thread(self.s.queue.resume, alt.sender_alt_id)
        self._remember(alt, paused=False)
        self.s.fleet.set_status((alt.customer_id, alt.alt_index), "active")
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.resume", customer_id=alt.customer_id, alt=alt.alt_index)
        return cmd.command_id

    # ── tune ────────────────────────────────────────────────────────────────
    async def tune(self, alt: Alt, *, actor_id: str, price: str | None = None, message: str | None = None, mode: str | None = None,
                   interval: int | None = None, hours: int | None = None, policy: str | None = None) -> list[str]:
        if not any(v is not None for v in (price, message, mode, interval, hours, policy)):
            raise ValidationError("❌ Provide at least one option to change.")
        if not self.s.queue.enabled:
            raise ConfigurationError()
        applied: list[str] = []
        q, sid = self.s.queue, alt.sender_alt_id
        if mode is not None:
            m = validate_ad_type(mode)
            await asyncio.to_thread(q.set_mode, sid, m); applied.append(f"mode → `{m}`"); self._remember(alt, ad_type=m)
        if price is not None:
            p = validate_price(price)
            await asyncio.to_thread(q.set_price, sid, p); applied.append(f"price → `${p:.2f}/1k`"); self._remember(alt, rate=p)
        if message is not None:
            t = validate_message(message)
            await asyncio.to_thread(q.set_message, sid, t); applied.append("message updated"); self._remember(alt, message=t)
        if interval is not None:
            iv = validate_interval(interval)
            await asyncio.to_thread(q.set_interval, sid, iv); applied.append(f"cadence → `{iv}m`"); self._remember(alt, interval_min=iv)
        if hours is not None:
            h = validate_runtime(hours)
            if h == 0:
                raise ValidationError("❌ Runtime 0 (limitless) must be chosen at /run time; use 6/12/18/24/48 here.")
            await asyncio.to_thread(q.set_runtime, sid, h); applied.append(f"runtime → `{h}h`")
        if policy is not None:
            pt = validate_policy(policy)
            await asyncio.to_thread(q.set_policy, sid, pt, policy_defaults(pt)); applied.append(f"policy → `{pt}`"); self._remember(alt, policy=pt)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.tune", customer_id=alt.customer_id, alt=alt.alt_index, changes=applied)
        return applied

    async def deals(self, alt: Alt, *, actor_id: str, keywords: str | None = None, delta: str | None = None, enabled: bool | None = None) -> list[str]:
        if keywords is None and delta is None and enabled is None:
            raise ValidationError("❌ Provide keywords, delta or enabled.")
        applied: list[str] = []
        q, sid = self.s.queue, alt.sender_alt_id
        if keywords is not None:
            kw = validate_keywords(keywords)
            await asyncio.to_thread(q.set_deal_keywords, sid, kw); applied.append(f"keywords → {', '.join(kw)[:100]}"); self._remember(alt, deal_keywords=list(kw))
        if delta is not None:
            d = validate_deal_delta(delta)
            await asyncio.to_thread(q.set_deal_delta, sid, d); applied.append(f"edge → `${d:.2f}/1k`"); self._remember(alt, deal_alert_delta=d)
        if enabled is not None:
            await asyncio.to_thread(q.set_deal_scan, sid, bool(enabled)); applied.append(f"scanner → `{'on' if enabled else 'off'}`"); self._remember(alt, deal_scan_enabled=bool(enabled))
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.deals", customer_id=alt.customer_id, alt=alt.alt_index, changes=applied)
        return applied

    async def reply(self, alt: Alt, user_id: str, text: str, *, actor_id: str) -> str:
        text = validate_autoreply(text)
        cmd = await asyncio.to_thread(self.s.queue.reply, alt.sender_alt_id, user_id, text)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "run.reply", customer_id=alt.customer_id, alt=alt.alt_index, to=user_id)
        return cmd.command_id

    async def rescan(self, alt: Alt) -> str:
        return (await asyncio.to_thread(self.s.queue.rescan, alt.sender_alt_id)).command_id

    async def reset_caution(self, alt: Alt, channel_id: str = "") -> str:
        return (await asyncio.to_thread(self.s.queue.reset_caution, alt.sender_alt_id, channel_id)).command_id

    # ── GitHub polling (scheduler) ──────────────────────────────────────────
    async def poll_runs(self) -> int:
        updated = 0
        for run in self.s.repos.runs.active():
            alt = self.s.repos.alts.get(run.customer_id, run.alt_index)
            if alt is None:
                self.s.repos.runs.delete(run.customer_id, run.alt_index)
                continue
            try:
                info = await asyncio.to_thread(self.s.dispatcher.run, alt.repo_owner, alt.repo_name, run.run_id) if run.run_id else \
                       await asyncio.to_thread(self.s.dispatcher.latest, alt.repo_owner, alt.repo_name)
            except (ExternalServiceError, ConfigurationError):
                continue
            if info is None:
                continue
            status = "in_progress" if info.active and info.status == "in_progress" else ("queued" if info.active else ("completed" if info.conclusion == "success" else (info.conclusion or "completed")))
            if status != run.status or (info.run_id and info.run_id != run.run_id):
                self.s.repos.runs.save(run.with_(run_id=info.run_id or run.run_id, status=status, conclusion=info.conclusion))
                updated += 1
                if not info.active:
                    self.s.fleet.set_status((run.customer_id, run.alt_index), "stopped")
        return updated

    # ── helpers ─────────────────────────────────────────────────────────────
    def _remember(self, alt: Alt, **overrides: Any) -> None:
        current = self.s.repos.alts.get(alt.customer_id, alt.alt_index) or alt   # never merge into a stale snapshot
        merged = dict(current.runtime_overrides) | overrides
        self.s.repos.alts.save(current.with_(runtime_overrides=merged), now=self.s.now())
