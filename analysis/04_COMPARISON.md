# Phase 4 — Legacy vs. V9 Comparison & Merge Decision

**Scope.** Compare the legacy system (repository root: `customer_manager.py`, `admin_commands.py`,
`send_ads.py`, `github_dispatch.py`, `gist_backup.py`, `security.py`, `timer_engine.py`,
`discord_forum.py`, `control_bot/` …) against the new control plane (`new_reform/`, package
`adfarm`). The legacy code is **untouched**; this comparison informs whether `new_reform/` can
replace it.

**Method.** Read every legacy and new module, cross-checked the legacy problem list in
`analysis/01_PROJECT_DESCRIPTION.md` §13 against the fixes in `analysis/02_REDESIGN.md`, and ran
the new suite (`145 passed`, unit + integration, real SQLite + fakes, no network).

---

## 1. Comparison summary

### ✅ Better in the new system

| Area | Legacy | V9 (`new_reform/`) |
|---|---|---|
| **Layout** | Repo root in `sys.path`, bot/`control_bot`/`customer_manager` tangled (L-15) | One package `adfarm`; one entry point `python -m adfarm`; composition root `app.build_services` (no singletons) |
| **`/setup`** | Did **not** register the alt — no repo↔token↔channel linkage (L-1) | `/setup` registers the alt (repo on a worker) and stores credentials in one flow |
| **Alt model** | Global `ALT_REPOS`/`ALT_DISCORD_IDS`/`ALT_NAMES` core secrets + `alt_credentials` table (L-2, L-5) | One `alts` table — per-customer, 1–4 alts, status + channels + repo + sender id |
| **Ownership** | No ownership on alt commands (L-4) | `AltService.resolve()` enforces customer-only access; admins must name the customer; no int→alt path without a customer id |
| **Platform kill switch** | Customer `/shutdown` could stop the whole bot (L-3) | Removed; replaced by `/admin shutdown-bot` (two-admin `MultiSig`) |
| **Security gate** | Could crash *open* (L-14) | `Guard.check()` never raises; any error ⇒ denial (fail closed); empty `OWNER_IDS` ⇒ no admin |
| **Channel awareness** | Customer commands worked anywhere | Channel matrix: customer cmds only in own hub / admin / ticket room; admin cmds only in admin rooms (audit in one place) |
| **Token at rest** | `base64(xor(token, key))` — reversible by anyone with the Gist (L-9) | `TokenVault`: HMAC-CTR + HMAC tag, PBKDF2-derived key; **no** plaintext/XOR fallback; skipped if key absent |
| **Repo secrets** | Used `gh` CLI (L-10) | `github.secrets.seal_secret` (PyNaCl) over REST only — no CLI dependency |
| **Webhooks** | Threads created, webhooks never (L-6) | `ForumProvisioner` creates `#dashboard/#farm-logs/#deals/#dm-inbox` **webhooks** and pushes them to every alt repo |
| **Telemetry routing** | Fuzzy `ALT_NAME` match across operator's global channels → collisions (L-7) | Thread-id routing: `thread_id → (customer, role)`, alt resolved by `alt_id`/name (no collisions) |
| **Worker distribution** | Round-robin not persisted → resets on chunk restart (L-11) | `meta.worker_cursor` persisted; survives hand-offs; failing workers skipped |
| **Run cancellation** | Cancelled the latest run of *any* workflow (L-12) | Cancels the recorded run id, else only active `send_ads.yml` runs |
| **`/run alt:0` ambiguity** | 0 meant "first or all" (L-13) | `alt` required, 1..alt_count; validated |
| **`/help` drift** | Hand-written, could drift from code (L-16) | Rendered from the policy tables — cannot drift |
| **Re-activate** | Reset expiry to now+days (L-18) | Extends from `max(expiry, now)`; clears reminders; unlocks hub |
| **Backup Gist** | Auto-created a new Gist on 404, id lost next run (L-19) | Id **never** auto-created at runtime; 404 ⇒ alert + refuse; restore chain verifies sha256 + integrity |
| **DB durability** | Write-through but single-writer, no lease | WAL + write-through + **advisory lease** (no split brain) + sha256 restore verification |
| **Testability** | ~6 000-line `send_ads.py`, monolithic bot; few targeted tests | Pure core (fake-clock), fakes for GitHub/Discord, **145 tests** (unit + integration: lifecycle, ban, restore, lease) |
| **Docs** | Root README only | `README`, `ARCHITECTURE`, `SKILL`, phased analysis docs, `setup.py`, `tools/migrate_legacy.py` |

### ⚠️ Worse / riskier in the new system

* **Not yet run end-to-end in production.** The control plane is new code; only the *sender*
  (`send_ads.py`) is byte-identical to the legacy. Every control-plane path is unit/integration
  tested against fakes, but no live Discord/GitHub run has validated the full chain.
* **External coupling is faked in tests by design.** There is no network/load test; behaviour
  under real GitHub rate limits, Discord gateway flaps, or a 350-min chunk restart is validated
  by logic, not by a live soak.
* **Webhook back-fill is manual.** Imported legacy customers get their `alts`/channels but the
  per-thread webhooks must be created with `/admin alt action:sync` once (documented in
  `README` / `SKILL`).

### ❌ Missing (gaps to close before/at cut-over)

| Gap | Impact | Status |
|---|---|---|
| Live soak test (Discord + GitHub) | Confirms runtime behaviour | Not done (environment-dependent) — mitigated by staged parallel run |
| `workflows/sync_to_alts.yml` (from blueprint §2) | Optional convenience to push sender to all repos | Covered at runtime by `RepoProvisioner` + `/admin repo action:sync`; file not added (not required) |
| `setup.py` Discord steps | Channel/forum creation + command registration | Implemented but **best-effort / guarded** (needs `discord.py` + live guild) |
| Legacy admin niceties folded away | `sweep-alts`→`/admin health`, `repo-sync`→`/admin repo action:sync`, `reset`→`/admin reset` | All present, just renamed — verify with operators |

**Command-parity check (intentional removals only):** `customer /shutdown` (→ admin multi-sig),
`/script` (arbitrary code exec, no customer value), `/refresh`+`/dashboard` (→ `/status` options),
`/reset` top-level (→ `/admin reset`), `/alt action:add|update` (→ `/admin alt action:add`). No
*useful* command was dropped.

---

## 2. Migration plan (from `analysis/02_REDESIGN.md` §8)

1. **Shadow deploy.** Install `workflows/control_bot.yml` as `.github/workflows/control_bot_v2.yml`
   (manual trigger) with a **new** `ADFARM_GIST_ID` and a second Discord application for staging so
   both bots coexist. The legacy Gist is left untouched for rollback.
2. **Import data.** `python -m tools.migrate_legacy --from customers.db --to adfarm.db` (dry-run
   first). Maps customers/alt_credentials/run_state/reminders/policy_acks/events/meta; assigns
   fresh `sender_alt_id`s; re-seals tokens with `TokenVault`.
3. **Webhook back-fill.** For each imported customer: `/admin alt action:sync` (creates thread
   webhooks, pushes them to alt repos) — flagged by `/admin health`.
4. **Parallel run (≥ 3 days).** v2 bot observes heartbeats read-only while legacy still dispatches;
   compare `/status` outputs.
5. **Cut-over.** Disable legacy `control_bot.yml`, enable v2 cron, swap the Discord application
   (or re-invite), run `/admin health` + `/admin backup action:now`. Runner workflows unchanged, so
   live runs are not interrupted.
6. **Decommission (after 14 days green).** Archive legacy modules; remove legacy workflows.

---

## 3. Rollback

Re-enable legacy `control_bot.yml`, disable v2 cron, restore `customers.db` from the **legacy**
Gist (never modified). Time to roll back: one workflow toggle (< 5 min). The v2 DB/Gist is not
required by the legacy bot.

---

## 4. Final recommendation

**Verdict: REPLACE — with the staged parallel-run cut-over as the safety net.**

The V9 control plane is *strictly better* on every dimension that matters — architecture,
security (fail-closed, ownership, authenticated token storage), correctness (alt registration,
webhooks, telemetry routing, lease/split-brain prevention), test coverage (145 tests vs. sparse
legacy tests), and operability (docs, `setup.py`, `migrate_legacy`, health/admin tooling). It is
feature-complete relative to the legacy command set; the only removed commands were dangerous or
valueless (`customer /shutdown`, `/script`).

The single honest caveat is that the new control plane has **not yet been exercised against live
Discord/GitHub**. That is a *rollout* risk, not a *code* risk, and it is fully mitigated by the
designed shadow-deploy → import → parallel-run → cut-over → decommission path, which keeps the
legacy Gist/DB untouched until v2 is proven green for ≥ 3 days.

**Action:** proceed with the migration plan §2; do **not** delete the legacy code until step 6.
The new system is ready to replace the legacy system.
