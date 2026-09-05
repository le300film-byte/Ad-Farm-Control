"""WorkflowDispatcher — start / stop / inspect the sender workflow of one alt repository.

Fixes L-12: ``cancel`` targets the run id recorded at dispatch time (or, as a fallback, only
*active* runs of ``send_ads.yml``), never "the latest run of any workflow".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..core.clock import Clock, SystemClock
from ..core.rules import LIMITLESS_RENEW_AFTER_SEC
from .accounts import WorkerPool

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}

# The inputs ``sender/workflows/send_ads.yml`` declares under ``workflow_dispatch``. GitHub
# rejects a dispatch carrying anything else with ``422 Unexpected input(s)``, and every
# ``type: choice`` input must carry one of its declared options — which is why the boolean-ish
# flags below are "1"/"0" and "yes"/"no" rather than "true"/"false" (F02).
WORKFLOW_INPUTS = frozenset({
    "ad_type", "message", "sell_rate", "sell_extra", "buy_style", "buy_rate", "buy_rate_rap", "buy_simple_text",
    "channel_1", "channel_2", "channel_1_name", "channel_2_name", "interval_min", "total_hours",
    "runtime_limitless", "attach_image",
})
# Values the workflow's choice inputs accept.
RUNTIME_LIMITLESS_VALUES = ("0", "1")
ATTACH_IMAGE_VALUES = ("yes", "no")


def flag_0_1(value: bool) -> str:
    """``runtime_limitless`` is ``type: choice`` over 0/1 in send_ads.yml."""
    return "1" if value else "0"


def flag_yes_no(value: bool) -> str:
    """``attach_image`` is ``type: choice`` over yes/no in send_ads.yml."""
    return "yes" if value else "no"


def declared_only(inputs: dict[str, str]) -> dict[str, str]:
    """Drop anything send_ads.yml does not declare, so a dispatch can never 422."""
    dropped = sorted(set(inputs) - WORKFLOW_INPUTS)
    if dropped:
        log.warning("dropping workflow inputs not declared by send_ads.yml: %s", ", ".join(dropped))
    return {k: v for k, v in inputs.items() if k in WORKFLOW_INPUTS}


@dataclass(frozen=True)
class RunInfo:
    run_id: int
    status: str
    conclusion: str
    created_at: str
    html_url: str
    workflow_file: str = ""

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def build_inputs(*, ad_type: str, message: str, sell_rate: str = "", buy_rate: str = "", buy_style: str = "simple",
                 buy_items: str = "", buy_items_price: str = "", buy_items_style: str = "list", interval_min: int = 5,
                 total_hours: int = 24, limitless: bool = False, attach_image: bool = False, channel_ids: tuple[str, ...] = ()) -> dict[str, str]:
    """Inputs for send_ads.yml (names are the workflow's; see sender/workflows/send_ads.yml).

    F02: ``runtime_limitless`` is a 0/1 choice and ``attach_image`` a yes/no choice, so they are
    rendered with :func:`flag_0_1` / :func:`flag_yes_no` — sending ``"true"``/``"false"`` made
    GitHub reject the dispatch (or silently fall back to the workflow defaults, which meant
    ``/run hours:Limitless`` never actually ran limitless).
    """
    inputs: dict[str, str] = {
        "ad_type": ad_type,
        "message": message,
        "interval_min": str(int(interval_min)),
        "total_hours": str(int(total_hours) if not limitless else 48),
        "runtime_limitless": flag_0_1(limitless),
        "attach_image": flag_yes_no(attach_image),
    }
    if ad_type == "sell":
        inputs["sell_rate"] = sell_rate
    else:
        inputs["buy_rate"] = buy_rate
        inputs["buy_style"] = buy_style
        # buy_items / buy_items_price / buy_items_style are kept on RunRequest for the control
        # gist overrides, but send_ads.yml has no matching workflow_dispatch input — they are
        # dropped by declared_only() instead of 422-ing the dispatch.
        if buy_items:
            inputs["buy_items"] = buy_items
            inputs["buy_items_price"] = buy_items_price
            inputs["buy_items_style"] = buy_items_style
    # channel_1/channel_2 override the CHANNEL_IDS secret; the full list lives in the secret.
    if len(channel_ids) in (1, 2):
        inputs["channel_1"] = channel_ids[0]
        if len(channel_ids) == 2:
            inputs["channel_2"] = channel_ids[1]
    return declared_only(inputs)


class WorkflowDispatcher:
    def __init__(self, pool: WorkerPool, *, workflow_file: str = "send_ads.yml", clock: Clock | None = None, discover_wait: float = 6.0):
        self.pool = pool
        self.workflow_file = workflow_file
        self.clock = clock or SystemClock()
        self.discover_wait = float(discover_wait)

    # ── dispatch ────────────────────────────────────────────────────────────
    def dispatch(self, owner: str, repo: str, inputs: dict[str, str], *, ref: str = "main") -> Optional[RunInfo]:
        client = self.pool.client_for(owner)
        before = {r["id"] for r in client.list_runs(owner, repo, self.workflow_file, per_page=5)}
        client.dispatch_workflow(owner, repo, self.workflow_file, inputs, ref=ref)
        # workflow_dispatch returns 204 without a run id — discover it (best effort).
        deadline = time.monotonic() + self.discover_wait
        while True:
            for raw in client.list_runs(owner, repo, self.workflow_file, per_page=5):
                if raw["id"] not in before:
                    return self._info(raw)
            if time.monotonic() >= deadline or self.discover_wait <= 0:
                return None
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))

    def self_check(self, owner: str, repo: str, workflow_file: str = "self_check.yml") -> bool:
        client = self.pool.client_for(owner)
        client.dispatch_workflow(owner, repo, workflow_file, {}, ref="main")
        return True

    # ── stop ────────────────────────────────────────────────────────────────
    def cancel(self, owner: str, repo: str, run_id: Optional[int] = None) -> list[int]:
        """Cancel the recorded run (if still active) or every active sender run. Returns cancelled ids."""
        client = self.pool.client_for(owner)
        cancelled: list[int] = []
        if run_id:
            info = client.get_run(owner, repo, run_id)
            if info and info.get("status") in ACTIVE_STATUSES:
                if client.cancel_run(owner, repo, run_id):
                    cancelled.append(int(run_id))
                return cancelled
        for raw in client.list_runs(owner, repo, self.workflow_file, per_page=10):
            if raw.get("status") in ACTIVE_STATUSES and client.cancel_run(owner, repo, int(raw["id"])):
                cancelled.append(int(raw["id"]))
        return cancelled

    # ── inspect ─────────────────────────────────────────────────────────────
    def latest(self, owner: str, repo: str) -> Optional[RunInfo]:
        runs = self.pool.client_for(owner).list_runs(owner, repo, self.workflow_file, per_page=1)
        return self._info(runs[0]) if runs else None

    def recent(self, owner: str, repo: str, limit: int = 5) -> list[RunInfo]:
        return [self._info(r) for r in self.pool.client_for(owner).list_runs(owner, repo, self.workflow_file, per_page=limit)]

    def run(self, owner: str, repo: str, run_id: int) -> Optional[RunInfo]:
        raw = self.pool.client_for(owner).get_run(owner, repo, run_id)
        return self._info(raw) if raw else None

    def active_run(self, owner: str, repo: str) -> Optional[RunInfo]:
        for info in self.recent(owner, repo, limit=5):
            if info.active:
                return info
        return None

    @staticmethod
    def needs_renewal(last_dispatch_at: float, now: float) -> bool:
        return (now - float(last_dispatch_at or 0)) >= LIMITLESS_RENEW_AFTER_SEC

    def _info(self, raw: dict[str, Any]) -> RunInfo:
        return RunInfo(
            run_id=int(raw.get("id") or 0),
            status=str(raw.get("status") or ""),
            conclusion=str(raw.get("conclusion") or ""),
            created_at=str(raw.get("created_at") or ""),
            html_url=str(raw.get("html_url") or ""),
            workflow_file=str(raw.get("path") or "").rsplit("/", 1)[-1],
        )
