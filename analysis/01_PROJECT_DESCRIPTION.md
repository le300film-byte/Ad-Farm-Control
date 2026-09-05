# AdFarm V8 — Complete System Description (Phase 1)

> Produced by reading every file of the legacy repository at commit `f06d8ab` ("legacy reform").
> Line numbers refer to that commit. Nothing in this document is inferred from docs alone;
> every statement was verified against source. Where the docs and the code disagree, the
> code wins and the discrepancy is listed in §13.

---

## 1. Project Overview

**What it is.** A Discord-operated SaaS for running "ad farms": customers hand over Discord
*alt* account tokens; the operator's infrastructure posts marketplace buy/sell advertisements
from those alts into trading channels, 24/7, with anti-detection behaviour, and relays buyer
DMs back to the customer.

**Who uses it.**
| Actor | Description |
|---|---|
| **Admin / founder** | Discord user IDs in `OWNER_IDS`. Own the GitHub *main* account that hosts the core repo, the control bot and the backup Gist. Approve crypto payments, activate/extend/deactivate customers. |
| **Customer** | Paying Discord user with a row in `customers.db` and `active=1`, `expiry_date>now`. Gets a private forum in the "🏢 Customer Hub" category. |
| **VIP customer** | Customer with `vip=1`. Extra: `#dm-inbox` thread, `/vip autoreply`, `/squad`, `/script`. |
| **Worker GitHub accounts** | Up to 3 GitHub accounts (`WORKER_{1..3}_USER/TOKEN` or `WORKER_TOKENS`) that *own* the customer alt repositories (public → free Actions minutes). |
| **Alt account** | A Discord user account (self-bot) whose token the customer supplies. One alt = one GitHub repo running `send_ads.py`. |

**Business problem.** Manual ad posting is slow and gets accounts banned. The product sells
automated, human-like posting with monitoring, deal scanning and DM relay, billed monthly via
manual BEP-20 crypto payments.

**High-level architecture.**
```
Discord guild ("control server")
 ├─ public rooms (welcome-about, pricing-plans, announcements, open-ticket, proofs)
 ├─ staff rooms  (admin-commands, admin-alerts, admin-chat, audit-logs)
 └─ 🏢 Customer Hub category → one private forum per customer
      threads: control · dashboard · farm-logs · deals · dm-inbox(VIP)
                 ▲ slash commands / webhooks / heartbeats
                 │
   ┌─────────────┴──────────────┐
   │ control bot (python -m control_bot) — GitHub Actions, main account,   │
   │ 8×350-min chained chunks, discord.py, SQLite customers.db,            │
   │ Gist write-through backup + run lease                                  │
   └─────────────┬──────────────┘
                 │ workflow_dispatch / secrets / Contents API / Gist queue
   ┌─────────────┴──────────────┐
   │ worker accounts (3) → customer alt repos (public)                      │
   │   send_ads.py + send_ads.yml + self_check.yml                          │
   │   runner: WARP egress → self-bot posting → webhooks back to Discord    │
   └────────────────────────────┘
```

---

## 2. Architecture

### 2.1 Components and responsibilities

| Component | File(s) | Runs where | Responsibility |
|---|---|---|---|
| Control bot | `control_bot/bot.py` (5 831 lines), `control_bot/*.py` | GitHub Actions `control_bot.yml` on the main account | Slash commands, dashboard rendering, heartbeat ingestion, GitHub dispatch, DB/Gist lifecycle, timers, ops monitors |
| Customer layer (V8) | `customer_manager.py`, `security.py`, `admin_commands.py`, `discord_forum.py`, `timer_engine.py`, `github_dispatch.py`, `gist_backup.py` | imported by the bot (root of repo added to `sys.path`) | Multi-customer DB, tiers/channel gates, `/admin`, forum provisioning, expiry timers, worker-account repo ops, DB backup |
| Sender | `send_ads.py` (6 200 lines, "V6.0") | Alt repo, `send_ads.yml` matrix chunks | Self-bot posting engine; polls Gist control queue; pushes heartbeats/logs/DMs/deals via webhooks |
| Workflows | `.github/workflows/*.yml` | GitHub Actions | `control_bot.yml` (24/7), `send_ads.yml` (runner), `self_check.yml` (preflight), `bootstrap.yml`, `sync_to_alts.yml`, `control_bot_staging.yml` |
| Installer | `setup.py` | operator laptop / `bootstrap.yml` | Creates Discord channels, Gist, secrets, DB |
| Tests | `tests/` (16 files, 6 231 lines) | pytest | 314 pass / 9 skip |

### 2.2 Data flow (command → alt)

1. Customer runs `/run` in their `#control` thread → `_check_perms` (channel gate → role gate → cooldown) → `RunStartView` (3-step ephemeral wizard) → `_execute_run_dispatch` (bot.py:2486).
2. Bot cancels the latest run in the alt repo (`github_api.cancel_run`) then `POST /repos/{owner}/{repo}/actions/workflows/send_ads.yml/dispatches` with inputs `ad_type, message, interval_min, total_hours, runtime_limitless, attach_image, channel_1, channel_2, sell_rate|buy_*`.
3. `send_ads.yml` `plan` job computes `chunks=(hours+5)/6 (max 8)`; each chunk (max-parallel 1) enables Cloudflare WARP, verifies the egress IP is residential (ipwho.is + ipinfo; rejects hosting orgs), builds `MSG`, and runs `python -u send_ads.py` with ~60 env knobs from repo secrets.
4. Runtime changes (`/tune`, `/pause`, `/channels add`, `/reply`, …) are written as `control_<ALT_ID>.json` into the private **control Gist** (`github_api.queue_control_command`, bot.py:4936 → github_api.py:113). The sender polls it every `SYNC_GIST_INTERVAL_SEC` (45 s, min 15) and writes an ack back into the same file. DM transport is only a fallback when `CONTROL_GIST_ID` is empty.
5. Sender output flows back through four webhooks: `DASHBOARD_WEBHOOK_URL` (heartbeat embed + `type:"heartbeat"` JSON, every 300 s), `LOG_WEBHOOK_URL` (`#farm-logs`, webhook username = `ALT_NAME`), `DEAL_WEBHOOK_URL`, `DM_WEBHOOK_URL`. The bot's `on_message` → `_handle_guild_webhook_message` (bot.py:5250) parses them into `AltStateManager`.

### 2.3 State locations

| State | Where | Durable? | Notes |
|---|---|---|---|
| Customers, credentials, run state, events, acks, reminders, meta | `customers.db` (SQLite, WAL) | Yes — write-through to Gist after every mutation | `customer_manager._after_write` → `gist_backup.enqueue_backup` |
| DB replica | Backup Gist files `customers.db.b64`, `customers.prev.db.b64`, `db-meta.json`, `LOCK` | Yes | Restored on every boot before first write |
| Fleet alt registry | **GitHub repo secrets** `ALT_REPOS`, `ALT_DISCORD_IDS`, `ALT_NAMES` of the core repo (mutated at runtime by `_persist_alt_registry`, bot.py:1761) | Yes (but opaque) | Read once at import into `config.ALT_REPOS`; capped at IDs 1–4 |
| Live alt telemetry | `AltStateManager` in memory + `.adfarm_control_state.json` (safe subset: name, squad, policy, rate, interval, runtime, tags) | Partially | Rebuilt from last 100 webhook messages per channel on boot (`_hydrate_discord_state`) |
| Per-alt channel registry | `.adfarm_channel_registry.json` + Gist files `channel_state_<alt>.json` | Yes | `ChannelRegistryStore` (persistence.py) |
| Control commands | Control Gist `control_<alt>.json` (+ optional broadcast `control.json`) | Transient | Last command wins; durable keys (deal keywords, paused, rate, …) are preserved across commands |
| Runtime config on the alt | Alt repo secrets (`USER_TOKEN`, `CHANNEL_IDS`, webhooks, `GIST_*`, `CONTROLLER_USER_IDS`) + repo variables `ALT_ID`, `ALT_NAME` | Yes | Set by `provision_alt_repository_files_and_secrets` / `/setup` / `/channels` |
| Dashboard message id | `dash_msg_id.txt` | Per runner | Lost on every chunk hand-off → new pinned dashboard message each chunk |
| Blocked ad variations | Sender Gist `GIST_ID` | Yes | Auto-learn blacklist |

### 2.4 Deployment topology

* **Main account**: core repo (this repository, public), backup Gist, control Gist, `control_bot.yml` cron `0 10 * * *` + `workflow_dispatch`, matrix `chunk: [1..8]`, `max-parallel: 1`, `timeout-minutes: 350`, each chunk `python -m control_bot`. A new run every day replaces the previous (concurrency group `control-bot-24-7`, `cancel-in-progress: true`).
* **Worker accounts**: hold alt repos (`{username}_alt{i}` per `_alt_repo_plan`, admin_commands.py:66). Tokens are fine-grained PATs; the bot picks the token by repo *owner* (`github_api._token_for_repo`).
* **Discord**: one guild (`GUILD_ID`). Commands are copied global→guild and synced on every `on_ready` (`command_sync.sync_guild_commands`), then per-channel visibility is pushed best-effort.
* Egress: runners use Cloudflare WARP (`sebst/actions-warp@v1`) unless `HTTPS_PROXY` is set or `WARP_ENABLED=0`; datacenter IPs abort the run.

---

## 3. Roles & Permissions

### 3.1 Role resolution (`security.py`, `bot.viewer_role`)
```
admin    : user id ∈ OWNER_IDS (env OWNER_IDS or OWNER_ID; empty set ⇒ nobody is admin — fail closed)
vip      : is_active_customer(uid) and customers.vip = 1
customer : is_active_customer(uid)  (active=1 AND expiry_date > now)
public   : everyone else
```
`is_active_customer` reads the DB on every call (no cache). Admins bypass *every* gate
(role, subscription, VIP, channel).

### 3.2 Command tiers (`security.py` — single source of truth, re-exported by bot.py)
| Tier | Commands |
|---|---|
| PUBLIC | `help`, `getstarted` |
| CUSTOMER | `setup run stop pause resume tune channels deals status reply refresh dashboard shutdown alt renew pause-billing proofs` |
| VIP | `squad script vip` |
| ADMIN | `admin reset` |

### 3.3 Channel-context matrix (`security.classify_channel_context` / `enforce_channel_gate`)
Channel names (thread → parent forum → category considered) are classified with env-overridable
lists: `PUBLIC_CHANNELS` (default `welcome-about,pricing-plans,announcements`),
`CUSTOMER_CHANNELS` (`control,dashboard,farm-logs,deals,open-ticket,tickets`),
`VIP_CHANNELS` (`dm-inbox`), `ADMIN_CHANNELS` (`admin-commands,admin-alerts,admin-chat,audit-logs`),
plus `CUSTOMER_HUB_MARKER` (category name substring, default "customer hub").

| Command tier → | public room | customer room | vip room | admin room | DM / unknown |
|---|---|---|---|---|---|
| public | ✅ | ✅ | ✅ | ✅ | ✅ |
| customer | ❌ | ✅ | ✅ | ✅ | ✅ |
| vip | ❌ | ✅ | ✅ | ✅ | ✅ |
| admin | ❌ | ❌ | ❌ | ✅ | ✅ |

Denial text: `❌ This command is not available in this channel.` The gate **fails open** on
exceptions (`enforce_channel_gate` catches and returns True) and admins bypass it.

### 3.4 Denial messages (verbatim)
* not admin: `❌ You are not authorized to use this command.`
* expired: `❌ Your subscription has expired. Contact an admin to renew.`
* never a customer: `❌ You do not have an active subscription. You are not authorized to use this command.`
* not VIP: `❌ This feature requires VIP. Run /admin activate @User vip:true to upgrade.`
* legacy owner gate (non-V8 paths, dashboard buttons): `🔒 You aren't authorized to run control commands.`
* cooldown (owners only, `CMD_COOLDOWN_SEC`=5): `⏱️ Cooldown — wait {n}s.`

### 3.5 Discord-side visibility
`command_sync.visibility_plan` denies customer/vip/admin commands in public rooms and restricts
admin commands to admin rooms using application-command permission overwrites (type 8 = channel).
Failure is logged, never fatal. `/admin` additionally has `default_permissions=administrator`
and `guild_only=True`.

### 3.6 Customer isolation
`_customer_owned_alt_ids(uid)` (bot.py:130) maps fleet alt IDs to a customer by **three heuristics**:
(1) fleet `ALT_REPOS` basename ∈ customer `repos`, (2) `ALT_DISCORD_IDS[alt] == customer.discord_id`,
(3) `/setup` credential username == fleet alt display name. `_visible_alt_ids` returns the whole
fleet for admins. Only `/alt` applies this filter (§13).

### 3.7 Two-person rule
`security.MultiSigConfirm` — `/admin shutdown confirm:ALL` requires a second distinct admin to
confirm within 120 s.

---

## 4. Slash Commands — Complete Inventory

Conventions: all replies are **ephemeral** unless stated. "Gate" = `_check_perms(inter, role, command)`.
`alt` parameters are plain integers (no autocomplete) unless a modal/select is used.

### 4.1 Public

#### `/help`
* **Description:** "V8 Command Reference — complete guide to all commands, arguments, and features."
* **Who/Where:** everyone, anywhere. No gate call; role decides content.
* **Params:** none.
* **Output:** paginated embeds (categories: Getting Started, Ad Farm Controls, Channels & Deals, Alt Management, VIP Features, Billing & Proofs, Admin Panel, System). Only commands in `commands_for_role(role)` are listed; when the V8 stack failed to import, every registered command is shown.
* **Behavior:** `_COMMAND_GUIDE` dict (bot.py:3931) holds usage + description per command.

#### `/getstarted`
* **Description:** "Quick-Start Guide — step-by-step V8 checklist from policy acceptance to first ad run."
* **Who/Where:** everyone. Active customers receive a short "✅ Already set up" embed instead.
* **Output:** 9-field embed (policy → pay → /setup → /run → monitor → /tune → ban → /renew → docs).

### 4.2 Customer tier

#### `/setup`
* **Description:** "V8 Setup Wizard: enter your alt tokens and channels to get started."
* **Gate:** customer. Requires a customer row (`❌ No customer record found…`) and `discord_forum.assert_forum_owner` (only the forum owner may configure).
* **Flow:** `SetupCountModal` ("How many alts? 1–min(4, alt_count)") → for each alt a `_NextAltButton` (300 s) opens `SetupAltModal` (token ≤100 chars, channel IDs paragraph ≤500 chars). Validation: ≤10 channel IDs (`MAX_CHANNELS_PER_ALT`), each 10–20 digits, token checked against `GET discord.com/api/v10/users/@me` in a thread.
* **Finalize (`_finalize_setup`):** for each valid alt `i` with `repos[i-1]` existing: `github_dispatch.set_repo_secret(owner, repo, "USER_TOKEN", token)`, `set_repo_secret(..., "CHANNEL_IDS", csv)`, `customer_manager.store_alt_credential(uid, i, token, channels, username)`; then `results.clear()` (memory hygiene), `record_event("setup_completed")`, `✅ Setup complete! Run /run…`.
* **Does NOT:** register the alt in the fleet registry (`ALT_REPOS`/`AltStateManager`) — see §13.1.

#### `/run`
* **Description:** "Launch Ad Run — pick alt, enter ad text, preview & confirm dispatch".
* **Gate:** customer; requires `state.alt_ids` non-empty and `GH_TOKEN` set.
* **Flow:** `RunStartView` (step 1: alt select, mode sell/buy, interval 3/5, runtime 6/12/18/24/48/0=∞ Limitless; step 2 modal: raw message ≤1900, optional rate 0<r≤20, image yes/no; step 3 preview + "Confirm Launch"). `_validate_run_values` (bot.py:1602) enforces: alt configured, mode ∈ {sell,buy}, rate/rap optional but bounded, message required unless a rate lets it synthesize `💸 Selling at {rate}/1K — DM me!`, buy_style ∈ {simple,detailed}, interval ∈ {3,5}, hours ∈ {0,6,12,18,24,48}, attach_image ∈ {yes,no}.
* **Dispatch:** `_execute_run_dispatch` re-checks `_is_operator`; channels = alt's live channels → any other alt's channels → `CHANNEL_IDS` fleet default; passes only `channel_1`/`channel_2`; cancels latest run; dispatches; `state.set_workflow(queued)`, `state.set_run_config`, `customer_manager.record_run_state(uid, alt, mode limitless|timed, hours, payload)`; logs to `#control`.
* **Output:** `🚀 **{alt}** queued privately: sell · 5min × 24h · image=no` + run id.
* **Note:** the ∞ Limitless preview shows "runs for a maximum of 48 hours per dispatch" and the timer engine re-dispatches (§8).

#### `/stop alt:<int>`
* Gate customer. Sends `!stop` through the control queue (`_send_control_wait_ack`, 15 s), then `github_api.cancel_run(alt)`; sets workflow state cancelled; logs. Documented SLA ≈30–45 s.

#### `/pause alt:<int>` / `/resume alt:<int>`
* Gate customer. `_finish_dm_control(inter, alt, "!pause"|"!resume", label)`: queues command, reports ack, appends CONTROL log. Queue payload also sets `paused: true|false` (durable key).

#### `/alt [action] [alt] [confirmation] [delete_repository] [limit] [kind] [search]`
* Choices: `overview add update remove logs clearlogs runs selfcheck`; `kind ∈ ALL ERROR DEAL CONTROL CHANNEL CAUTION DEBUG`.
* Gate customer; `_visible_alt_ids` restricts non-admins to their own alts (empty → "👥 Your Alt Accounts … Run /setup" embed listing provisioned repos).
* `add` → `AltAddModal` (token, name, alt id 1–4, repo, channels): validates token via `/users/@me`, resolves first free slot in {1,2,3,4}, repo `{worker}/{username}` round-robin via `github_dispatch.pick_worker`, `provision_alt_repository_files_and_secrets` (uploads `send_ads.py`, both workflows, sets `USER_TOKEN`, `CHANNEL_IDS`, webhook URLs, `GIST_ID/TOKEN`, `CONTROL_GIST_ID`, `CONTROLLER_USER_IDS`), then `_persist_alt_registry` (**writes ALT_REPOS/ALT_DISCORD_IDS/ALT_NAMES core-repo secrets**) and `state.add_alt`.
* `update` → `AltUpdateModal` (name/repo/token). `remove` requires `confirmation:DELETE`, optionally deletes the repo; goes through `_drop_alts_from_everywhere`.
* `logs` → last ≤50 typed log lines; `clearlogs`; `runs` → recent workflow runs; `selfcheck` → dispatches `self_check.yml`.
* Default → `AltControlHubView` interactive hub.

#### `/tune [alt=0] [policy] [price] [mode] [message] [interval] [runtime] [image]`
* Choices: policy `stealth|aggressive|peak_hour|balanced`; mode `sell|buy`; interval `3|5`; runtime `6|12|18|24|48`.
* No params → `FleetTuningView`. `image` → validates png/jpeg/webp ≤8 MB and uploads `ad_image.png` via Contents API to the alt repo(s). `policy` → `state.set_policy_template` + `!policy X` (queue expands into interval/deal settings). `price` → `_extract_price`, 0<p≤20 → `!setprice`. `message` ≤1900 → `!setmessage`. `interval` → `!setinterval`. `runtime` → `!setruntime`.
* `alt=0` means "first alt" for single-value tunes and "all alts" for policy/image. **No ownership check** (§13).

#### `/channels [alt=1] [action=view] [channel_id] [new_channel_id] [name]`
* Actions `view list add replace remove overwrite refresh rescan reset_caution`.
* `add`: numeric id, cap 10, `!setchannel <id> [label]` + `state.set_channel` with `_rollback_capable_update` (restores previous state if remote failed). `replace`: `!replacechannel old new`. `remove`: `!removechannel`. `overwrite`: comma list (ASCII/full-width commas) ≤10 → `!setchannels` and `set_repository_secret(repo, "CHANNEL_IDS")`. `refresh`/`rescan`: `!rescan` + registry comparison (`_reconcile_control_channels`). `reset_caution [id|all]` → `!resetcaution`.
* Default → `ChannelsView` with yield grades.

#### `/deals [alt=0] [enabled on|off] [min_delta] [keywords] [sample_listing]`
* `sample_listing` → dry-run parser; `enabled` → `!setdealscan`; `min_delta` 0–5 → `!setdealdelta`; `keywords` ≤20 items, each ≤60 chars, total ≤500 → `!setdealkeywords`; default `DealsHubView`.

#### `/status [alt=0]`
* Calls `_fresh_state()` (GitHub run refresh + webhook re-hydration) then `build_all(state)` (3 embeds: summary, channels, alerts) or `build_single_alt_embed`. **Shows the whole fleet to any customer** (§13).

#### `/reply [alt=1] [user] [text]`
* No text → `BuyerReplyModal`. Else validates alt ∈ fleet, digits-only buyer id, non-empty text → `!reply <uid> <text>`; reply `📤 Reply Queued…`.

#### `/refresh` — `_fresh_state()` + `_refresh_dashboard_now()`; `✅ … refreshed`.
#### `/dashboard` — posts a fresh 3-embed snapshot with `DashboardControlView` to `DASHBOARD_CH_ID`.

#### `/shutdown confirmation:SHUTDOWN`
* **Gate customer (!)**: any active customer can stop every alt, cancel every workflow, cancel the control bot's own run (`GITHUB_RUN_ID`) and `bot.close()` (§13.3).

#### `/renew`
* Posts `🔄 Renewal request — customer {uid} … Days remaining: N` into the ticket channel resolved by `tickets.resolve_ticket_channel_id` (env `OPEN_TICKET_CH_ID`/`TICKET_CH_ID` → DB meta `open_ticket_ch_id` → guild channel named `open-ticket|tickets|ticket|support-tickets`), notifies `ADMIN_ALERTS_CH_ID`, replies `✅ Your renewal ticket was opened…`.

#### `/pause-billing`
* Same resolver; posts `⏸️ Pause-billing request …`; admin is expected to run `/admin extend`.

#### `/proofs`
* Sends `ProofsView` → `ProofModal` (5–400 chars) → `proofs.publish_proof` to `PROOFS_CH_ID` with the customer snowflake replaced by `` `1234…` ``. Requires `is_active`.

### 4.3 VIP tier

#### `/squad [action=overview] [squad_name] [alt=0] [value]`
* Actions `overview list view assign pause resume policy price`. `assign` → `state.set_squad`. Batch actions send `!pause|!resume|!policy v|!price v` to each member with `random.uniform(0.25,1.25)` stagger and update local state. Operates on the **whole fleet** (no ownership filter).

#### `/script action:simulate|run code:<python>`
* Runs arbitrary Python in `sandbox.run_script` (subprocess with RLIMITs: 20 s, 256 MB, 10 s CPU, network off by default, static blacklist of dangerous imports, 20 000 chars). Result embed + attached stdout/stderr files.

#### `/vip autoreply [message]`
* Blank → shows current; `off|disable|disabled|none|stop` → clear; else ≤1500 chars, `@everyone/@here` neutralised, `customer_manager.set_autoreply`. Relay: `_maybe_vip_autoreply` (bot.py:5184) on forwarded DM-inbox embeds → `!reply <buyer> <text>` once per (alt, buyer) per `AUTOREPLY_COOLDOWN_SEC` (1800).

### 4.4 Admin tier (`admin_commands.AdminCog`, group `/admin`, `default_permissions=administrator`, guild-only; every handler additionally calls `security.is_admin`; every action audited to `AUDIT_LOG_CH_ID`)

| Sub-command | Params | Behaviour |
|---|---|---|
| `list` | — | Table of all customers: id, username, alts, VIP, days left, active/expired. |
| `activate` | `user, days=30, alts=1, vip=false, github_account=""` | Refuses when no worker accounts are configured (`_no_workers_refusal`). Repo plan `{username}_alt{i}`; reuses existing repos/forum; `provision_alt_repo(owner, repo, token)` per repo; `discord_forum.create_customer_forum` (threads control/dashboard/farm-logs/deals[/dm-inbox]); `customer_manager.add_customer` (upsert, **resets expiry to now+days**); welcome DM. |
| `extend` | `user, days` | `extend_customer` (adds to max(expiry, now)); clears reminders. |
| `deactivate` | `user` | `timer_engine.run_shutdown_for_customer` (cancel runs, deactivate, forum read-only, DM). |
| `shutdown` | `confirm:ALL` | Multi-sig; shuts down every active customer. |
| `repos` | — | Lists all repos across workers. |
| `repo` | `action sync|delete, repo_name, confirmation DELETE` | `sync_sender_to_all_repos`; delete → `delete_customer_repo` + prune fleet mapping via `control_bot.bot._drop_alts_from_everywhere`. |
| `expiry-alerts` | — | `timer_engine.dry_run_expiry_alerts` (no messages). |
| `pin-policy` | `[channel]` | `policy.pin_policy_in_channel` (policy card + `PolicyAckView`). |
| `ticket-panel` | `[channel]` | Posts `TicketPanelView` (persistent button `ticket:open`), stores `open_ticket_ch_id` in DB meta + env. |
| `activate-template` | `user, [tx_hash]` | Pre-filled activation command text (`payments.activation_template`). |
| `payment-address` | `user` | Money-gate: requires `has_policy_ack(uid, POLICY_VERSION)`; DMs `PAYMENT_ADDRESS`. |
| `verify-tokens` | — | `verify_github_token` write-proof (scratch repo create+delete) + expiry for each worker PAT. |
| `sync-commands` | — | `_sync_and_hide` again. |
| `sweep-alts` | `dry_run` | `_sweep_stale_fleet_alts` (prune 404 repos). |
| `logs` | `user` | Link to customer's `#farm-logs` thread. |

#### `/reset confirmation:RESET` (top-level, owner-only)
* Without confirmation: impact summary. With: cancel all runs, `customer_manager.reset_all_data()`, `_drop_alts_from_everywhere(all)` (clears the three registry secrets), clears caches, `security.reload_channel_rules()`.

### 4.5 Interactive components (non-command entry points)
* `DashboardControlView` (persistent, on the pinned dashboard): Add Alt, Launch Run, Tune Fleet, Channels, Pause All, Resume All, Rescan, Reset Caution, Stop All — the last five use the **legacy owner gate** only.
* `TicketPanelView` → `TicketCategoryView` (Payment/Bug/Suggestion) → modals → `create_ticket_thread` (private thread in ticket channel, 7-day archive, adds user + members of role "Admin", alerts `ADMIN_ALERTS_CH_ID`).
* `PolicyAckView` → `customer_manager.ack_policy(uid, "v8-2026-09-03-1")`.
* `ReSetupView` (ban watch) → instructs the customer to re-run `/setup`.

---

## 5. Database Schema (`customer_manager.py`, `SCHEMA_VERSION = 3`, path `CUSTOMERS_DB` default `customers.db`, `PRAGMA journal_mode=WAL`)

```sql
customers(
  discord_id TEXT PRIMARY KEY, discord_username TEXT, alt_count INTEGER DEFAULT 1,
  vip INTEGER DEFAULT 0, start_date REAL, expiry_date REAL, active INTEGER DEFAULT 1,
  github_account TEXT, repos TEXT /* JSON list */, forum_id TEXT,
  control_thread_id TEXT, dashboard_thread_id TEXT, logs_thread_id TEXT,
  dm_thread_id TEXT, deals_thread_id TEXT, autoreply_text TEXT DEFAULT '' /* v3 */)
reminder_sent(discord_id TEXT, threshold INTEGER, sent_at REAL, PRIMARY KEY(discord_id, threshold))
policy_acks(discord_id TEXT PRIMARY KEY, acked_at REAL, version TEXT)
events(id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id TEXT, event TEXT, ts REAL, payload TEXT /* JSON */)
alt_credentials(discord_id TEXT, alt_index INTEGER, token TEXT /* obfuscated */, channel_ids TEXT /* JSON */,
                username TEXT, updated_at REAL, PRIMARY KEY(discord_id, alt_index))
run_state(discord_id TEXT, alt_index INTEGER, mode TEXT /* timed|limitless */, runtime_hours INTEGER,
          started_at REAL, last_dispatch_at REAL, renewals INTEGER DEFAULT 0, payload TEXT, PRIMARY KEY(discord_id, alt_index))
meta(key TEXT PRIMARY KEY, value TEXT)   -- schema_version, open_ticket_ch_id
```

**Relationships:** all tables key on `customers.discord_id` (no FK constraints declared). Alt
identity in the DB is `(discord_id, alt_index)`; alt identity in the bot is the fleet slot 1–4;
they are not linked by schema.

**Token storage:** `_obfuscate` = XOR with repeating `TOKEN_VAULT_KEY` then base64 (no
authentication, no salt); plain base64 when the key is empty. Disabled by `STORE_ALT_TOKENS_IN_DB=0`.

**Write path:** every mutating function calls `_after_write()` → `gist_backup.enqueue_backup()`
(background thread, coalesced). `deactivate_customer` takes a local file backup first
(`backup_db` → `BACKUP_DIR`).

**Public API:** `init_db, backup_db, reset_all_data, add_customer, get_customer, list_customers,
extend_customer, deactivate_customer, update_forum_ids, update_repos, days_remaining, is_active,
is_vip, set_vip, set_autoreply, get_autoreply, get_expiring_customers, get_expired_customers,
mark_reminder_sent, was_reminder_sent, clear_reminders, ack_policy, has_policy_ack, record_event,
get_events, store_alt_credential, get_alt_credentials, clear_alt_credentials, record_run_state,
get_run_states, bump_run_renewal, get_meta, set_meta, MAX_CHANNELS_PER_ALT=10, enforce_channel_limit`.

---

## 6. Gist Backup System (`gist_backup.py`)

* **Config:** `CUSTOMERS_GIST_ID || CONTROL_GIST_ID`, token `GIST_TOKEN || GH_ADMIN_TOKEN || GH_TOKEN`. Lease TTL `DB_GIST_LEASE_SECONDS` (600).
* **Files in the Gist:** `customers.db.b64` (current), `customers.prev.db.b64` (previous), `db-meta.json` `{revision, sha256, writer_run_id, prev_sha256, ts}`, `LOCK` `{run_id, host, pid, expires}`, and a bootstrap `customers.json`.
* **Backup (`backup_db_to_gist`):** `PRAGMA wal_checkpoint(TRUNCATE)`, read file, sha256, rotate current→prev, PATCH gist; retries with 10/20/40 s back-off; if `db-meta.writer_run_id` differs from ours → split-brain **warning** (still writes). Alerts are debounced 900 s through `register_alert_callback`.
* **Restore (`restore_db_from_gist`):** try current → prev → up to 5 history revisions; verify sha256 and `PRAGMA integrity_check`; atomic `os.replace`; records `LAST_RESTORE`.
* **Lease:** `acquire_run_lease(run_id)` refuses when a non-expired LOCK belongs to another run (bot does `os._exit(1)` — bot.py:471); `renew_run_lease` every 300 s (timer_engine); `release_run_lease` on shutdown.
* **Auto-recreate:** on 404/410/422 the Gist is recreated (`ensure_gist`) and the new id is logged (the secret is *not* updated automatically).
* **Frequency:** every DB mutation enqueues; the worker coalesces bursts; also flushed on shutdown.

---

## 7. GitHub Operations

### 7.1 `github_dispatch.py` (customer/worker layer)
* Token resolution: `GH_ADMIN_TOKEN` (main), workers from `WORKER_TOKENS="org:tok,…"`, `WORKER_GITHUB_OWNERS`+`WORKER_TOKENS_LIST`, or `WORKER_{1..3}_USER/TOKEN`. `pick_worker()` round-robins in-process (index resets on restart). `token_for_owner(owner)`.
* Repos: `repo_exists`, `create_repo` (public unless `private=True`, `auto_init`), `provision_alt_repo` (create → sleep 2 s → upload `send_ads.py`, `.github/workflows/send_ads.yml`, `self_check.yml` via Contents API → `enable_repo_secret_protection`), `soft_delete_repo` (rename `_DELETED_<ts>`), `rename_banned_repo` (`_BANNED_<ts>`), `create_replacement_alt_repo`, `delete_repo`, `delete_customer_repo` (also prunes `customers.repos` and fleet mapping), `disable_workflow`, `list_all_repos`, `sync_sender_to_all_repos`.
* Secrets: `set_repo_secret` seals with PyNaCl using the repo public key; `ALLOW_BASE64_SECRET_FALLBACK` exists for tests only (a base64 "secret" is rejected by GitHub).
* Workflows: `dispatch_workflow(owner, repo, workflow, inputs, ref="main")`, `cancel_workflow_runs`.
* Health: `verify_github_token` (write-proof by creating+deleting `adfarm-token-check-<ts>`), `check_token_status` (expiry header `github-authentication-token-expiration`), `list_worker_tokens`.

### 7.2 `control_bot/github_api.py` (fleet layer)
* `_auth_headers(repo)` picks the worker PAT when the slug owner is a worker, else `GH_TOKEN`.
* `dispatch_workflow(alt_id, inputs)` → `send_ads.yml` on `main`, then sleeps 3 s and fetches the latest run id. `dispatch_named_workflow(alt, "self_check.yml")`. `cancel_run(alt)` cancels the *latest* run (whatever it is). `cancel_workflow_run_by_id(run_id, repo=CORE_REPO)`. `list_runs`, `refresh_all_run_statuses(state)` (every 60 s).
* Secrets through the **`gh` CLI** (`gh secret set --repo … ` with the value on stdin) — requires `gh` on the runner; REST fallback only for delete. `delete_repository` via `gh repo delete --yes`.
* `create_alt_repository` (REST org/user endpoints then `gh repo create`), `provision_alt_repository_files_and_secrets` (uploads the three files from the local checkout or raw.githubusercontent, sets `USER_TOKEN`, `CHANNEL_IDS`, `DM/LOG/DASHBOARD/DEAL_WEBHOOK_URL`, `GIST_ID`, `GIST_TOKEN`, `CONTROL_GIST_ID`, `CONTROLLER_USER_IDS`).
* `upload_repository_file` (Contents API PUT with sha), `queue_control_command` (§2.2 step 4), `fetch/save_channel_registry_snapshot` (Gist files `channel_state_<alt>.json`), `gist_usage_stats` (429s, latency).

### 7.3 Multi-account handling
Customer repos are always created under a worker; the main token cannot see them, hence the
owner-based token lookup. `sync_to_alts.yml` pushes `send_ads.py`, `control_bot/persistence.py`,
`control_bot/__init__.py` and both workflows to every repo in `ALT_REPOS` on push to `main`,
using `WORKER_TOKENS` for owner-matched repos.

---

## 8. Timer Engine (`timer_engine.py`)

* `start(bot, audit_channel_id)` launches: `expiry_loop` (every `SCAN_INTERVAL_SEC`=3600, first run after 60 s), `auto_redispatch_loop` (3600), `lease_renewal_loop` (300).
* `scan_once`: for each expired customer (`active=1 AND expiry ≤ now`) → `run_shutdown_for_customer`: cancel workflow runs for each repo (`github_dispatch.cancel_workflow_runs`), `deactivate_customer`, `discord_forum.make_forum_readonly`, DM the customer, audit line. For each customer expiring within 7 days → reminders at thresholds **7, 3, 1 days**, each sent once (`reminder_sent`), by DM and to the `#control` thread.
* `auto_redispatch_loop_once`: for every `run_state` with `mode=limitless` whose `last_dispatch_at` is ≥ 48 h old and whose customer is active → re-dispatch via `control_bot.github_api.dispatch_workflow` with the stored payload, `bump_run_renewal`, notice in `#control`.
* `dry_run_expiry_alerts()` → report for `/admin expiry-alerts`.
* Time source: `time.time()` everywhere; no timezone logic (all epoch floats).

---

## 9. Sender (`send_ads.py`, `send_ads.yml`, `self_check.yml`)

* **Inputs (workflow_dispatch):** `ad_type sell|buy`, `message`, `sell_rate`, `sell_extra`, `buy_style`, `buy_rate`, `buy_rate_rap`, `buy_simple_text`, `channel_1/2` (+names), `interval_min 3|5`, `total_hours 6–48`, `runtime_limitless`, `attach_image`.
* **Required env:** `USER_TOKEN`, `AD_TYPE`, `MESSAGE`; `CHANNEL_IDS` (ids or keywords resolved after auth). `curl_cffi` is mandatory unless `ALLOW_REQUESTS_FALLBACK=1`.
* **Optional env (60+):** AFK breaks, warmup posts, typo edits, reactions, EXIF strip, image jitter, proxy check, gateway, DM pause, blocked-variation strikes, `GIST_ID/GIST_TOKEN` (blacklist), `ALLOWED_COUNTRIES`, deal scanner (`DEAL_*`), caution mode (`CAUTION_*`), IP health, panic (`PANIC_TRUSTED_IDS`, gist flag), `CHANNEL_NAMES/KEYWORDS`, `CONFIRM_USER_IDS`, `CONTROLLER_USER_IDS`, `CONTROL_GIST_ID`, `SYNC_GIST_INTERVAL_SEC` (45, min 15), `HEARTBEAT_INTERVAL_SEC` (300, min 60), `ALT_ID`, `ALT_NAME`, `TUNING_JSON`, locale/timezone, status text.
* **Control protocol (Gist):** file `control_<ALT_ID>.json` `{alt_id, command_id, command, args, issued_at, transport, +durable keys: paused, rate, interval_min, runtime_hours, policy_template, ad_type, message, deal_keywords, deal_scan_enabled, deal_alert_delta}`. Commands understood (`_handle_controller_dm`, send_ads.py:3483): `ping/hello, status, pause, resume, stop/quit/panic, setprice/price/rate, setmode/mode, setmessage/message, setinterval, setruntime, policy, setdealkeywords, setdealscan, setdealdelta, setchannel, replacechannel, removechannel, setchannels, rescan, resetcaution, reply, sync`. Ack is written back into the same file (`_ack_control_gist`).
* **Heartbeat payload** (`_send_heartbeat`, send_ads.py:4055): `type:"heartbeat", version, alt_id, alt_name, ad_type, rate, rate_currency, interval_min, policy_template, runtime_hours, message_preview, total_sent/errors/skips/edits, deal_alerts, last_deal_ts, deal_keywords, deal_scan_enabled, deal_alert_delta, last_error, log_counts, uptime_sec, active_channels, total_channels, last_post_ts, status ∈ {active,paused,caution,ip_pause,afk,stopped,error}, warnings, channels{cid:{name,sent,errors,last_post,slowmode,alive}}, channel_registry, run_started_ts, ip_org, ip_country, ts` — sent as a readable embed; the bot parses embed fields (`_consume_embed_heartbeat`) and legacy JSON.
* **Exit codes:** 0 normal, 2 = ban/panic/new-location/safety stop (cancels the whole workflow).
* **Anti-detection stack:** curl_cffi Chrome impersonation, gateway presence, cookie warm-up, per-channel referer/nonce, typing indicator, variations (emoji/typo/casing), typo-fix edits, AFK breaks, IP/country checks, 429 handling, caution mode, fleet collision avoidance (`_check_fleet_collision`).
* **self_check.yml:** 10 checks (TUNING_JSON, USER_TOKEN, channels, webhooks, gist, controller ids, `ALT_ID/ALT_NAME` vars, WARP).

---

## 10. Security

* **Token handling:** bot token, GH tokens, worker PATs in repo secrets; alt tokens transit Discord modals → memory → repo secret (PyNaCl) → obfuscated DB copy. `_mask_secrets` in the sender log path; `gh secret set` receives values on stdin. Nightly `ops.nightly_token_sweep` re-validates every stored alt token against `/users/@me` and alerts.
* **Permission enforcement:** `_check_perms` per command (§3); decorators `require_access`/`require_channel` exist in `security.py` but bot.py calls the helpers directly. Modal/hub callbacks re-check with `_is_operator` (owner or any active customer — not ownership).
* **Rate limiting:** `CMD_COOLDOWN_SEC` for owners only; `AUTOREPLY_COOLDOWN_SEC`; alert debounce 60 s (`alerts.py`) / 900 s (gist_backup).
* **Confirmations:** `/alt remove confirmation:DELETE`, `/admin repo action:delete confirmation:DELETE`, `/shutdown confirmation:SHUTDOWN`, `/reset confirmation:RESET`, `/admin shutdown confirm:ALL` + multisig.
* **Sandbox:** `/script` subprocess with RLIMITs and a static import blacklist; network off unless `SCRIPT_NETWORK_ENABLED`.
* **Money gate:** payment address only after `policy_acks` for `POLICY_VERSION`.
* **Fail-closed rules:** empty `OWNER_IDS` ⇒ no admin; V8 import failure ⇒ every command falls back to owner-only; lease conflict ⇒ hard exit.
* **Fail-open rules:** channel gate on exception; visibility push errors; forum owner assertion on exception (`/setup`).

---

## 11. Business Rules

| Rule | Where | Detail |
|---|---|---|
| Subscription length | `/admin activate days` (default 30) | `expiry = now + days·86400`; re-activation **resets** expiry (does not add). `extend` adds to `max(expiry, now)`. |
| Reminders | timer_engine | 7 / 3 / 1 days before expiry, once each, DM + `#control`. |
| Expiry | timer_engine hourly | cancel runs → `active=0` → forum read-only → DM. |
| Alt count | `alt_count` (1–4 enforced by `/setup` modal `min(4, alt_count)`) | Fleet slots are globally 1–4 (`_resolve_new_alt_id`). |
| Channels per alt | `MAX_CHANNELS_PER_ALT = 10` | `/setup`, `/channels add|overwrite`. Message: `❌ Maximum 10 channels per alt. Remove one before adding a new one.` |
| Intervals / runtimes | 3 or 5 min; 6/12/18/24/48 h or 0 (∞) | Sender chunks 350 min each, max 8 (48 h). |
| ∞ Limitless | 48 h per dispatch, auto-renew hourly check while active | `run_state.mode = limitless`. |
| Price bounds | `0 < price ≤ 20` ($/1k) | `/tune price`, `/run` rates, sender `!setprice`. |
| Deal delta | `0 ≤ delta ≤ 5` | `/deals min_delta`. |
| Keywords | ≤20, ≤60 chars each, ≤500 total | `/deals keywords`. |
| Message length | ≤1900 chars | `/run`, `/tune message`. |
| Autoreply | ≤1500 chars, no mass mentions, 30 min cooldown per buyer | `/vip autoreply`. |
| VIP features | `/squad`, `/script`, `/vip`, `#dm-inbox` thread | `vip=1`. |
| Payments | Manual BEP-20 USDT/BUSD; TX hash `0x[a-f0-9]{64}` auto-acked once per customer | `payments.py`. |
| Policy | Version `v8-2026-09-03-1`; ack required before wallet address | `policy.py`. |
| Bans | Marker regex on `#farm-logs`; first event per hour → `#control` message + `ReSetupView`; repo renamed `_BANNED_<ts>`, replacement created; time credit: full ≤48 h, pro-rated after | `ban_watch.py` (credit itself is manual). |
| Repo naming | `{username_lower}_alt{i}` | admin_commands `_alt_repo_plan`. |
| Forum layout | Category "🏢 Customer Hub", forum per customer, threads control/dashboard/farm-logs/deals(+dm-inbox VIP), read-only on expiry | `discord_forum.py`. |
| Public channels | welcome-about, pricing-plans, whats-new, open-ticket (read-only), general-chat; staff admin-commands/admin-chat/audit-logs; `setup.py` creates admin-alerts/admin-chat/audit-logs/announcements/open-ticket | `discord_forum.create_public_channels`, `setup.py`. |

---

## 12. Operational monitors (`control_bot/ops.py`, `metrics.py`, `alerts.py`)
Hourly worker-token health (401 → critical, ≤7 d / ≤1 d expiry warnings), `/healthz` HTTP
endpoint (`HEARTBEAT_PORT` 8080) with missed-external-ping alert (15 min), RSS memory doubling
check (30 min), Gist request-rate alert (>4000/h, 429s), nightly alt-token sweep, `/tune` hint
after day 3, weekly metrics summary (TTFTV, survival, bans), daily forum permission self-check,
5-minute fleet health probe (`!sync` on stale heartbeats), 60 s GitHub run refresh, 300 s
dashboard refresh (edits a pinned message whose id is stored in `dash_msg_id.txt`).

---

## 13. Known Issues & Limitations (verified in code)

1. **Customer alts are never registered in the fleet.** `/admin activate` stores repos in `customers.repos`; `/setup` writes secrets + credentials; neither calls `_persist_alt_registry`/`state.add_alt`. `/run`, `/status`, `/tune`, `/channels`, `/stop` all iterate `state.alt_ids`, which only `/alt action:add` (an admin-style modal that provisions a *new* repo under a worker) populates. A freshly activated customer therefore sees "❌ No configured alts are available" on `/run` until an admin manually adds each alt with `/alt action:add` — and that path ignores the repo already provisioned by activation.
2. **Fleet cap of 4 alts globally** (`_resolve_new_alt_id`, `AltAddModal` "Alt ID 1–4") — incompatible with more than a handful of customers.
3. **`/shutdown` is customer-tier**: any active customer can stop all alts, cancel the bot's own workflow run and close the process.
4. **No ownership enforcement** on `/run`, `/stop`, `/pause`, `/resume`, `/tune`, `/channels`, `/deals`, `/reply`, `/status`, `/squad`: alt ids are plain integers checked only against `state.alt_ids`; `/status` renders the whole fleet to every customer; `_is_operator` (any active customer) guards modal callbacks. Only `/alt` filters by ownership, using three fragile heuristics (repo basename, Discord id, username string match).
5. **Mutable state in GitHub secrets**: `ALT_REPOS`/`ALT_DISCORD_IDS`/`ALT_NAMES` are rewritten at runtime via `gh secret set`; a failed `gh` call leaves memory and secrets diverged; secrets are invisible for debugging; the values are also read once at import (`config.py`), so other chunks only see them on restart.
6. **Webhooks for customer forums are never created**: `discord_forum.create_customer_forum` creates threads but no webhooks; alt repos receive the operator-global `*_WEBHOOK_URL` secrets. Customer `#dashboard`/`#farm-logs`/`#deals` threads stay empty; ban detection (`_v8_message_hooks`) that keys on `logs_thread_id` cannot fire.
7. **Heartbeat ingestion is global, not per customer**: `_handle_guild_webhook_message` only parses `DASHBOARD_CH_ID`, `LOG_CH_ID`, `DEALS_CH_ID` (operator channels) and routes by fuzzy `ALT_NAME` matching (`_match_alt_name` substring logic — "Alt 1" matches "Alt 12").
8. **Dashboard message id file is per runner** → every 350-min chunk posts a new pinned dashboard message.
9. **Token obfuscation is XOR/base64** (no MAC, no salt); an attacker with the DB and any known plaintext recovers the key.
10. **Secrets via `gh` CLI** for the fleet layer (requires the CLI on the runner) while the customer layer uses PyNaCl REST; two code paths for the same operation.
11. **Round-robin state is in-process** (`_worker_index`), so worker selection restarts at worker 1 on every chunk → uneven distribution.
12. **`cancel_run` cancels the latest run of the repo**, whatever it is (could be a self-check).
13. **`/tune alt:0` semantics are inconsistent** (first alt for price/message, all alts for policy/image).
14. **Fail-open channel gate** and fail-open forum-owner assertion.
15. **`bot.py` is a 5 831-line module** mixing UI views, dispatch, parsing, persistence, security shims and background tasks; module-level singletons (`state`, `bot`) make unit testing require heavy monkeypatching. Two GitHub clients (`github_dispatch`, `github_api`), two alert paths, two token-resolution implementations.
16. **Docs drift:** README references `SETUP_CONTROL.md`, `SETUP_GUIDE.md`, `ROADMAP.md`, `V8_RUNBOOKS.md` at repo root (the first two live in `docs/`, the others do not exist); SKILL.md says "21 customer commands + 12 admin subcommands" (actual: 20 top-level + `vip autoreply`; 16 admin sub-commands); README lists `/admin repo-sync` (actual `/admin repo action:sync`).
17. **`sync_to_alts.yml`** targets `ALT_REPOS` only — customer repos provisioned by `/admin activate` are not in that secret, so sender updates never reach them (only `/admin repo action:sync` does).
18. **Re-activation resets expiry** instead of extending — an admin re-running `activate` to add an alt silently gives a fresh 30 days (or shortens a longer remaining period).
19. **Gist auto-recreate** changes the Gist id without updating the secret; the next chunk restores from the old (missing) Gist.
20. **Edge cases not handled:** more than 25 alts in a select (Discord limit — sliced silently), `/channels` when the alt is offline (command sits in the Gist until the next run; the durable keys keep it, but `!setchannel` itself is one-shot), forum deletion by a moderator (IDs kept in DB; `_unreachable_state_channels` only covers operator channels), a customer with `alt_count > len(repos)`.

---

## 14. Setup & Deployment

### 14.1 `setup.py` (9 steps)
1. `gh auth token` + scopes check → main token; detect user and `CORE_REPO` from git remote.
2. Collect `BOT_TOKEN`, `OWNER_IDS`, `GUILD_ID`, optional `PAYMENT_ADDRESS` (prompt or env with `--quick`).
3. Collect up to 3 worker accounts (user + PAT), verifying each with `GET /user`.
4. Verify the bot token and guild membership.
5. Create Discord structure: role "Admin" (assigned to owners), `#admin-alerts`, `#admin-chat`, `#audit-logs` (Admin+bot only), `#announcements`, `#open-ticket` (read-only), category "🏢 Customer Hub" (hidden).
6. Create the backup Gist (`customers.json` bootstrap).
7. Set core secrets: `BOT_TOKEN, GH_TOKEN, GH_ADMIN_TOKEN, OWNER_IDS, GUILD_ID, REPO_OWNER, CORE_REPO, *_CH_ID, CUSTOMER_HUB_ID, GIST_ID, GIST_TOKEN, PAYMENT_ADDRESS, WORKER_TOKENS, WORKER_GITHUB_OWNERS, WORKER_TOKENS_LIST, WORKER_{i}_USER/TOKEN`.
8. `init_db`, verify workflows exist, enable secret scanning.
9. Pin the policy card; print summary. `bootstrap.yml` runs the same non-interactively from `BOOTSTRAP_*` secrets.

### 14.2 Environment / secrets consumed by the bot (`control_bot.yml`)
`BOT_TOKEN GUILD_ID CONTROL_CH_ID DASHBOARD_CH_ID LOG_CH_ID DEALS_CH_ID DM_INBOX_CH_ID OWNER_IDS
GH_TOKEN GH_ADMIN_TOKEN GITHUB_OWNER(=ALT_GITHUB_OWNER||REPO_OWNER) CORE_REPO CONTROL_GIST_ID
CUSTOMERS_GIST_ID GIST_TOKEN CHANNEL_STATE_GIST_ID ALT_REPOS ALT_DISCORD_IDS ALT_NAMES TUNING_JSON
CUSTOMERS_DB WORKER_TOKENS WORKER_GITHUB_OWNERS WORKER_TOKENS_LIST WORKER_{1..3}_USER/TOKEN
AUDIT_LOG_CH_ID ADMIN_ALERTS_CH_ID ADMIN_CHAT_CH_ID OPEN_TICKET_CH_ID PROOFS_CH_ID PAYMENT_ADDRESS
TOKEN_VAULT_KEY STORE_ALT_TOKENS_IN_DB PUBLIC/CUSTOMER/VIP/ADMIN_CHANNELS CUSTOMER_HUB_MARKER
HEARTBEAT_PORT CMD_COOLDOWN_SEC DASHBOARD_REFRESH_SEC OFFLINE_AFTER_SEC …`
Deploy: run `setup.py`, push, trigger `control_bot.yml` once (cron keeps it alive), invite the
bot with `bot` + `applications.commands` scopes and Message Content intent.

---

## 15. Testing

* 16 files, 314 passing, 9 skipped (skips: network/`gh` CLI dependent). Requires `discord.py`, `requests`, `curl_cffi` (or `ALLOW_REQUESTS_FALLBACK=1`, which then fails on `Session(proxies=)`).
* `tests/conftest.py` strips every production secret from the environment before imports, redirects `CUSTOMERS_DB`, state JSON files and `dash_msg_id.txt` to a temp directory, snapshots/restores `os.environ` around each test → **isolated, non-destructive**.
* Coverage areas: control runtime (state manager, dashboard), inbox/end-to-end (webhook parsing, VIP autoreply, DM relay), phase-0 hardening (gist backup, lease, restore), persistence (JSON stores, channel registry), plan features, run UI validation, safety (sender helpers), setup shim, V8 bug-fix rounds (functional, security gates, edge cases, timer, stress, channel reset/sync).
* Not covered: real Discord interactions (views are driven with fakes), GitHub REST (mocked `requests`), `send_ads.main()`, `admin_commands.activate` end-to-end with forum creation.
