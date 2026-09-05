"""db: migrations, repositories, vault, gist backup + restore + lease."""
import base64
import json
import sqlite3

import pytest

from adfarm.core.clock import FakeClock
from adfarm.core.models import Alt, AltStatus, Customer, RunMode, RunState, SyncState, Webhooks
from adfarm.db import (AltRepo, BackupUnavailable, CustomerRepo, Database, EventRepo, GistBackup, MetaRepo, PolicyAckRepo, ReminderRepo, RunRepo, TicketRepo, TokenVault, VaultError, WebhookRepo)
from adfarm.github.client import GitHubClient
from tests.fakes import FakeGitHubTransport


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    return d


def test_migrate_is_idempotent_and_versioned(db):
    assert db.schema_version == 1
    assert db.migrate() == 1
    tables = db.table_counts()
    for name in ("customers", "alts", "runs", "customer_webhooks", "reminders_sent", "policy_acks", "events", "tickets", "meta"):
        assert name in tables


def test_commit_hook_fires_once_per_outer_transaction(db):
    fired = []
    db.on_commit(lambda: fired.append(1))
    repo = MetaRepo(db)
    with db.transaction():
        repo.set("a", "1")
        repo.set("b", "2")
    assert fired == [1]
    repo.set("c", "3")
    assert fired == [1, 1]


def test_rollback_on_error(db):
    repo = MetaRepo(db)
    with pytest.raises(RuntimeError):
        with db.transaction():
            repo.set("x", "1")
            raise RuntimeError("boom")
    assert repo.get("x", "missing") == "missing"


def test_customer_repo_roundtrip(db):
    repo = CustomerRepo(db)
    c = Customer("1", "alice", 2, True, 10.0, 20.0, True, forum_id="f1", thread_ids={"control": "c1"}, autoreply_text="hi")
    repo.save(c, now=11.0)
    got = repo.get("1")
    assert got == c
    assert repo.by_forum("f1") == c and repo.by_forum("nope") is None
    repo.save(c.with_(expiry_date=5.0), now=12.0)
    assert repo.expired(now=6.0)[0].discord_id == "1"
    assert repo.expiring_between(6, 100) == []
    assert repo.expiring_between(0, 100)[0].discord_id == "1"
    assert repo.delete("1") and repo.get("1") is None


def test_alt_repo_roundtrip_and_indexes(db):
    CustomerRepo(db).save(Customer("1", "alice", 2, False, 0, 100, True), now=1.0)
    repo = AltRepo(db)
    assert repo.next_sender_alt_id() == 1
    a = Alt("1", 1, 1, "worker1", "alice_alt1", channel_ids=("111",), token_ciphertext="v1:x", runtime_overrides={"rate": 2.0})
    saved = repo.save(a, now=5.0)
    assert saved.created_at == 5.0
    got = repo.get("1", 1)
    assert got.channel_ids == ("111",) and got.runtime_overrides == {"rate": 2.0}
    assert repo.by_sender_id(1) == got and repo.by_repo("WORKER1", "ALICE_ALT1") == got
    repo.save(got.with_(sync_state=SyncState.DIRTY), now=6.0)
    assert [x.alt_index for x in repo.dirty()] == [1]
    repo.save(got.with_(status=AltStatus.REMOVED), now=7.0)
    assert repo.for_customer("1") == [] and len(repo.for_customer("1", include_removed=True)) == 1
    assert repo.next_sender_alt_id() == 2
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(Alt("1", 2, 1, "worker2", "other"), now=8.0)  # sender id must be unique


def test_cascade_delete_removes_alts_and_runs(db):
    CustomerRepo(db).save(Customer("1", "alice", 1, False, 0, 100, True), now=1.0)
    AltRepo(db).save(Alt("1", 1, 1, "w", "r"), now=1.0)
    RunRepo(db).save(RunState("1", 1, RunMode.TIMED, 24, 1.0, 1.0))
    CustomerRepo(db).delete("1")
    assert AltRepo(db).get("1", 1) is None and RunRepo(db).get("1", 1) is None


def test_run_repo(db):
    CustomerRepo(db).save(Customer("1", "alice", 1, False, 0, 100, True), now=1.0)
    AltRepo(db).save(Alt("1", 1, 1, "w", "r"), now=1.0)
    repo = RunRepo(db)
    r = RunState("1", 1, RunMode.LIMITLESS, 0, 1.0, 1.0, payload={"rate": 1.5}, run_id=5, status="in_progress")
    repo.save(r)
    assert repo.get("1", 1) == r and repo.active() == [r]
    repo.save(r.with_(status="cancelled"))
    assert repo.active() == [] and repo.all()[0].status == "cancelled"


def test_webhook_reminder_policy_event_ticket_meta_repos(db):
    CustomerRepo(db).save(Customer("1", "alice", 1, False, 0, 100, True), now=1.0)
    wh = WebhookRepo(db)
    wh.save(Webhooks("1", dashboard="d", logs="l", deals="x"), now=1.0)
    assert wh.get("1").complete() and wh.get("1").as_secrets()["LOG_WEBHOOK_URL"] == "l"
    rem = ReminderRepo(db)
    assert not rem.was_sent("1", 7, 100.0)
    rem.mark("1", 7, 100.0, now=2.0)
    assert rem.was_sent("1", 7, 100.0) and not rem.was_sent("1", 7, 200.0)
    rem.clear("1")
    assert not rem.was_sent("1", 7, 100.0)
    pa = PolicyAckRepo(db)
    assert not pa.has_acked("1", "v1")
    pa.ack("1", "v1", now=1.0)
    assert pa.has_acked("1", "v1")
    ev = EventRepo(db)
    ev.log("1", "run_start", now=1.0, alt=1)
    ev.log("1", "run_stop", now=2.0)
    assert [e.event for e in ev.recent(discord_id="1")] == ["run_stop", "run_start"]
    assert ev.last("1", "run_start").payload == {"alt": 1}
    tk = TicketRepo(db)
    tid = tk.open("1", "renew", now=1.0, days=30)
    assert tk.find_open("1", "renew")["id"] == tid
    assert tk.close(tid, now=2.0, status="closed", note="x")
    assert tk.find_open("1", "renew") is None and tk.list_open() == []
    meta = MetaRepo(db)
    meta.set("k", "5")
    assert meta.get_int("k") == 5 and meta.get("missing", "d") == "d"
    meta.delete("k")
    assert meta.get("k") == ""


# ── vault ───────────────────────────────────────────────────────────────────
def test_vault_roundtrip_and_tamper_detection():
    v = TokenVault("a-long-master-key")
    sealed = v.seal("mfa.super-secret-token")
    assert sealed.startswith("v1:") and "super-secret" not in sealed
    assert v.open(sealed) == "mfa.super-secret-token"
    assert v.seal("x") != v.seal("x")            # random salt/nonce
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(VaultError):
        v.open(tampered)
    with pytest.raises(VaultError):
        TokenVault("another-master-key").open(sealed)
    assert v.try_open("garbage") is None
    assert TokenVault.is_sealed(sealed) and not TokenVault.is_sealed("plain")


def test_vault_refuses_without_key():
    v = TokenVault("")
    assert not v.available
    with pytest.raises(VaultError):
        v.seal("x")


# ── gist backup ─────────────────────────────────────────────────────────────
@pytest.fixture
def backup_env(tmp_path):
    transport = FakeGitHubTransport(tokens={"t": "main"})
    transport.add_gist("g1", {})
    client = GitHubClient("t", transport=transport, retries=1)
    db = Database(tmp_path / "b.db")
    db.migrate()
    clock = FakeClock(1000)
    backup = GistBackup(db, client, "g1", run_id="run-A", clock=clock, lease_ttl=600, debounce=0.01, retries=())
    return transport, client, db, backup, clock


def test_backup_upload_writes_current_prev_meta(backup_env):
    transport, client, db, backup, clock = backup_env
    CustomerRepo(db).save(Customer("1", "a", 1, False, 0, 100, True), now=1.0)
    assert backup.flush()
    files = transport.gists["g1"]["files"]
    assert "adfarm.db.b64" in files and "db-meta.json" in files and "adfarm.prev.db.b64" not in files
    meta = json.loads(files["db-meta.json"]["content"])
    assert meta["run_id"] == "run-A" and meta["seq"] == 1
    CustomerRepo(db).save(Customer("2", "b", 1, False, 0, 100, True), now=2.0)
    assert backup.flush()
    files = transport.gists["g1"]["files"]
    assert "adfarm.prev.db.b64" in files
    assert json.loads(files["db-meta.json"]["content"])["seq"] == 2


def test_backup_restore_chain_prefers_valid_current_then_prev_then_revision(backup_env, tmp_path):
    transport, client, db, backup, clock = backup_env
    CustomerRepo(db).save(Customer("1", "a", 1, False, 0, 100, True), now=1.0)
    backup.flush()
    CustomerRepo(db).save(Customer("2", "b", 1, False, 0, 100, True), now=2.0)
    backup.flush()
    # corrupt current → previous must be used
    transport.gists["g1"]["files"]["adfarm.db.b64"]["content"] = base64.b64encode(b"not a database").decode()
    fresh = Database(tmp_path / "restored.db")
    restorer = GistBackup(fresh, client, "g1", run_id="run-B", clock=clock)
    assert restorer.restore() == "previous"
    assert CustomerRepo(fresh).get("1") is not None and CustomerRepo(fresh).get("2") is None
    # corrupt previous too → fall back to a revision
    transport.gists["g1"]["files"]["adfarm.prev.db.b64"]["content"] = "!!!"
    fresh2 = Database(tmp_path / "restored2.db")
    assert GistBackup(fresh2, client, "g1", run_id="run-C", clock=clock).restore().startswith("revision:")


def test_backup_restore_if_missing_uses_local_when_healthy(backup_env):
    transport, client, db, backup, clock = backup_env
    CustomerRepo(db).save(Customer("1", "a", 1, False, 0, 100, True), now=1.0)
    assert backup.restore_if_missing() == "local"


def test_backup_404_is_reported_not_recreated(tmp_path):
    transport = FakeGitHubTransport(tokens={"t": "main"})
    client = GitHubClient("t", transport=transport, retries=1)
    db = Database(tmp_path / "c.db")
    db.migrate()
    backup = GistBackup(db, client, "missing", run_id="r", clock=FakeClock(1), retries=())
    assert backup.flush() is False
    assert "not found" in backup.last_error
    assert "missing" not in transport.gists and len(transport.gists) == 0
    with pytest.raises(BackupUnavailable):
        backup.restore()


def test_lease_acquire_conflict_and_expiry(backup_env):
    transport, client, db, backup, clock = backup_env
    assert backup.acquire_lease()
    other = GistBackup(db, client, "g1", run_id="run-B", clock=clock, lease_ttl=600)
    assert not other.acquire_lease() and other.lease_holder == "run-A"
    clock.advance(601)
    assert other.acquire_lease()
    # F04: run-A lost the lease when it expired, so it must not be able to release run-B's.
    assert backup.release_lease() is False
    assert json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])["run_id"] == "run-B"
    # the real holder still releases its own lease
    assert other.release_lease() is True
    assert json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])["run_id"] == ""


def test_backup_disabled_without_gist(tmp_path):
    db = Database(tmp_path / "d.db")
    db.migrate()
    b = GistBackup(db, None, "", run_id="r")
    assert not b.enabled and b.flush() is False and b.acquire_lease() and b.restore_if_missing() == "disabled"


def test_write_through_thread_coalesces(backup_env):
    transport, client, db, backup, clock = backup_env
    backup.attach()
    backup.start()
    for i in range(5):
        MetaRepo(db).set(f"k{i}", "v")
    backup.stop(flush=True)
    assert backup.status().seq >= 1
    assert json.loads(transport.gists["g1"]["files"]["db-meta.json"]["content"])["seq"] == backup.status().seq
    assert backup.status().seq < 5  # coalesced


# ═════════════════════════════════════════════════════════════════════════════
# V9 critical-fix regressions (F04 / F05 / F06)
# ═════════════════════════════════════════════════════════════════════════════
def test_f05_failed_begin_does_not_poison_later_transactions(tmp_path):
    """A BEGIN that fails on a lock timeout used to leave depth=1 behind, which made every
    later transaction on that thread reuse a closed connection and silently stop committing."""
    path = tmp_path / "locked.db"
    db = Database(path, busy_timeout=0.2)
    db.migrate()
    repo = MetaRepo(db)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
    try:
        with pytest.raises(sqlite3.OperationalError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO meta(key, value) VALUES ('never', 'committed')")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # the thread-local transaction state was reset, so the next write really commits
    repo.set("after", "1")
    assert repo.get("after") == "1"
    assert repo.get("never") == ""
    assert getattr(db._local, "depth", 0) == 0 and getattr(db._local, "conn", None) is None


def test_f05_rollback_error_does_not_mask_the_original(tmp_path):
    db = Database(tmp_path / "t2.db")
    db.migrate()
    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction():
            raise RuntimeError("boom")
    MetaRepo(db).set("ok", "1")
    assert MetaRepo(db).get("ok") == "1"


def test_f06_payload_is_usable_rejects_truncated_and_foreign_databases(tmp_path):
    db = Database(tmp_path / "t3.db")
    db.migrate()
    good = db.snapshot_bytes()
    assert db.payload_is_usable(good)
    assert not db.payload_is_usable(b"")
    assert not db.payload_is_usable(good[: len(good) // 2])
    foreign = sqlite3.connect(":memory:")
    foreign.execute("CREATE TABLE unrelated(x)")
    assert not db.payload_is_usable(foreign.serialize())


def test_f06_failed_restore_blocks_the_write_through_instead_of_overwriting(backup_env, tmp_path):
    transport, client, db, backup, clock = backup_env
    CustomerRepo(db).save(Customer("1", "a", 1, False, 0, 100, True), now=1.0)
    backup.flush()
    remote = transport.gists["g1"]["files"]["adfarm.db.b64"]["content"]
    # the snapshot on the gist is now garbage and no older revision survives
    transport.gists["g1"]["files"]["adfarm.db.b64"]["content"] = base64.b64encode(b"not a database").decode()
    transport.gists["g1"]["history"] = []
    fresh = Database(tmp_path / "empty.db")
    fresh.migrate()
    restorer = GistBackup(fresh, client, "g1", run_id="run-B", clock=clock, retries=())
    assert restorer.restore_if_missing() == "none"
    assert restorer.restore_blocked and restorer.status().restore_blocked
    # the empty local database must not clobber the remote snapshot
    assert restorer.flush() is False
    assert "refusing to overwrite" in restorer.last_error
    assert transport.gists["g1"]["files"]["adfarm.db.b64"]["content"] == base64.b64encode(b"not a database").decode()
    assert base64.b64decode(remote)  # the operator still has the original bytes to inspect
    # an operator can still force the overwrite once they have decided
    assert restorer.flush(force=True) is True


def test_f06_empty_gist_is_a_fresh_install_not_a_blocked_restore(backup_env, tmp_path):
    transport, client, db, backup, clock = backup_env
    fresh = Database(tmp_path / "new.db")
    fresh.migrate()
    restorer = GistBackup(fresh, client, "g1", run_id="run-B", clock=clock, retries=())
    assert restorer.restore_if_missing() == "none"
    assert not restorer.restore_blocked
    assert restorer.flush() is True


def test_f04_two_racing_runners_only_one_wins_the_lease(backup_env):
    transport, client, db, backup, clock = backup_env
    assert backup.acquire_lease()
    stolen = GistBackup(db, client, "g1", run_id="run-B", clock=clock, lease_ttl=600)
    assert not stolen.acquire_lease()
    assert json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])["run_id"] == "run-A"
    # renewing keeps our token, and a foreign holder is refused instead of overwritten
    assert backup.renew_lease() is True
    assert json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])["token"] == backup._lease_token
    assert stolen.renew_lease() is False
    assert json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])["run_id"] == "run-A"


def test_f04_reacquiring_after_release_works(backup_env):
    transport, client, db, backup, clock = backup_env
    assert backup.acquire_lease()
    assert backup.release_lease() is True
    other = GistBackup(db, client, "g1", run_id="run-B", clock=clock, lease_ttl=600)
    assert other.acquire_lease() is True
