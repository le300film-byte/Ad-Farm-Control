# AdFarm — Industrial-Grade Redesign (Phase 2)

This document turns the Phase 1 description into a blueprint for the new system built in
`new_reform/`. Every decision carries a justification that points at a concrete legacy problem
(§13 of `01_PROJECT_DESCRIPTION.md`, referenced as **L-n**).

## 0. Fixed constraints (unchanged)
1. Main repo is public.
2. Three worker GitHub accounts host the customer alt repositories.
3. A separate main GitHub account runs the control bot.
4. All interaction happens through Discord slash commands.
5. Payments are manual crypto (BEP-20) — no payment API.
6. Persistence is SQLite with Gist write-through.

## 1. Design principles applied
| Principle | How it shows up in the new code |
|---|---|
| One file, one job | 40 small modules (largest < 600 lines) instead of a 5 831-line `bot.py`. |
| No module-level singletons | A single composition root (`adfarm/app.py`) builds `Services` and injects them; every class takes its collaborators in `__init__`. |
| Fail closed | Unknown channel ⇒ deny customer commands; missing owner list ⇒ no admin; missing worker ⇒ refuse provisioning; secret sealing unavailable ⇒ refuse to store tokens. |
| Deterministic time | Every timer takes `now: float`; the scheduler is the only place that calls `time.time()`. |
| Pure core, thin edges | Discord/GitHub/HTTP are adapters behind small protocols; business rules live in `services/` and are tested with fakes. |
| Explicit ownership | Every alt-targeting operation resolves `(customer_id, alt_index)` from the *caller*; admins name the customer explicitly. |

---

## 2. New folder structure

```
new_reform/
├── README.md                    # install, run, operate
├── ARCHITECTURE.md              # component map + data-flow (mirrors §6 below)
├── SKILL.md                     # AI-operator skill for the new command set
├── setup.py                     # idempotent installer (REST only, --dry-run, --non-interactive)
├── requirements.txt
├── pytest.ini
├── analysis/                    # phase documents (01, 02, 04)
├── adfarm/                      # the control-bot package  (python -m adfarm)
│   ├── __main__.py              # entry point → app.main()
│   ├── app.py                   # composition root: Settings → Database → Services → Bot → loops
│   ├── config.py                # Settings dataclass; parsed & validated once; no globals
│   ├── core/
│   │   ├── models.py            # Customer, Alt, RunState, Tier, AltStatus … (frozen dataclasses)
│   │   ├── errors.py            # AdFarmError hierarchy (NotAuthorized, NotFound, Validation…)
│   │   ├── rules.py             # business constants + validators (limits, prices, intervals)
│   │   └── clock.py             # Clock protocol + SystemClock/FakeClock
│   ├── db/
│   │   ├── database.py          # Database: connection factory, transactions, migrations runner
│   │   ├── migrations.py        # ordered SQL migrations (v1 …)
│   │   ├── repositories.py      # CustomerRepo, AltRepo, RunRepo, EventRepo, MetaRepo, TicketRepo
│   │   ├── vault.py             # TokenVault: authenticated encryption (HMAC-SHA256 CTR + tag)
│   │   └── gist_backup.py       # GistBackup: write-through, restore chain, lease
│   ├── github/
│   │   ├── client.py            # GitHubClient: one REST wrapper (retries, timeouts, typed errors)
│   │   ├── accounts.py          # WorkerPool: 3 worker accounts, persistent round-robin
│   │   ├── secrets.py           # seal_secret() via PyNaCl (no gh CLI)
│   │   ├── repos.py             # RepoProvisioner: create/upload/protect/rename/delete
│   │   ├── workflows.py         # WorkflowDispatcher: dispatch/cancel/list by (owner, repo)
│   │   └── control_queue.py     # ControlQueue: control_<ALT_ID>.json protocol (sender-compatible)
│   ├── discord/
│   │   ├── channels.py          # ChannelContext classifier (public/customer/vip/admin/unknown)
│   │   ├── forums.py            # ForumProvisioner: category, forum, threads, WEBHOOKS, read-only
│   │   ├── embeds.py            # pure embed builders (dict-based, testable without discord)
│   │   └── replies.py           # Reply value object + send helpers (ephemeral by default)
│   ├── security/
│   │   ├── roles.py             # Tier enum, resolve_tier(user_id, customer)
│   │   ├── policy.py            # COMMAND_TIERS, CHANNEL_MATRIX, denial texts (single source)
│   │   ├── guards.py            # Guard.check(ctx, command) → Decision; MultiSig
│   │   └── redact.py            # secret masking for logs/alerts
│   ├── telemetry/
│   │   ├── heartbeat.py         # parse heartbeat embed/JSON → Heartbeat dataclass
│   │   ├── fleet_state.py       # FleetState: per-alt live telemetry + typed logs (in-memory)
│   │   └── ingest.py            # WebhookIngestor: thread-id → (customer, alt) routing
│   ├── timers/
│   │   ├── expiry.py            # ExpiryEngine.scan(now): reminders 7/3/1 + shutdown (pure)
│   │   ├── renewal.py           # LimitlessRenewer.tick(now): 48 h re-dispatch (pure)
│   │   └── scheduler.py         # asyncio periodic runner (the only clock consumer)
│   ├── services/
│   │   ├── container.py         # Services dataclass (what commands receive)
│   │   ├── customers.py         # CustomerService: activate/extend/deactivate/reactivate
│   │   ├── alts.py              # AltService: register, credentials, channels, ownership
│   │   ├── runs.py              # RunService: validate → dispatch/stop/pause/resume/tune
│   │   ├── tickets.py           # TicketService: renew, pause-billing, tx-hash ack, policy ack
│   │   ├── alerts.py            # AlertService: admin alerts w/ debounce
│   │   └── bans.py              # BanService: marker detection → rename repo, notify, replacement
│   └── commands/
│       ├── context.py           # CommandContext protocol (wraps Interaction; fakeable)
│       ├── public.py            # /help /getstarted /account
│       ├── customer.py          # /setup /run /stop /pause /resume /tune /channels /deals /status /reply /alt /renew /pause-billing /proofs
│       ├── vip.py               # /vip autoreply, /vip squad
│       ├── admin.py             # /admin … (customer lifecycle, repos, health, backup, reset, shutdown)
│       └── registry.py          # binds handlers to discord.py app_commands (the only discord-UI file)
├── sender/
│   ├── send_ads.py              # battle-tested V6 sender (kept; import shim only)
│   ├── channel_registry.py      # the persistence helper the sender needs (was control_bot/persistence.py)
│   └── workflows/send_ads.yml, self_check.yml
├── workflows/
│   ├── control_bot.yml          # runs python -m adfarm (chunked 24/7)
│   └── sync_to_alts.yml         # pushes sender files to every alt repo listed in the DB export
├── tools/
│   └── migrate_legacy.py        # legacy customers.db → new schema
└── tests/
    ├── conftest.py              # env scrub, temp DB, fakes
    ├── fakes.py                 # FakeGitHub, FakeGist, FakeDiscord, FakeClock
    ├── unit/                    # one test module per source module
    └── integration/             # activate → setup → run → tune → expiry, ban flow, restore flow
```

**Why a package instead of root modules (L-15):** the legacy imports the repo root into
`sys.path` and mixes `customer_manager` with `control_bot.*`. A single package gives one import
root, one entry point and lets `setup.py`/tools import the same code.

**Why the sender is kept (Phase 3 instruction + risk):** the 6 000-line sender encodes years of
anti-detection tuning that cannot be validated offline. It is copied verbatim except for the
import shim; the control plane is designed around its *existing* protocol (Gist command files,
heartbeat embed) so it needs no change.

---

## 3. Command design

### 3.1 Kept (same name, tightened semantics)
| Command | Tier | Change vs legacy |
|---|---|---|
| `/help`, `/getstarted` | public | Generated from the policy table so it can never drift. |
| `/setup` | customer | Same 2-step modal flow; now **registers the alt** (repo ↔ token ↔ channels) so `/run` works immediately (fixes L-1). |
| `/run` | customer | Alt chosen from **the caller's own alts** (autocomplete). Passes the full channel list (up to 10) instead of `channel_1/2`. |
| `/stop`, `/pause`, `/resume` | customer | `alt` is the caller's alt index (1..alt_count); ownership enforced (L-4). Cancels only runs of `send_ads.yml` (L-12). |
| `/tune` | customer | Same options; `alt` required (no "0 means first or all" ambiguity, L-13). |
| `/channels` | customer | Same actions; persists to DB → alt repo secret → live queue, in that order. |
| `/deals` | customer | Same options; per alt. |
| `/status` | customer | Shows **only the caller's alts**; admins get the fleet (L-4). Absorbs `/refresh` (`refresh:true`) and `/dashboard` (`post:true`). |
| `/reply` | customer | Ownership enforced. |
| `/alt` | customer | `overview/logs/clearlogs/runs/selfcheck/remove`; `add/update` removed for customers (alt count is a paid plan attribute; admins register alts). |
| `/renew`, `/pause-billing`, `/proofs` | customer | Unchanged behaviour; ticket routing via DB meta. |
| `/vip autoreply` | vip | Unchanged. |
| `/admin …` | admin | See 3.4. |

### 3.2 Removed
| Command | Reason |
|---|---|
| `/shutdown` (customer tier) | A customer could kill the whole platform (L-3). Replaced by `/admin shutdown-bot` (multi-sig). |
| `/script` | Arbitrary Python execution inside the control-bot runner has no customer value and a large blast radius; the sandbox is best-effort. |
| `/refresh`, `/dashboard` | Folded into `/status` options — one place to look at telemetry. |
| `/reset` (top level) | Moved to `/admin reset confirmation:RESET` so all destructive admin actions live in one, admin-room-only group. |
| `/alt action:add|update` | Alt provisioning is an admin/plan action (`/admin alt add`); customers supply credentials via `/setup`. Removes the global 1–4 slot model (L-2, L-5). |

### 3.3 Combined
* `/squad` → `/vip squad` (all VIP features under one group; visible only to VIPs).
* `/admin repo-sync` + `/admin repo` → `/admin repo action:list|sync|delete`.
* `/admin sweep-alts` → `/admin health` (worker tokens + gist + lease + stale repos in one report).

### 3.4 Added
| Command | Tier | Why |
|---|---|---|
| `/account` | customer | Self-service view: plan, days left, alts, policy-ack, ticket links. Removes "contact an admin to know my expiry". |
| `/admin customer user:@` | admin | Detail card (replaces reading the DB). |
| `/admin alt action:add|remove|list user:@` | admin | Register an extra alt for a customer (creates repo under a worker, stores mapping). |
| `/admin health` | admin | Worker PAT validity/expiry, Gist backup age, lease holder, unreachable repos. |
| `/admin backup action:now|status` | admin | Force a backup / show restore chain — the legacy had no manual trigger. |
| `/admin shutdown-bot confirmation:SHUTDOWN` | admin (multi-sig) | Replaces customer `/shutdown`. |

Final top-level set: `help getstarted account setup run stop pause resume tune channels deals status reply alt renew pause-billing proofs vip admin` (18 top-level; `vip` has 2 sub-commands, `admin` has 17).

---

## 4. State model

| State item | Source of truth | Mirrors / caches | Sync rule |
|---|---|---|---|
| Customers (plan, expiry, VIP, forum ids) | `adfarm.db` table `customers` | Gist `adfarm.db.b64` | Write-through after every committed transaction (coalesced ≤ 5 s). |
| **Alts** (customer_id, alt_index, sender ALT_ID, repo owner/name, discord user id, username, status, channels) | `adfarm.db` table `alts` | Alt-repo secrets `USER_TOKEN`, `CHANNEL_IDS`, repo variables `ALT_ID`, `ALT_NAME` | DB first; then push to GitHub; a failed push marks `alts.sync_state='dirty'` and is retried by the scheduler. **No more registry in core secrets** (L-5). |
| Alt tokens | Alt-repo secret (sealed) | `alts.token_ciphertext` (authenticated encryption, needed for re-provisioning after bans) | Both written in the same service call; DB copy only if `TOKEN_VAULT_KEY` is set. |
| Per-customer webhooks | `customer_webhooks` table (URL encrypted) | Alt-repo secrets `*_WEBHOOK_URL` | Created once per forum by `ForumProvisioner`; re-pushed on every alt registration (L-6). |
| Run state (mode, hours, payload, last dispatch, renewals) | `runs` table | GitHub run status (polled) | Polled every 60 s into `FleetState`; never authoritative. |
| Live telemetry (heartbeat, counters, logs) | **Sender** (heartbeat) | `FleetState` in memory | Routed by `(thread_id → customer, payload.alt_id → alt)`; rebuilt on boot from the last 50 messages of each dashboard thread. |
| Runtime overrides (price, paused, keywords …) | Control Gist `control_<ALT_ID>.json` | `alts.runtime_overrides` JSON (last command) | Queue write + DB write in the same call; the sender acks into the Gist file. |
| Worker round-robin cursor | `meta.worker_cursor` | — | Persisted, so distribution survives chunk restarts (L-11). |
| Ticket channel, dashboard message ids | `meta` table | — | Survives chunk hand-offs (L-8). |
| Events / audit ledger | `events` table | `#audit-logs` channel | Append-only. |
| Policy acks | `policy_acks` | — | Versioned. |
| Sender blacklist / channel registry | Sender-owned Gists | — | Untouched by the control plane. |

**Reconciliation:** a `SyncSweeper` job (every 15 min) re-pushes `dirty` alts, verifies repos still
exist (marks `alts.status='missing'` instead of deleting), and refreshes worker token health.

---

## 5. Security model

### 5.1 Tiers & command policy (`security/policy.py`)
```
PUBLIC   = {help, getstarted}
CUSTOMER = PUBLIC   ∪ {account, setup, run, stop, pause, resume, tune, channels, deals,
                        status, reply, alt, renew, pause-billing, proofs}
VIP      = CUSTOMER ∪ {vip}
ADMIN    = everything ∪ {admin}
```
Tier resolution: `admin` if `user_id ∈ settings.owner_ids`; else from the customer row
(`active and expiry > now`, `vip`). Empty owner list ⇒ nobody is admin (fail closed).

### 5.2 Channel-aware matrix (`discord/channels.py`)
Channels are classified by *ids first* (forum/thread ids stored in the DB), then by configured
names, then by parent category:

| context | how detected |
|---|---|
| `customer_hub(customer_id)` | thread's parent forum id ∈ `customers.forum_id` |
| `admin` | channel id ∈ settings.admin_channel_ids or name ∈ ADMIN_CHANNELS |
| `public` | name ∈ PUBLIC_CHANNELS |
| `ticket` | id == meta.ticket_channel_id or name ∈ TICKET_CHANNELS |
| `dm` | no guild |
| `unknown` | anything else |

| tier of command → | public | ticket | own hub | other hub | admin | dm | unknown |
|---|---|---|---|---|---|---|---|
| public | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| customer | ❌ | ✅ (`renew`, `pause-billing`, `proofs`, `account` only) | ✅ | ❌ | ✅ | ❌ | ❌ |
| vip | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ |
| admin | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |

Admins bypass the channel gate **except** for admin-tier commands (they must be issued in an
admin room so the audit trail is in one place). Decision is computed by a pure function
`decide(tier_of_user, command, context, owner_of_context)` → `Decision(allowed, reason)`; the
gate **fails closed** (exception ⇒ deny with a generic message, L-14).

### 5.3 Ownership
`AltService.resolve_for(actor, alt_index, customer_id=None)`: customers may only pass their own
`alt_index`; admins must pass `customer`. There is no code path that turns an integer into an
alt without a customer id. Interactive components carry `(customer_id, alt_index)` in their
custom_id and re-check the actor on every click.

### 5.4 Secrets
* Bot/GitHub tokens: env only; `Settings.redacted()` for logs; `security/redact.py` masks anything that looks like a token in alerts.
* Alt tokens: sealed with the repo public key (PyNaCl) — the *only* path; the `gh` CLI is not used (L-10). The DB copy uses `TokenVault` (HMAC-SHA256 keystream + HMAC tag, keyed by `TOKEN_VAULT_KEY` through PBKDF2). If the key is missing the copy is skipped, never stored plaintext or XOR'ed (L-9).
* Webhook URLs are secrets too (anyone with the URL can post into a customer's forum) → stored through the vault.

### 5.5 Destructive actions
`/admin deactivate`, `/admin alt remove`, `/admin repo delete`, `/admin reset`, `/admin shutdown-bot`
all require a typed confirmation; `reset` and `shutdown-bot` additionally require a second admin
within 120 s (`MultiSig`). Every admin action writes an `events` row and an `#audit-logs` line.

---

## 6. Architecture diagram

```
                        ┌──────────────────────────── Discord guild ────────────────────────────┐
                        │ public rooms   ticket room   admin rooms   🏢 Customer Hub/forum-N     │
                        │                                               threads: control dashboard│
                        │                                               farm-logs deals dm-inbox │
                        └───────▲──────────────▲───────────────▲──────────────▲──────────────────┘
        slash commands / views  │              │ alerts/audit  │ webhooks     │ heartbeats/logs/deals/DMs
                                │              │               │ (per thread) │
┌───────────────────────────────┴──────────────┴───────────────┴──────────────┴─────────────────┐
│  adfarm (control bot, main GitHub account, Actions chunk)                                       │
│                                                                                                 │
│  commands/*  ──►  security.Guard ──►  services/*  ──►  db.repositories ──► SQLite adfarm.db     │
│   (thin)            (tier+channel+        │                 │                    │ write-through │
│                      ownership)           │                 │                    ▼               │
│                                           │                 │            db.GistBackup ──► Gist  │
│  telemetry.ingest ◄── on_message          │                 │            (current/prev/meta/LOCK)│
│        │                                  ▼                 ▼                                   │
│        ▼                        github.WorkflowDispatcher  github.ControlQueue ──► control Gist │
│  telemetry.FleetState           github.RepoProvisioner     (control_<ALT_ID>.json)              │
│        ▲                        github.WorkerPool (3 PATs, cursor in DB)                        │
│        │ poll 60 s                        │                                                     │
│  timers.scheduler ─► ExpiryEngine, LimitlessRenewer, SyncSweeper, HealthMonitor                 │
└──────────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                           │ REST (dispatch / secrets / contents)
                 ┌─────────────────────────┼─────────────────────────┐
                 ▼                         ▼                         ▼
        worker-1 account           worker-2 account           worker-3 account
        <user>_alt1 (public)       …                          …
        send_ads.py + workflows    each repo: secrets USER_TOKEN, CHANNEL_IDS, *_WEBHOOK_URL,
        vars ALT_ID, ALT_NAME      GIST_*, CONTROL_GIST_ID, CONTROLLER_USER_IDS
                 │
                 └── runner: WARP egress → Discord self-bot posting → webhooks back to the forum
```

State boundaries: **DB** (durable truth) · **Gist** (backup + command bus) · **GitHub secrets**
(delivery of credentials to runners, never read back) · **memory** (telemetry only).
External dependencies: Discord API/gateway, GitHub REST, GitHub Gists, Cloudflare WARP (runner
only), ipwho.is/ipinfo (runner only).

---

## 7. Testing strategy

| Layer | Approach | Mocked |
|---|---|---|
| `core`, `security`, `timers`, `telemetry.heartbeat` | Pure unit tests, table-driven | nothing |
| `db` | Real SQLite in `tmp_path`; migrations applied from scratch and from v1; vault round-trips; Gist backup against `FakeGist` (in-memory dict with revisions) | HTTP |
| `github` | `FakeGitHub` implements the small `GitHubTransport` protocol (`request(method, path, json)`) with an in-memory model of repos/secrets/runs/gists; asserts sealed secrets are base64 and never equal to plaintext | network |
| `services` | Real services + real DB + fakes for GitHub/Discord/clock | external systems |
| `commands` | Handlers called with `FakeContext` (user id, channel, options) → assert `Reply` text/embeds and side effects | discord.py |
| `discord.forums` | `FakeGuild` with create_forum/create_thread/create_webhook capturing calls | discord.py |
| integration | `tests/integration`: activate → setup → run → tune → heartbeat → expiry shutdown; ban → rename → replacement; crash → restore from Gist; lease conflict | external systems only |

Non-destructive guarantees: `conftest.py` scrubs every credential env var, points the DB to
`tmp_path`, and the fakes refuse any URL that is not `fake://`. No test touches the network,
the filesystem outside `tmp_path`, or the legacy tree.

---

## 8. Migration strategy

### 8.1 Steps
1. **Shadow deploy** — add `workflows/control_bot.yml` as `control_bot_v2.yml` (manual trigger), same secrets plus `ADFARM_DB=adfarm.db`, `ADFARM_GIST_ID=<new gist>`. Bot token: create a *second* Discord application for staging so both bots can coexist in the guild (different command sets).
2. **Data import** — `python -m tools.migrate_legacy --from customers.db --to adfarm.db`: customers → customers; `alt_credentials` + `customers.repos` → `alts` (one row per index, `repo_owner=github_account`, sender `ALT_ID` newly assigned); `run_state` → `runs`; `policy_acks`, `events`, `meta` copied. Dry-run prints the mapping.
3. **Webhook back-fill** — `/admin health` lists customers without webhooks; `/admin alt sync user:@` (re)creates thread webhooks and pushes them to the alt repos.
4. **Parallel run (≥ 3 days)** — v2 bot observes heartbeats (read-only) while the legacy bot still dispatches; compare `/status` outputs.
5. **Cut-over** — disable `control_bot.yml`, enable v2 cron, swap Discord application (or re-invite), run `/admin health`, `/admin backup action:now`.
6. **Decommission** — after 14 days of green weekly summaries, archive legacy modules (`legacy/` folder) and remove the legacy workflows.

### 8.2 Risks & mitigations
| Risk | Mitigation |
|---|---|
| Two bots answering the same command names | Different application ids; v2 registers commands only when `ADFARM_REGISTER_COMMANDS=1`. |
| Customers lose live runs during cut-over | Cut-over does not touch runner workflows; `runs` import preserves `limitless` renewals. |
| Legacy fleet alts (`ALT_REPOS`) not in the DB | Importer accepts `--alt-repos` to map operator-owned alts to an "operator" customer row. |
| Gist quota | New Gist for v2; the legacy Gist is left untouched for rollback. |
| Sender protocol drift | Sender is byte-identical; the control queue writes the same file names/keys. |

### 8.3 Rollback
Re-enable `control_bot.yml`, disable v2 cron, restore `customers.db` from the legacy Gist (it was
never modified). No data in the v2 DB is required by the legacy bot. Time to roll back: one
workflow toggle (< 5 min).
