# AdFarm V9 — Architecture

This document is the component map for the control plane in `new_reform/` (package `adfarm`).
It mirrors the data-flow diagram in `analysis/02_REDESIGN.md` §6. The legacy system lives in
the repository root and is **untouched** — this is a clean-room reimplementation that keeps the
battle-tested `send_ads.py` sender byte-for-byte (`new_reform/sender/`).

## 1. Layering principle

```
                Discord guild (public · ticket · admin rooms · 🏢 Customer Hub forums)
                              ▲ slash commands / webhooks / heartbeats
┌─────────────────────────────┴──────────────────────────────────────────────────┐
│  commands/*   →  security.Guard  →  services/*  →  db.repositories  →  SQLite    │
│    (thin)        (tier+channel+      │                   │            (adfarm.db) │
│                     ownership)       │                   │                 │      │
│                                      ▼                   ▼                 ▼      │
│                          github.* (dispatch/secrets/contents)         db.GistBackup  │
│                          telemetry.* (FleetState, ingest)            (write-through)│
│                          timers.* (ExpiryEngine, LimitlessRenewer)                    │
└───────────────────────────────────────────────────────────────────────────────────┘
                              │ REST (dispatch / secrets / contents / gists)
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
     worker-1 account   worker-2 account   worker-3 account
     <user>_altN        …                …
     send_ads.py + workflows   (the sender is unchanged)
```

* **Pure core** (`core/`, `security/policy.py`, `timers/`, `telemetry/heartbeat.py`): no I/O,
  fully unit-testable with a fake clock.
* **Adapters** (`github/`, `discord/adapter.py`, `db/gist_backup.py`): the only places that touch
  the network. Everything above them is framework-neutral and tested with fakes.
* **Composition root** (`app.py::build_services`): the single place objects are wired together,
  so there are no module-level singletons and tests can inject fakes.

## 2. Module map

| Package | Responsibility |
|---|---|
| `core/` | `models` (frozen dataclasses), `errors`, `rules` (every limit/validator), `clock` |
| `db/` | `database` (WAL + transactions + migrations + online backup), `repositories` (SQL lives here only), `vault` (authenticated token encryption), `gist_backup` (write-through + lease + restore chain) |
| `github/` | `client` (one REST wrapper), `accounts` (3-worker round-robin pool), `secrets` (PyNaCl sealing — no `gh` CLI), `repos` (provision/upload/rename/delete), `workflows` (dispatch/cancel/inspect), `control_queue` (Gist command bus) |
| `discord/` | `channels` (classifier), `ports` (the `DiscordPort` protocol), `adapter` (discord.py impl — only file importing discord.py), `forums` (provision threads **and** webhooks), `embeds` (pure builders), `replies` (value object) |
| `security/` | `roles`, `policy` (single source for tiers + channel matrix + denial text), `guards` (Gate + MultiSig), `redact` |
| `telemetry/` | `heartbeat` (parse embed/JSON → `Heartbeat`), `fleet_state` (in-memory live per-alt state), `ingest` (thread-id routing + ban detection) |
| `timers/` | `expiry` (7/3/1-day reminders + shutdown), `renewal` (48 h limitless re-dispatch), `scheduler` (asyncio job runner — the only clock consumer) |
| `services/` | `customers`, `alts`, `runs`, `tickets`, `alerts`, `bans`, plus `container` (`Services` bundle) |
| `commands/` | `context` (CommandContext + error→Reply mapping), `public`, `customer`, `vip`, `admin`, `registry` (discord.py binding — the only other discord.py file) |
| `sender/` | `send_ads.py` (V6, verbatim), `channel_registry.py`, `workflows/*.yml` |
| `tools/` | `migrate_legacy.py` (legacy `customers.db` → `adfarm.db`) |

## 3. State boundaries

| State | Source of truth | Mirrors / caches | Sync rule |
|---|---|---|---|
| Customers | `adfarm.db` `customers` | Gist backup | Write-through after every transaction |
| Alts | `adfarm.db` `alts` | GitHub repo secrets/vars | DB first → push to GitHub; failed push → `dirty` → retried by sweeper |
| Alt tokens | GitHub repo secret (sealed) | `alts.token_ciphertext` (vault) | Both written together; DB copy only if `TOKEN_VAULT_KEY` set |
| Run state | `adfarm.db` `runs` | GitHub run status (polled) | Polled every 60 s; never authoritative |
| Telemetry | **Sender** (heartbeat) | `FleetState` (memory) | Rebuilt on boot from last heartbeat per dashboard thread |
| Control bus | Gist `control_<ALT_ID>.json` | `alts.runtime_overrides` | Queue write + DB write in same call |
| Worker cursor | `meta.worker_cursor` | — | Persisted so distribution survives chunk restarts |
| Events / audit | `adfarm.db` `events` | `#audit-logs` | Append-only |

**Reconciliation**: a `SyncSweeper` job (every 15 min) re-pushes `dirty` alts and marks
`missing` repos instead of deleting them.

## 4. Security model (summary)

* **Tiers**: `public ⊂ customer ⊂ vip ⊂ admin`. Resolve from `OWNER_IDS` then the customer row.
  Empty owner list ⇒ nobody is admin (fail closed).
* **Channel gate** (see `security/policy.py::CHANNEL_MATRIX`):
  * public commands: anywhere;
  * customer commands: own hub, admin room, or ticket room (`renew`/`pause-billing`/`proofs`/`account` only);
  * vip: own hub or admin room;
  * admin: admin room **only** (so the audit trail is in one place).
* **Ownership**: every alt-targeting operation goes through `AltService.resolve`, which enforces
  that non-admins may only address their own alts. There is no code path that turns an integer
  into an alt without a customer id.
* **Secrets**: Discord/GitHub tokens are env-only; alt tokens are sealed with the repo public key
  (PyNaCl, no `gh` CLI); the DB copy uses `TokenVault` (HMAC-CTR + tag, PBKDF2-derived key) and is
  skipped — never stored plaintext or XOR'd — when the key is missing. External systems never see plaintext tokens.
* **Destructive actions** (`/admin deactivate`, `alt remove`, `repo delete`, `reset`,
  `shutdown-bot`) require a typed confirmation; `reset` and `shutdown-bot` additionally require a
  second admin within 120 s (`MultiSig`). Every admin action writes an `events` row and an
  `#audit-logs` line.

## 5. Command set (18 top-level)

`help getstarted account setup run stop pause resume tune channels deals status reply alt
renew pause-billing proofs vip admin` — `vip` has 2 sub-commands (`autoreply`, `squad`); `admin`
has 17 (`list customer activate extend deactivate vip alt repo health backup tickets resolve
ticket-panel payment-address sync-commands logs reset shutdown-bot`). `/help` renders directly
from the policy tables so documentation cannot drift from enforcement.

## 6. Failure modes handled

* **Split brain**: the backup Gist holds an advisory lease; a second runner exits instead of racing.
* **Gist 404**: never auto-creates a new Gist (the legacy bug that lost the id); alerts and refuses.
* **Worker down**: round-robin skips failing workers and notifies `/admin health`.
* **Missing repo / banned alt**: `sweep-dirty` marks `missing`; the ban flow renames the repo,
  credits time, and prepares a fresh replacement repo.
* **Crash**: the GitHub Actions watchdog restarts the bot with back-off; the DB lease hands off
  cleanly between 8×350-min chunks.
