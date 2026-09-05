# AdFarm V9 — Operator Skill

Skill for operating the AdFarm V9 control bot (`python -m adfarm`, package `adfarm`). Use this
when activating customers, running alts, handling bans, or answering "how do I…" questions about
the bot. The legacy bot in the repo root is **not** this system — if a user references the old
`/admin repo-sync` or `ALT_REPOS` secrets, that is the legacy flow and does not apply here.

## Mental model

* A **customer** has a private Discord **forum** ("hub") with threads `control`, `dashboard`,
  `farm-logs`, `deals`, plus `dm-inbox` if VIP.
* Each customer has 1–4 **alts**. An alt = one Discord self-bot token running in one GitHub repo
  on a **worker** account. The alt posts ads into marketplace channels.
* The bot is **channel-aware**: customer commands only work inside the customer's own hub (or the
  ticket room for billing). Admin commands only work in admin rooms. This is enforced, not
  advisory.
* All state lives in `adfarm.db` (SQLite), mirrored to a backup Gist. There are **no** `ALT_REPOS`
  / `ALT_DISCORD_IDS` / `ALT_NAMES` core secrets anymore — the fleet is the database.

## Server layout (created automatically — never by hand)

`python setup.py --discord` provisions the whole guild. If a user asks "which channels do I need
to create?", the answer is **none**:

| Category | Channels | Visibility |
|---|---|---|
| `📣 AdFarm` | `#welcome-about` `#pricing-plans` `#whats-new` `#open-ticket` `#general-chat` | `@everyone` can view + post; command access is still gated by tier |
| `🛡️ AdFarm Staff` | `#admin-commands` `#admin-chat` `#audit-logs` | hidden from `@everyone`; `Bot Admin` role + `OWNER_IDS` only |
| `🏢 Customer Hub` | per-customer forums, created at activation | hidden from `@everyone`; each customer sees only their own hub |

The `Bot Admin` role is created and granted to every id in `OWNER_IDS`. All ids are stored in the
`meta` table of `adfarm.db` (`CUSTOMER_HUB_ID`, `ADMIN_CHAT_CH_ID`, `ADMIN_COMMANDS_CH_ID`,
`AUDIT_LOG_CH_ID`, `OPEN_TICKET_CH_ID`, …) and merged into `Settings` at boot — no secret copying.
Re-running `--discord` is safe and self-healing: existing channels are reused and their permissions
re-applied. If a channel was created by hand earlier, the provisioner adopts it instead of making a
duplicate.

Operator troubleshooting: if provisioning reports failures, the bot is missing **Manage Channels**
/ **Manage Roles** in the guild, or its role sits below the channels it must edit — fix the invite
permissions and re-run `python setup.py --discord`.

## Tiers (who may run what)

| Tier | Who | Where |
|---|---|---|
| `public` | anyone | anywhere |
| `customer` | active subscriber | own hub, admin room (support), ticket room (`/renew`, `/pause-billing`, `/proofs`, `/account`) |
| `vip` | active VIP | own hub, admin room |
| `admin` | `OWNER_IDS` | **admin rooms only** |

Empty `OWNER_IDS` ⇒ nobody is admin (fail-closed). Expired/inactive customers get a tailored
"subscription expired" message instead of a generic denial.

## Command reference

### Public / self-service
* `/help` — commands available to *your* tier (rendered from the policy tables).
* `/getstarted` — how the service works + payment address.
* `/account` — plan, days left, alts, policy-ack status, ticket links.

### Customer (in their hub)
* `/setup alt:<n>` — opens a private modal for the alt token + target channel IDs. Registers the
  alt (creates its repo on a worker) and stores credentials. **Must be done before `/run`.**
* `/run alt:<n>` — start posting. Options: `mode` (sell/buy), `rate` ($/1k, 0–20), `message`,
  `interval` (3/5 min), `hours` (0=limitless, 6/12/18/24/48), `policy` (stealth/aggressive/
  peak_hour/balanced), optional image. First run asks for policy acknowledgement if not yet acked.
* `/stop`, `/pause`, `/resume alt:<n>` — pause keeps the run alive (no public posts); stop sends
  `stop` to the sender (exits in 30–45 s).
* `/tune alt:<n>` — change `price`/`message`/`mode`/`interval`/`hours`/`policy` live (no restart).
* `/channels action:<view|add|remove|replace|overwrite|rescan|reset_caution> alt:<n>` — manage
  target channels (max 10). `rescan`/`reset_caution` talk to the sender directly.
* `/deals alt:<n>` — deal scanner: `keywords`, `delta` (edge $/1k), `enabled`.
* `/status alt:<n>` — live status card. `refresh:true` polls GitHub; `post:true` posts to
  `#dashboard`; `fleet:true` (admin) shows the whole fleet.
* `/reply user:<id> text:... alt:<n>` — DM a buyer through the alt.
* `/alt action:<overview|logs|clearlogs|runs|selfcheck|remove> alt:<n>` — inspect/remove.
  `remove` needs `confirm:REMOVE`.
* `/renew days:<n>`, `/pause-billing days:<n>`, `/proofs tx_hash:<0x…>` — billing, in hub or
  ticket room.

### VIP
* `/vip autoreply text:...` — set/show/disable the DM auto-reply.
* `/vip squad name:<x> alts:<1,2>` — named groups of the customer's own alts.

### Admin (admin rooms only)
* `/admin list [fleet:true]` — customers or fleet overview.
* `/admin customer user:<id>` — full customer card (forums, alts, webhooks).
* `/admin activate user:<id> alts:<n> days:<n> [vip:true] [username:...]` — create or extend.
* `/admin extend user:<id> days:<n>` — add days; auto-closes the open renewal ticket.
* `/admin deactivate user:<id> confirm:DEACTIVATE` — stop runs, lock hub, remove roles.
* `/admin vip user:<id> [enabled:false]` — toggle VIP (creates/removes `#dm-inbox`).
* `/admin alt action:<list|add|remove|sync|replace> user:<id>` — register/remove/resync/replace
  an alt. `remove` needs `confirm:REMOVE`; `add` picks the next free slot.
* `/admin repo action:<list|sync|delete>` — `sync` pushes sender files to all repos;
  `delete` needs `confirm:DELETE` and refuses repos that still belong to an active alt.
* `/admin health` — worker PAT validity, backup age + lease holder, dirty/missing alts, customers
  without webhooks, config problems.
* `/admin backup action:<now|status>` — force a snapshot / show restore chain.
* `/admin tickets` / `/admin resolve ticket:<id>` — payment tickets.
* `/admin ticket-panel channel:<id>` — post the tickets/payments card and register the channel.
* `/admin payment-address` — show the configured wallet.
* `/admin sync-commands` — re-sync slash commands.
* `/admin logs user:<id>` — event ledger.
* `/admin reset confirm:RESET` — **two-admin** factory reset (stops runs, renames alt repos
  `_DELETED_`); customer rows are kept.
* `/admin shutdown-bot confirm:SHUTDOWN` — **two-admin** stop of the current runner only; the next
  cron chunk starts a fresh one. Replaces the old customer `/shutdown`.

## Common tasks

**Onboard a new customer**
1. Take crypto, verify on-chain, then `/admin activate user:<id> alts:<n> days:<30>`.
2. Tell them to run `/setup` in their hub, then `/run`.
3. If `/admin health` shows "Customers without webhooks", run `/admin alt action:sync` for them
   (creates the per-thread webhooks and pushes them to the alt repos).

**An alt stops heartbeating**
* Check `/status alt:<n>` and `/alt action:runs alt:<n>`. If the repo is gone, `/admin health`
  marks it `missing`. A ban is auto-detected from the heartbeat/DM text → repo renamed
  `_BANNED_`, time credited, replacement repo prepared; the customer re-runs `/setup`.

**Renewal**
* Customer opens `/renew` → ticket in the ticket room. They post `/proofs tx_hash:0x…`. You verify
  on-chain and `/admin extend user:<id> days:<n>`, which closes the ticket and DMs them.

**Limitless runs keep going**
* They auto-renew every 48 h via `timers/renewal.py`. If the subscription lapses, the renewer
  stops the orphaned run instead of re-dispatching.

**Split brain / chunk hand-off**
* The DB lease in the backup Gist prevents two runners writing at once. A runner that fails to
  acquire the lease exits cleanly; the next 350-min chunk picks up where it left off from the Gist.

## Gotchas

* **Never** tell a customer to run `/shutdown` — that command no longer exists (it was a customer
  footgun). Use `/admin shutdown-bot` (two-admin).
* `/run hours:0` is limitless (auto-renewed). `/tune hours:0` is rejected — runtime is set at run
  time only.
* `admin` commands in a customer hub or public channel are **denied** — they must be issued in an
  admin room so the audit trail lives in one place.
* Tokens are stored sealed in the runner repo secrets (PyNaCl) and, if `TOKEN_VAULT_KEY` is set,
  re-encrypted at rest in `adfarm.db`. They are never shown in chat or logs.
* Do not hand-create AdFarm channels; run `python setup.py --discord` instead. A hand-made channel
  with a different name (e.g. `#admin-cmds`) will not be classified as an admin room.
* The sender (`send_ads.py`) is unchanged. If a customer's ads misbehave, the fix is in
  `new_reform/sender/`, not in the control plane — and a `/admin repo action:sync` pushes it.
