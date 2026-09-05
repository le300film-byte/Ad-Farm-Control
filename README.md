# AdFarm V9 — control plane (`new_reform/`)

The reformed AdFarm control bot. A Discord-operated SaaS that runs "ad farms": customers hand
over Discord alt-account tokens, the operator's infrastructure posts marketplace ads from those
alts 24/7 with anti-detection behaviour, relays buyer DMs back to the customer, and bills monthly
via manual crypto (BEP-20).

This folder (`new_reform/`) is a **clean-room reimplementation** of the legacy system in the
repository root. It keeps the battle-tested `send_ads.py` sender **byte-for-byte** and replaces
the fragile, monolithic bot with a modular, tested control plane. **The legacy code in the root
is not modified by anything here.**

```
new_reform/
├── README.md            ← you are here
├── ARCHITECTURE.md      ← component map + data flow
├── SKILL.md             ← AI-operator skill (command reference)
├── setup.py             ← idempotent installer (REST only, --dry-run, --non-interactive)
├── requirements.txt
├── pytest.ini
├── analysis/            ← 01 description, 02 redesign, 04 comparison (Phase docs)
├── adfarm/              ← the control-bot package (python -m adfarm)
├── sender/              ← send_ads.py (V6, verbatim) + channel_registry + workflows
├── workflows/           ← control_bot.yml (install as .github/workflows/)
├── tools/               ← migrate_legacy.py (legacy customers.db → adfarm.db)
└── tests/               ← unit/ + integration/ (real SQLite + fakes, no network)
```

## 1. Requirements

* Python ≥ 3.11
* `discord.py>=2.4.0` (runtime; the control bot entry point imports it)
* `PyNaCl>=1.5.0` (sealing GitHub repo secrets)
* `requests>=2.31.0` (GitHub REST transport)
* `pytest>=8.0` (only for the test suite)

```bash
pip install -r requirements.txt
```

## 2. Configuration

All configuration is read **once** from the environment by `adfarm.config.Settings.from_env()`
(optionally merged with a JSON blob in `TUNING_JSON`). The bot **refuses to start** (exit 2) if
`BOT_TOKEN` or `OWNER_IDS` is missing, and logs every other misconfiguration via
`Settings.problems()` so problems surface at boot, not at the first command.

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Discord bot token (Message Content + applications.commands intents) |
| `GUILD_ID` | Discord server id |
| `OWNER_IDS` | Comma-separated admin Discord user ids (fail-closed if empty) |
| `ADMIN_ALERTS_CH_ID` / `ADMIN_CHAT_CH_ID` / `AUDIT_LOG_CH_ID` | Admin room ids |
| `OPEN_TICKET_CH_ID` | Ticket room id |
| `CUSTOMER_HUB_ID` | 🏢 Customer Hub category id |
| `PAYMENT_ADDRESS` | BEP-20 wallet address shown on tickets |
| `GH_TOKEN` | Main-account PAT (`repo`, `workflow`, `gist`) |
| `CORE_REPO` | `owner/repo` of this core repo (defaults to `GITHUB_REPOSITORY`) |
| `WORKER_TOKENS` | `login:token,login:token` for the 3 worker accounts |
| `WORKER_1_USER`/`_TOKEN`, `WORKER_2_*`, `WORKER_3_*` | Alternative per-worker env form |
| `CONTROL_GIST_ID` | Gist holding `control_<ALT_ID>.json` command bus |
| `ADFARM_GIST_ID` | Backup Gist for `adfarm.db` (write-through) |
| `GIST_TOKEN` | Gist-write token (defaults to `GH_TOKEN`) |
| `TOKEN_VAULT_KEY` | ≥8-char key to encrypt alt tokens at rest in the DB |
| `ADFARM_DB` | Path to `adfarm.db` (defaults to `customers.db` for an easy cut-over) |
| `ADFARM_REGISTER_COMMANDS` | `true` to sync slash commands (set on chunk 1 only) |

Full list with defaults: `adfarm/config.py`.

## 3. Running

```bash
# Local / dev
export BOT_TOKEN=... OWNER_IDS=... GH_TOKEN=... ADFARM_GIST_ID=... CONTROL_GIST_ID=...
python -m adfarm

# Or as a GitHub Actions service (recommended — 8×350-min chunks, ~46 h coverage)
# Install new_reform/workflows/control_bot.yml as .github/workflows/control_bot.yml
# and set the secrets above on the repo.
```

On boot the bot:
1. builds the service graph (`app.build_services`);
2. restores `adfarm.db` from the backup Gist if the local copy is missing/empty;
3. acquires the DB lease (exits if another runner holds it — split-brain prevention);
4. syncs slash commands (chunk 1 only);
5. starts the scheduler (expiry reminders, limitless renewal, run polling, dirty-sweep,
   stale detection, lease renewal).

## 4. Installing (optional)

`new_reform/setup.py` is an idempotent installer that uses only the GitHub REST API:

```bash
python -m tools.  # (no-op placeholder — run the file directly)
python setup.py --dry-run                 # print what it would do
python setup.py                            # create backup gist, set repo secrets, init DB
python setup.py --push-workflows          # also upload control_bot.yml + sender files
python setup.py --discord                 # also create Discord channels/forum + register commands
```

It never deletes or recreates the backup Gist, and all writes are `--dry-run`-safe. Discord-side
steps require `discord.py` and are skipped gracefully when it is not installed.

## 5. Migrating from the legacy system

The new DB schema (`adfarm.db`) is intentionally different from the legacy `customers.db`.
`tools/migrate_legacy.py` imports it:

```bash
python -m tools.migrate_legacy \
    --from customers.db --to adfarm.db \
    --legacy-vault-key "$LEGACY_TOKEN_VAULT_KEY" \
    --vault-key "$TOKEN_VAULT_KEY" \
    --alt-repos "200000000000000001:worker1/alice_alt1,worker1/alice_alt2" \
    --dry-run
```

The importer maps `customers`, `alt_credentials`, `run_state`, `reminder_sent`,
`policy_acks`, `events` and `meta`. Legacy repo names are best-effort split into
`repo_owner`/`repo_name`; `--alt-repos` overrides them when the legacy list is unreliable.
Tokens are de-obfuscated with the *legacy* key and re-sealed with the *new* `TokenVault`.

Full migration runbook (shadow deploy → import → webhook backfill → parallel run → cut-over →
decommission): `analysis/02_REDESIGN.md` §8 and `analysis/04_COMPARISON.md`.

## 6. Commands

See `SKILL.md` for the full operator reference. Top-level:
`help getstarted account setup run stop pause resume tune channels deals status reply alt
renew pause-billing proofs vip admin`.

## 7. Development & testing

```bash
pip install -r requirements.txt
python -m pytest -q          # unit + integration; uses real SQLite + fakes, never the network
```

* `core`, `security`, `timers`, `telemetry.heartbeat` — pure, no mocks.
* `db` — real SQLite in `tmp_path`; Gist backup against an in-memory `FakeGist`.
* `github` — `FakeGitHubTransport` models repos/secrets/runs/gists; asserts sealed secrets are
  base64 and never equal to plaintext.
* `services` / `commands` — real services + real DB + fakes for GitHub/Discord/clock.
* `integration/` — activate → setup → run → tune → heartbeat → expiry shutdown; ban → rename →
  replacement; crash → restore from Gist; lease conflict.

## 8. What is intentionally unchanged

`new_reform/sender/send_ads.py` is the same V6 sender as the root. The control plane talks to it
through its **existing** protocol (GitHub repo secrets/variables + the `control_<ALT_ID>.json`
Gist command bus + the heartbeat embed), so no sender changes are required for cut-over.
