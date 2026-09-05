"""GitHub adapters: REST client, worker pool, secret sealing, repo provisioning, workflows, control queue."""
from .accounts import WorkerHealth, WorkerPool
from .client import GitHubClient, HttpTransport, Response
from .control_queue import Ack, ControlQueue, QueuedCommand, SENDER_COMMANDS
from .repos import ProvisionResult, RepoProvisioner
from .secrets import looks_like_token, seal_secret, sealing_available
from .workflows import RunInfo, WorkflowDispatcher, build_inputs

__all__ = [
    "GitHubClient", "HttpTransport", "Response", "WorkerPool", "WorkerHealth", "RepoProvisioner", "ProvisionResult",
    "WorkflowDispatcher", "RunInfo", "build_inputs", "ControlQueue", "QueuedCommand", "Ack", "SENDER_COMMANDS",
    "seal_secret", "sealing_available", "looks_like_token",
]
