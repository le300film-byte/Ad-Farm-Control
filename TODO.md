## 📋 `TODO.md` — V9 Critical Fixes & Improvements

### 🎯 EXECUTION INSTRUCTION FOR THE AI

```
Read this entire TODO.md. Explore the codebase thoroughly. Fix every item in order of priority. Do not skip anything. After fixing, verify each item with tests.
```

**Status: all P0 / P1 / P2 items below are fixed and verified.**

Two independent checks cover every item:

```bash
pytest tests/                          # 200 tests (154 pre-existing + 46 new regressions)
python analysis/forensic_repros.py     # 15 item-by-item reproductions, one per TODO entry
```

`analysis/forensic_repros.py` did not exist before (the P2-10 entry pointed at it). It was written
as part of this work and was validated in both directions: **14 of 15 checks fail on the pre-fix
commit `a041b54`** (P2-8 passes in both, because there were never hardcoded ids) and **15/15 pass
after the fixes**. Item IDs in the source and tests map to the entries below.

---

## 🔴 CRITICAL BLOCKERS (P0 — Must Fix Before Any Customer Run)

### F01 — Sender Webhook URLs Are Broken ✅ FIXED

**Problem:** The bot creates webhook URLs like `?thread_id=456?wait=true` and `456/messages/777`. This is malformed—sender can't deliver logs/dashboards to customer forums.

**Fix applied:** Neither of the two options in the original entry was safe on its own — dropping
`?thread_id=` breaks forum targeting (a webhook on a *forum* channel 400s without a thread
selector), and dropping `?wait=true` breaks the heartbeat edit (no message id comes back). So the
joining itself was fixed, in `sender/send_ads.py`:

* `_webhook_execute(url)` — appends `wait=true` with `&` when the URL already has a query string.
  All 10 webhook call sites (DM, log, dashboard, deals, heartbeat) now go through it.
* `_webhook_base(url)` — strips the query before building `{base}/messages/<id>`, so the heartbeat
  PATCH is well-formed.

`adfarm/discord/adapter.py::ensure_forum_webhooks` keeps `?thread_id=` and now documents why.

**Files:** `sender/send_ads.py`, `adfarm/discord/adapter.py` · **Tests:** `test_v9_fixes.py::test_f01_*`, `forensic_repros.py F01`

---

### F02 — Workflow Input Flags Mismatch ✅ FIXED

**Problem:** The dispatcher sends `true/false` for `runtime_limitless` and `attach_image`, but the workflow expects `0/1` and `yes/no`. Images are uploaded as secrets but the workflow never reads them.

**Fix applied:**

* `runtime_limitless` → `"1"`/`"0"`, `attach_image` → `"yes"`/`"no"` (`flag_0_1` / `flag_yes_no`).
* `build_inputs()` now filters through `WORKFLOW_INPUTS`, the exact set `send_ads.yml` declares, so
  a dispatch can never 422 on an undeclared input (this also caught `buy_items` /
  `buy_items_price` / `buy_items_style`, which no workflow input exists for — see F10).
* Images are committed to the alt repo as `ad.png` through the Contents API
  (`RepoProvisioner.upload_image`) instead of an `AD_IMAGE_B64` secret. That is the path the sender
  actually reads (`IMAGE_PATH` defaults to `ad.png` in the checkout), and a secret could never have
  worked anyway — GitHub caps secrets at 48 KB. `MAX_IMAGE_BYTES` is now the Contents API's 1 MB
  limit rather than a misleading 8 MB (see F11).

A test parses the real `send_ads.yml` and asserts the two sets match, so the two files cannot drift.

**Files:** `adfarm/github/workflows.py`, `adfarm/github/repos.py`, `adfarm/commands/customer.py`, `adfarm/core/rules.py` · **Tests:** `test_v9_fixes.py::test_f02_*`, `test_github.py`, `test_services.py`, `forensic_repros.py F02`

---

### F03 — Heartbeat Edits Are Ignored ✅ FIXED

**Problem:** The sender edits heartbeat messages, but V9 doesn't listen to `on_message_edit` events. The bot thinks alts are offline when they're actually healthy.

**Fix applied:** The ingest side-effects moved out of `main()` into a module-level
`ingest_message(services, ingestor, message)`, which all three handlers share:

* `on_message` (unchanged behaviour),
* `on_message_edit(before, after)` — ingests `after`; this is the heartbeat path,
* `on_raw_message_edit(payload)` — the fallback for uncached messages, gated on
  `cached_message is None` so an edit is never ingested twice.

Extracting the function is what makes it testable without a live gateway.

**File:** `adfarm/app.py` · **Tests:** `test_v9_fixes.py::test_f03_*`, `forensic_repros.py F03`

---

### F04 — Lease Release Is Unsafe (Split-Brain) ✅ FIXED

**Problem:** A rejected bot can release another bot's lease. Two bots can acquire the same lease simultaneously.

**Fix applied** in `adfarm/db/gist_backup.py`:

* `release_lease()` reads the lock first and only clears it when `run_id` **and** the acquisition
  token match; otherwise it logs and returns `False`. It now returns a bool.
* `acquire_lease()` is a compare-and-swap. The Gist API has no conditional write, so the CAS is
  emulated: write a lock carrying a unique token, read the Gist back, and only claim the lease if
  our token is the one that survived. Losers re-read and retry (`lease_attempts`). A live foreign
  lease is still never stolen.
* `renew_lease()` verifies ownership before extending, and returns `False` when the lease belongs to
  somebody else. `app.py`'s lease job now reacts to that by alerting admins and shutting the runner
  down rather than continuing to write the database — which is the split brain the lock exists to
  prevent (see F14).

**File:** `adfarm/db/gist_backup.py`, `adfarm/app.py` · **Tests:** `test_db.py::test_f04_*` (+ the existing `test_lease_acquire_conflict_and_expiry`, which asserted the buggy release and was corrected), `forensic_repros.py F04`

---

### F05 — Failed Database BEGIN Poisons Later Transactions ✅ FIXED

**Problem:** If a database lock times out, the bot marks the transaction as complete and stops scheduling backups.

**Fix applied** in `adfarm/db/database.py`: `transaction()` set `depth = 1` *before* `BEGIN
IMMEDIATE`, so a raising BEGIN skipped the `finally` cleanup and left `depth=1` plus a closed
connection on the thread-local. Every later `transaction()` on that thread then took the re-entrant
branch and never committed again. The BEGIN is now inside its own `try` that closes the connection
and leaves the thread-local untouched. Rollback also catches `BaseException` (so
`asyncio.CancelledError` rolls back) and a failing ROLLBACK can no longer mask the original error.
`migrate()` got the same treatment. `Database(busy_timeout=…)` was added so lock contention is
reproducible in a test rather than needing a 30 s stall.

**File:** `adfarm/db/database.py` · **Tests:** `test_db.py::test_f05_*`, `forensic_repros.py F05`

---

### F06 — Backup Restore Can Silently Fail ✅ FIXED

**Problem:** If a Gist restore fails, the bot starts with an empty DB and overwrites the backup with the empty DB.

**Fix applied** in `adfarm/db/gist_backup.py` + `adfarm/db/database.py`:

* New `Database.payload_is_usable(raw)` validates a candidate on a throwaway copy — integrity check
  **and** the presence of the adfarm schema — so a rejected payload never touches the live database.
* When the gist holds a snapshot but no candidate passes, `restore()` returns `"none"` and arms
  `restore_blocked`. An empty gist is still treated as a fresh install, not a failure.
* While `restore_blocked` is set and the local DB has no customers, `_upload()` raises
  `BackupUnavailable` instead of overwriting the remote copy. The reason surfaces in
  `BackupStatus.restore_blocked`, `/admin backup` and `/admin health`.
* `/admin backup sub:force` (`GistBackup.flush(force=True)`) is the deliberate escape hatch.

**Files:** `adfarm/db/gist_backup.py`, `adfarm/db/database.py`, `adfarm/commands/admin.py` · **Tests:** `test_db.py::test_f06_*`, `forensic_repros.py F06`

---

### F09 — Renewal Resets Tuning; Expiration Locks Customers Out of `/renew` ✅ FIXED

**Problem:** Limitless renewals discard price/message changes. Expired customers can't run `/renew` (the very command that tells them to renew).

**Fix applied:**

* `services/runs.py`: `/tune` records changes in `Alt.runtime_overrides`, never in
  `RunState.payload`, so a renewal rebuilt from the payload reverted everything. New pure helper
  `merged_renewal_payload(payload, overrides)` layers the tuned values on top before dispatch.
* `security/policy.py`: `decide()` takes the caller's subscription `state`, and
  `EXPIRED_ALLOWED_COMMANDS = {"renew"}` keeps the renewal path open while expired. Channel rules
  still apply (own hub / ticket / admin — not public rooms), and `Guard.check` passes the state
  through. `DENY_EXPIRED` now tells the user to run `/renew` instead of "contact an admin".

**Files:** `adfarm/services/runs.py`, `adfarm/security/policy.py`, `adfarm/security/guards.py` · **Tests:** `test_v9_fixes.py::test_f09_*`, `forensic_repros.py F09`

---

## 🟡 HIGH PRIORITY (P1 — UX, Security & Permissions)

### 1. Command Title Length Errors ✅ FIXED

**Problem:** `400 Bad Request (error code: 50035): Invalid Form Body — In data.title: Must be between 1 and 45 in length.`

**Fix applied:** The offender was `SetupModal`, whose title
`"Setup alt 1 (never share this token elsewhere)"` is exactly **46** characters. All modal titles
now go through `modal_title()`, which clamps to `MODAL_TITLE_LIMIT = 45`. A test constructs every
modal for alt slots 1–4 and asserts the limit, so a future title cannot regress silently.

**Files:** `adfarm/commands/registry.py` · **Tests:** `test_v9_fixes.py::test_p1_1_*`, `forensic_repros.py P1-1`

---

### 2. Channel Permissions for Non-Customers ✅ FIXED

**Problem:** Normal users (non-customers) should not be able to chat or send messages in customer forums. Also, they should not see any commands except `/help` and `/getstarted`.

**Fix applied:** `adfarm/discord/permissions.py` did not exist; it is now the single home for the
whole permission matrix (previously duplicated inline in `provision.py` and the adapter, where it
could drift). It exports `public_overwrites` / `staff_overwrites` / `hub_overwrites` and the new
`forum_overwrites`, all framework-neutral and therefore unit-testable:

* public rooms → `@everyone` view + send + slash;
* `#admin-commands` / `#admin-chat` / `#audit-logs` and the `🏢 Customer Hub` category → hidden from `@everyone`;
* customer forums → hidden from `@everyone`, writable by that customer, visible to the admin role
  **and** to each owner id explicitly (previously admins relied on the implicit view-all that
  `manage_channels` grants — see F19);
* `use_application_commands` is deliberately allowed in public rooms so `/help` and `/getstarted`
  work; command *visibility* is the registry's job (P2-9).

**Files:** `adfarm/discord/permissions.py` (new), `adfarm/discord/provision.py`, `adfarm/discord/adapter.py`, `adfarm/discord/forums.py` · **Tests:** `test_v9_fixes.py::test_p1_2_*`, `test_provision.py`, `forensic_repros.py P1-2`

---

### 3. Add `/help-admin` Command ✅ FIXED

**Fix applied:** `admin.help_admin` renders `/admin action:<name>`, a summary and a copy-pasteable
example for all 18 actions, plus the multisig window. It is built from the `ADMIN_HELP` table and a
test asserts every entry of `ADMIN_ACTIONS` is present, so a new action cannot ship undocumented.
Registered as a top-level admin-only command (tier `ADMIN`, `default_permissions=administrator`).

**Files:** `adfarm/commands/admin.py`, `adfarm/commands/registry.py`, `adfarm/security/policy.py`, `adfarm/discord/embeds.py` · **Tests:** `test_v9_fixes.py::test_p1_3_*`, `forensic_repros.py P1-3`

---

### 4. Hide Repo Names & GitHub Accounts from `/status` ✅ FIXED

**Fix applied:** `alt_status_embed(..., reveal_infra=False)` only shows the `Repo` field for admins.
The same leak existed in four other customer-facing places and was closed too: `/alt
action:overview` (repo + `Sender ALT_ID` + sync state), `/alt action:runs` (the Actions URL embeds
the worker account and repo name), the `/setup` confirmation, and the `/run` confirmation (run
URL). Customers now see alt index/label, mode, rate, cadence and sent/error counts only.

**Files:** `adfarm/discord/embeds.py`, `adfarm/commands/customer.py` · **Tests:** `test_v9_fixes.py::test_p1_4_*`, `test_commands.py`, `forensic_repros.py P1-4`

---

### 5. Remove Risk-Focused Policy Text ✅ FIXED

**Fix applied:** The wording lives in the new `adfarm/discord/policy.py` (the file this entry
pointed at did not exist; the text was inline in `services/tickets.py`), replaced with the
welcoming service agreement from this document. `POLICY_VERSION` moved to `core/rules.py` (importing
it from `discord/` would be circular, since `discord/__init__` → `channels` → `config`) and is now
`v9-2026-09-05-1`, so every customer is asked to confirm the new wording once. The accept button
label is `✅ I understand — let's proceed`, matching the "Click ✅ below" line, and `/getstarted`
shows the agreement up front.

**Files:** `adfarm/discord/policy.py` (new), `adfarm/services/tickets.py`, `adfarm/commands/public.py`, `adfarm/commands/registry.py`, `adfarm/core/rules.py`, `adfarm/config.py` · **Tests:** `test_v9_fixes.py::test_p1_5_*`, `forensic_repros.py P1-5`

---

### 6. `/run` with `hours:Limitless` Should Work ✅ FIXED

**Fix applied:** This was F02's `runtime_limitless` flag plus the dropdown parsing. `hours:Limitless`
(value `0`) now reaches `validate_runtime` as `0`, produces `runtime_limitless: "1"` with
`total_hours: 48`, and is stored as `RunMode.LIMITLESS` so the 48 h renewer picks it up. Verified
end to end against the recorded dispatch inputs, in both the limitless and the timed direction.

**Files:** `adfarm/github/workflows.py`, `adfarm/commands/customer.py`, `adfarm/services/runs.py` · **Tests:** `test_v9_fixes.py::test_p1_6_*`, `forensic_repros.py F02/F09`

---

### 7. Ticket Panel Missing Button (View) ✅ FIXED

**Problem:** The `/admin ticket-panel` command sends an embed and pins it, but **no button** appears for customers to click and open a ticket.

**Fix applied:** Handlers are framework-neutral and cannot build a `discord.ui.View`, so the handler
returns a `post_ticket_panel` marker naming the channel and carrying the embed, and
`CommandRegistry.post_ticket_panel()` — the only module allowed to import discord.py — performs the
send with a real persistent `TicketPanelView` (`timeout=None`, stable
`custom_id="adfarm:ticket:open"`, re-registered via `client.add_view` on every boot) and pins the
message. The button opens `TicketModal` → `TicketService.open_support()`, which records a ticket and
creates a thread in the ticket channel. `open_support` deliberately does not need a `Customer` row,
because the people clicking it are usually prospective buyers. New `DiscordPort.create_thread`
backs it.

**Files:** `adfarm/commands/admin.py`, `adfarm/commands/registry.py`, `adfarm/services/tickets.py`, `adfarm/discord/adapter.py`, `adfarm/discord/ports.py`, `adfarm/app.py` · **Tests:** `test_v9_fixes.py::test_p1_7_*`, `test_commands.py`, `forensic_repros.py P1-7`

---

## 🟢 MEDIUM PRIORITY (P2 — Additional Checks & Improvements)

### 7. Double-Check Forum Permission Overrides ✅ VERIFIED + FIXED

**Fix applied:** Audited through the new `forum_overwrites()` (see P1-2). Two real gaps were closed:
admins had no explicit overwrite in customer forums (F19), and when the customer could not be
resolved as a member the forum was created invisible to everyone with no log line at all (F19).
`ForumProvisioner` now takes `admin_user_ids`/`admin_role_id` and `build_services` wires them from
settings.

**Files:** `adfarm/discord/forums.py`, `adfarm/discord/adapter.py`, `adfarm/app.py` · **Tests:** `test_v9_fixes.py::test_p2_7_*`

---

### 8. Check for Any Other Hardcoded Values ✅ CHECKED — none found

**Result:** `grep -rn '"[0-9]\{16,20\}"' adfarm/ --include="*.py"` returns nothing, and a broader
scan for id-like literals, wallet addresses and account names found only embed colour constants
(`0x5865F2` …). No change needed. A regression test and a forensic check keep it that way; this is
the one item that passes both before and after the fixes.

**Tests:** `test_v9_fixes.py::test_p2_8_*`, `forensic_repros.py P2-8`

---

### 9. Command Visibility in Discord Slash List ✅ FIXED (within platform limits)

**Fix applied:** `CommandRegistry.apply_default_permissions()` sets
`default_permissions=administrator` on `/admin` and `/help-admin` (so Discord hides them from
non-admin members outright) and marks every command `guild_only`. The tier table lives in
`security.policy.ADMIN_ONLY_COMMANDS` so it is testable without a gateway.

**Honest limitation:** Discord has no per-*user* command visibility — a command synced to a guild is
visible to every member of it or to none. "Non-customers see only `/help` and `/getstarted`" is
therefore not achievable through the slash list alone; what *is* enforced is that a stranger
invoking any other command is refused by `Guard` with a tailored message, and a test asserts exactly
`["getstarted", "help"]` is usable by a stranger in a public room.

**Files:** `adfarm/commands/registry.py`, `adfarm/security/policy.py` · **Tests:** `test_v9_fixes.py::test_p2_9_*`, `forensic_repros.py P2-9`

---

### 10. Explore for Any Other Undiscovered Issues ✅ DONE

**Fix applied:** `analysis/forensic_repros.py` did not exist, so it was written (see the header) and
run against both the pre-fix and post-fix trees. The audit below is the result.

---

## 🔎 ISSUES FOUND DURING THE AUDIT (not in the original list) — all fixed

### F10 — Undeclared workflow inputs would 422 the dispatch ✅ FIXED
`build_inputs()` emitted `buy_items` / `buy_items_price` / `buy_items_style`, which `send_ads.yml`
does not declare under `workflow_dispatch`. GitHub rejects those with `422 Unexpected input(s)`.
Latent today only because `/run` never exposes those options. Fixed by the `WORKFLOW_INPUTS`
filter, with a test that parses the YAML so the two cannot drift. — `adfarm/github/workflows.py`

### F11 — `MAX_IMAGE_BYTES` was larger than the transport allows ✅ FIXED
The limit was 8 MB, but the image now travels through the Contents API, which caps a file at 1 MB.
An 8 MB image would have passed validation and then failed at upload with an opaque 422. The limit
is 1 MB and the error message says so. — `adfarm/core/rules.py`, `adfarm/commands/customer.py`

### F12 — Repo/worker-account leakage beyond `/status` ✅ FIXED
P1-4 named `/status`; the same data also leaked through `/alt action:overview`, `/alt action:runs`,
the `/setup` confirmation and the `/run` confirmation (whose Actions URL contains the worker login
and repo name). All five are gated on `ctx.is_admin`. — `adfarm/commands/customer.py`, `adfarm/discord/embeds.py`

### F13 — `migrate()` rollback could mask the real error ✅ FIXED
`conn.execute("ROLLBACK")` ran unconditionally in the `except`, so a failed `BEGIN` raised
"cannot rollback - no transaction is active" over the top of the original failure. — `adfarm/db/database.py`

### F14 — A runner that lost the DB lease kept writing ✅ FIXED
`renew_lease()` rewrote the lock unconditionally, so a runner whose lease had expired and been taken
over would silently steal it back. It now verifies ownership first and returns `False`; the lease job
alerts admins and sets `shutdown_requested` so the runner exits instead of split-braining. —
`adfarm/db/gist_backup.py`, `adfarm/app.py`

### F15 — Dead `sync_commands` view marker ✅ FIXED
`/admin sync-commands` returned `view={"kind": "sync_commands"}`, which no renderer handled — a
marker for a button that never existed. Removed; the sync itself was already driven by the content
sentinel. — `adfarm/commands/admin.py`

### F16 — `analysis/forensic_repros.py` was referenced but missing ✅ FIXED
Written, and validated to fail on the pre-fix commit. — `analysis/forensic_repros.py`

### F17 — `adfarm/discord/permissions.py` and `adfarm/discord/policy.py` were referenced but missing ✅ FIXED
Both created; the permission matrix and the policy text each have one home now instead of being
inlined where they could drift. — `adfarm/discord/permissions.py`, `adfarm/discord/policy.py`

### F18 — `BOT_ADMIN_ROLE_ID` was provisioned but never read ✅ FIXED
`setup.py`/`GuildProvisioner` creates a "Bot Admin" role and stores its id in the `meta` table, but
`Settings` had no field for it, so customer forums never granted that role access. Added
`Settings.admin_role_id` (env `BOT_ADMIN_ROLE_ID`/`ADMIN_ROLE_ID`, also picked up from `meta` via
`with_channel_ids`) and threaded it into `ForumProvisioner`. — `adfarm/config.py`, `adfarm/app.py`

### F19 — An unresolvable customer produced a silently invisible hub ✅ FIXED
`create_customer_forum` created the forum with no member overwrite when `fetch_member` failed, so the
customer could not see their own hub and nothing was logged. It now logs a warning naming the user
and the need for a permission refresh. — `adfarm/discord/adapter.py`

### F20 — `GistBackup.flush()` clears `_pending` before the upload ⚠️ KNOWN, not changed
A failed upload leaves the pending flag cleared, so the snapshot waits for the next commit rather
than being retried on its own. Every write re-enqueues, so nothing is lost permanently, and the F06
interlock deliberately reports rather than retries. Left as-is on purpose; noted so it is not
rediscovered as a bug.

---

## ✅ VERIFICATION CHECKLIST

- [x] F01 — Webhook URLs are correct (`?thread_id=456&wait=true`, and `/messages/<id>` has no query)
- [x] F02 — Workflow flags match (`runtime_limitless: "0"/"1"`, `attach_image: "yes"/"no"`); image reaches the repo as `ad.png`
- [x] F03 — Heartbeat edits are processed (`on_message_edit` + `on_raw_message_edit`)
- [x] F04 — Lease release checks ownership; acquire is compare-and-swap
- [x] F05 — Failed BEGIN doesn't poison transactions
- [x] F06 — Backup restore doesn't overwrite with an empty DB
- [x] F09 — Renewal preserves tuning; expired customers can run `/renew`
- [x] Command titles are ≤45 characters
- [x] Non-customers can only *use* `/help` and `/getstarted`; operator commands are hidden from non-admins
- [x] `/help-admin` exists and documents all 18 admin actions
- [x] Repo names/GitHub accounts are hidden from customers (5 call sites)
- [x] Policy text is risk-free and welcoming
- [x] `/run hours:Limitless` works
- [x] Ticket panel has a working button that opens tickets
- [x] All tests pass — `pytest tests/` → **200 passed**
- [x] `python analysis/forensic_repros.py` → **15/15 forensic checks passed**
