"""github: client, worker pool, secrets sealing, provisioner, dispatcher, control queue."""
import json

import pytest

from adfarm.core.clock import FakeClock
from adfarm.core.errors import ConfigurationError, ExternalServiceError
from adfarm.db import Database, MetaRepo
from adfarm.github import ControlQueue, GitHubClient, RepoProvisioner, WorkerPool, WorkflowDispatcher, build_inputs, looks_like_token, seal_secret
from adfarm.github.accounts import CURSOR_KEY
from adfarm.config import WorkerAccount
from tests.fakes import FakeGitHubTransport, valid_token


@pytest.fixture
def transport():
    t = FakeGitHubTransport(tokens={"tm": "mainacct", "t1": "worker1", "t2": "worker2", "t3": "worker3"})
    t.add_gist("ctl", {})
    return t


@pytest.fixture
def pool(transport, tmp_path):
    db = Database(tmp_path / "p.db")
    db.migrate()
    base = GitHubClient("tm", transport=transport, retries=1)
    return WorkerPool((WorkerAccount("worker1", "t1"), WorkerAccount("worker2", "t2"), WorkerAccount("worker3", "t3")), base, MetaRepo(db), main_login="mainacct", main_client=base)


def test_client_errors_are_typed(transport):
    client = GitHubClient("tm", transport=transport, retries=1)
    with pytest.raises(ExternalServiceError) as exc:
        client.get("/repos/nobody/nothing")
    assert exc.value.status == 404
    assert client.get_repo("nobody", "nothing") is None  # 404 tolerated where documented
    bad = GitHubClient("wrong", transport=transport, retries=1)
    with pytest.raises(ExternalServiceError) as exc2:
        bad.viewer()
    assert exc2.value.status == 401


def test_client_retries_on_5xx(transport):
    transport.fail_next.append(("/user", 502))
    client = GitHubClient("tm", transport=transport, retries=2)
    assert client.viewer()["login"] == "mainacct"


def test_worker_pool_token_by_owner_and_round_robin_persists(pool, tmp_path):
    assert pool.client_for("worker2").viewer()["login"] == "worker2"
    assert pool.client_for("MAINACCT").viewer()["login"] == "mainacct"
    with pytest.raises(ConfigurationError):
        pool.client_for("stranger")
    picks = [pool.pick().login for _ in range(4)]
    assert picks == ["worker1", "worker2", "worker3", "worker1"]
    assert pool._meta.get_int(CURSOR_KEY) == 1
    # a *new* pool on the same DB continues where the previous one stopped (survives restarts)
    pool2 = WorkerPool(pool._workers, pool._base, pool._meta)
    assert pool2.pick().login == "worker2"
    assert pool.pick(exclude={"worker3"}).login in {"worker1", "worker2"}


def test_worker_pool_health_detects_wrong_owner(transport, tmp_path):
    db = Database(tmp_path / "h.db"); db.migrate()
    base = GitHubClient("tm", transport=transport, retries=1)
    pool = WorkerPool((WorkerAccount("worker1", "t1"), WorkerAccount("worker9", "t2"), WorkerAccount("dead", "zzz")), base, MetaRepo(db))
    health = {h.login: h for h in pool.health()}
    assert health["worker1"].ok
    assert not health["worker9"].ok and "belongs to 'worker2'" in health["worker9"].detail
    assert not health["dead"].ok


def test_seal_secret_is_not_plaintext_and_decrypts(transport):
    key = transport._key.public_key.encode(__import__("nacl.encoding", fromlist=["Base64Encoder"]).Base64Encoder()).decode()
    sealed = seal_secret(key, "hunter2")
    assert sealed != "hunter2" and transport.unseal(sealed) == "hunter2"
    assert looks_like_token(valid_token()) and not looks_like_token("short") and not looks_like_token("a b c")


def test_provisioner_creates_repo_uploads_sender_sets_secrets(pool, transport):
    prov = RepoProvisioner(pool)
    result = prov.ensure_repo("worker1", "alice_alt1")
    assert result.created and set(result.files) >= {"send_ads.py", ".github/workflows/send_ads.yml", ".github/workflows/self_check.yml", "channel_registry.py"}
    repo = transport.repo("worker1", "alice_alt1")
    assert repo.secret_scanning and not repo.private
    assert b"VERSION" in repo.files["send_ads.py"]
    again = prov.ensure_repo("worker1", "alice_alt1")
    assert not again.created  # idempotent
    done = prov.set_secrets("worker1", "alice_alt1", {"USER_TOKEN": "tok", "EMPTY": ""})
    assert done == ["USER_TOKEN"] and transport.secret("worker1", "alice_alt1", "USER_TOKEN") == "tok"
    prov.set_variables("worker1", "alice_alt1", {"ALT_ID": "7"})
    prov.set_variables("worker1", "alice_alt1", {"ALT_ID": "8"})  # PATCH path
    assert repo.variables["ALT_ID"] == "8"
    assert prov.secret_names("worker1", "alice_alt1") == ["USER_TOKEN"]
    assert prov.sender_version().startswith("V")


def test_provisioner_soft_delete_and_ban_rename(pool, transport):
    prov = RepoProvisioner(pool)
    prov.ensure_repo("worker2", "bob_alt1")
    assert prov.mark_banned("worker2", "bob_alt1") == "_BANNED_bob_alt1"
    assert transport.repo("worker2", "_BANNED_bob_alt1") is not None and transport.repo("worker2", "bob_alt1") is None
    assert prov.soft_delete("worker2", "gone") == "gone"  # missing → no-op
    assert prov.hard_delete("worker2", "_BANNED_bob_alt1") and not prov.exists("worker2", "_BANNED_bob_alt1")


def test_provisioner_refuses_unknown_owner(pool):
    with pytest.raises(ConfigurationError):
        RepoProvisioner(pool).ensure_repo("stranger", "x")


def test_build_inputs_sell_buy_and_channel_override():
    sell = build_inputs(ad_type="sell", message="m", sell_rate="2.30", interval_min=3, total_hours=24, channel_ids=("1", "2"))
    assert sell["sell_rate"] == "2.30" and sell["channel_1"] == "1" and sell["channel_2"] == "2" and sell["runtime_limitless"] == "false"
    buy = build_inputs(ad_type="buy", message="m", buy_rate="1.10", buy_items="skins", buy_items_price="5", limitless=True, channel_ids=("1", "2", "3"))
    assert buy["buy_rate"] == "1.10" and buy["buy_items"] == "skins" and buy["total_hours"] == "48" and buy["runtime_limitless"] == "true"
    assert "channel_1" not in buy  # >2 channels → CHANNEL_IDS secret is authoritative


def test_dispatcher_dispatch_cancel_targets_only_sender_runs(pool, transport):
    prov = RepoProvisioner(pool)
    prov.ensure_repo("worker1", "r1")
    disp = WorkflowDispatcher(pool, discover_wait=0.0)
    info = disp.dispatch("worker1", "r1", {"ad_type": "sell"})
    assert info is not None and info.active and info.workflow_file == "send_ads.yml"
    # an unrelated workflow run must not be cancelled
    other = disp.dispatch("worker1", "r1", {}) if False else None
    pool.client_for("worker1").dispatch_workflow("worker1", "r1", "self_check.yml", {})
    latest_any = transport.repo("worker1", "r1").runs[0]
    assert latest_any["path"].endswith("self_check.yml")
    cancelled = disp.cancel("worker1", "r1", info.run_id)
    assert cancelled == [info.run_id]
    assert latest_any["status"] == "in_progress"       # untouched
    assert disp.cancel("worker1", "r1", info.run_id) == []  # already cancelled
    assert disp.run("worker1", "r1", info.run_id).conclusion == "cancelled"
    assert disp.active_run("worker1", "r1") is None
    assert disp.self_check("worker1", "r1")


def test_dispatcher_cancel_without_run_id_cancels_all_active_sender_runs(pool, transport):
    RepoProvisioner(pool).ensure_repo("worker1", "r2")
    disp = WorkflowDispatcher(pool, discover_wait=0.0)
    a = disp.dispatch("worker1", "r2", {})
    b = disp.dispatch("worker1", "r2", {})
    assert sorted(disp.cancel("worker1", "r2")) == sorted([a.run_id, b.run_id])
    assert disp.recent("worker1", "r2")[0].status == "completed"


def test_dispatch_failure_is_typed(pool, transport):
    RepoProvisioner(pool).ensure_repo("worker1", "r3")
    transport.fail_dispatch = True
    with pytest.raises(ExternalServiceError):
        WorkflowDispatcher(pool, discover_wait=0.0).dispatch("worker1", "r3", {})


def test_control_queue_protocol_matches_sender(transport):
    client = GitHubClient("tm", transport=transport, retries=1)
    q = ControlQueue(client, "ctl", clock=FakeClock(500))
    cmd = q.set_price(7, 2.5)
    raw = json.loads(transport.gists["ctl"]["files"]["control_7.json"]["content"])
    assert raw["alt_id"] == 7 and raw["command"] == "setprice" and raw["args"] == "2.50" and raw["rate"] == 2.5 and raw["issued_at"] == 500
    assert raw["command_id"] == cmd.command_id and cmd.filename == "control_7.json"
    # sender acks by rewriting the same file
    raw.update({"ack_id": cmd.command_id, "ack": "✅ Price updated", "ack_at": 530})
    transport.gists["ctl"]["files"]["control_7.json"]["content"] = json.dumps(raw)
    ack = q.ack_for(7, cmd.command_id)
    assert ack and ack.text.startswith("✅")
    # a new command drops the stale ack but keeps overrides
    q.pause(7)
    raw2 = json.loads(transport.gists["ctl"]["files"]["control_7.json"]["content"])
    assert "ack" not in raw2 and raw2["rate"] == 2.5 and raw2["paused"] is True and raw2["command"] == "pause"
    q.set_overrides(7, message="hello")
    assert json.loads(transport.gists["ctl"]["files"]["control_7.json"]["content"])["message"] == "hello"
    q.clear(7)
    assert "control_7.json" not in transport.gists["ctl"]["files"]
    with pytest.raises(ValueError):
        q.enqueue(7, "format-disk")


def test_control_queue_disabled():
    q = ControlQueue(None, "")
    assert not q.enabled and q.read_raw(1) is None
    with pytest.raises(RuntimeError):
        q.stop(1)
