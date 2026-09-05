"""Services — the dependency bundle handed to every command handler and timer job."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..config import Settings
from ..core.clock import Clock
from ..db import (AltRepo, CustomerRepo, Database, EventRepo, GistBackup, MetaRepo, PolicyAckRepo, ReminderRepo, RunRepo, TicketRepo, TokenVault, WebhookRepo)
from ..discord.ports import DiscordPort
from ..github import ControlQueue, RepoProvisioner, WorkerPool, WorkflowDispatcher
from ..security.guards import Guard, MultiSig
from ..telemetry import FleetState

if TYPE_CHECKING:  # pragma: no cover
    from .alerts import AlertService
    from .alts import AltService
    from .bans import BanService
    from .customers import CustomerService
    from .runs import RunService
    from .tickets import TicketService


@dataclass
class Repos:
    customers: CustomerRepo
    alts: AltRepo
    runs: RunRepo
    webhooks: WebhookRepo
    reminders: ReminderRepo
    policy_acks: PolicyAckRepo
    events: EventRepo
    tickets: TicketRepo
    meta: MetaRepo

    @classmethod
    def for_db(cls, db: Database) -> "Repos":
        return cls(CustomerRepo(db), AltRepo(db), RunRepo(db), WebhookRepo(db), ReminderRepo(db), PolicyAckRepo(db), EventRepo(db), TicketRepo(db), MetaRepo(db))


@dataclass
class Services:
    settings: Settings
    clock: Clock
    db: Database
    repos: Repos
    vault: TokenVault
    backup: GistBackup
    discord: DiscordPort
    workers: WorkerPool
    provisioner: RepoProvisioner
    dispatcher: WorkflowDispatcher
    queue: ControlQueue
    fleet: FleetState
    guard: Guard
    multisig: MultiSig
    # use-case services (set by the composition root; Optional to allow staged construction)
    customers: Optional["CustomerService"] = None
    alts: Optional["AltService"] = None
    runs: Optional["RunService"] = None
    tickets: Optional["TicketService"] = None
    alerts: Optional["AlertService"] = None
    bans: Optional["BanService"] = None
    shutdown_requested: bool = field(default=False)

    def now(self) -> float:
        return self.clock.now()
