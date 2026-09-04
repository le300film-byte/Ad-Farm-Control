# Discord Ad Sender — V8 (Enterprise Multi-Customer Service)

> **V8 (2026-09-03):** Full enterprise upgrade with multi-customer management,
> subscription timers, VIP tier, admin panel, and 24/7 continuous bot runtime.
> **One-command setup with 3-worker architecture.**
> **V8 bug-fix round:** every command is now channel-aware (public/customer/vip/admin
> tiers per channel + synced slash-command visibility), the fleet registry self-heals
> via `/admin sweep-alts`, `/reset` performs a factory wipe, and ~150 silent
> `except: pass` handlers across the codebase now log instead of swallowing.

This repository contains the canonical V8 sender and official control-bot
source for a GitHub Actions deployment. V8 transforms the bot from a single-operator
tool into a full enterprise service supporting multiple customers, each with their own
private forum channels, GitHub repos, subscription timers, and VIP features.

## 🆕 V8 Quick Start (One Command)

### Prerequisites (one-time, 5 min):
1. Install [GitHub CLI](https://cli.github.com): `brew install gh` (macOS) / `sudo apt install gh` (Ubuntu)
2. Authenticate on your **main** account: `gh auth login && gh auth refresh -s repo,workflow,gist`
3. Create **3 fresh GitHub accounts** for workers at [github.com/signup](https://github.com/signup) (these host customer alt repos — gives isolation if one gets flagged)
4. Create a Discord bot at [discord.com/developers](https://discord.com/developers/applications) — enable Message Content Intent, invite to your server

### Setup (one command):
```bash
python3 setup.py
```
The script asks for:
- **4 Discord/billing inputs:** Bot Token, User ID, Server ID, Wallet Address
- **3 worker accounts:** Username + token for each (the script opens the token creation page for you in your browser)

Everything else is automated — channels, secrets, Gist backup, database, policy card.

### Architecture:
```
Main account (gh auth)  →  core repo + control bot + Gist backup
Worker 1 (@username)    →  customer alt repos (round-robin)
Worker 2 (@username)    →  customer alt repos (round-robin)
Worker 3 (@username)    →  customer alt repos (round-robin)
```

### Launch:
```bash
git add . && git commit -m '🚀 V8 setup' && git push origin main
```

### Onboard a customer:
```
/admin activate @User days:30 alts:2
```

📖 See [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) for the full guide.

---

## V8 Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    V8 CONTROL BOT (24/7)                     │
│  Runs on: MAIN GitHub account                                │
│  • /admin panel (owner-only hidden commands)                 │
│  • Customer commands: /setup /run /stop /pause /resume etc.  │
│  • VIP commands: /squad /script (VIP customers only)         │
│  • Timer engine: hourly subscription scan + auto-shutdown    │
│  • SQLite: customers.db (Gist write-through backup)          │
└─────────────────────────┬────────────────────────────────────┘
                          │  dispatches to worker accounts
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Worker 1    │ │  Worker 2    │ │  Worker 3    │
│  alt repos   │ │  alt repos   │ │  alt repos   │
│  send_ads.py │ │  send_ads.py │ │  send_ads.py │
│  (Actions)   │ │  (Actions)   │ │  (Actions)   │
└──────────────┘ └──────────────┘ └──────────────┘
          │
          ▼
┌─────────────────────┐         ┌─────────────────────┐
│   Customer A Forum  │         │   Customer B Forum  │
│   #control          │         │   #control          │
│   #dashboard        │         │   #dashboard        │
│   #farm-logs        │         │   #farm-logs        │
│   #dm-inbox (VIP)   │         │   #deals            │
│   #deals            │         └─────────────────────┘
└─────────────────────┘
```

## V8 Modules

| File | Purpose |
|------|---------|
| `customer_manager.py` | SQLite CRUD, expiry helpers, VIP management |
| `security.py` | Global `@require_access` permission decorator |
| `github_dispatch.py` | Multi-worker repo provisioning + workflow dispatch |
| `discord_forum.py` | Customer forum channel + thread creation |
| `timer_engine.py` | Hourly subscription scan, reminders, auto-shutdown |
| `admin_commands.py` | `/admin` slash command group (hidden) |
| `gist_backup.py` | Gist write-through backup + restore-on-startup |
| `setup.py` | **One-command installer** (gh CLI auth + 3 workers) |

## V8 Command Reference

### Admin Commands (OWNER_IDS only)
| Command | Function |
|---------|----------|
| `/admin list` | Show all customers, days remaining |
| `/admin activate @User days alts vip` | Onboard a customer |
| `/admin extend @User days` | Extend subscription |
| `/admin deactivate @User` | Shut down and lock |
| `/admin shutdown confirm:ALL` | Emergency kill-switch (2-admin multi-sig) |
| `/admin repo-sync` | Push latest sender to all repos |
| `/admin logs @User` | View customer log thread |
| `/admin pin-policy` | Pin ToS in #open-ticket |
| `/admin payment-address @User` | Share wallet (policy-gated) |
| `/admin verify-tokens` | Audit worker tokens |
| `/admin expiry-alerts` | Dry-run reminder path |
| `/admin activate-template @User` | Pre-filled activation command |
| `/admin sync-commands` | Re-register the slash-command tree and re-apply channel visibility (run after deploys or channel renames) |
| `/admin sweep-alts` | Probe every `ALT_REPOS` entry against GitHub and prune confirmed-404 ghost alts from state, DB and repo secrets |
| `/reset confirm:RESET` | Owner-only factory reset: DB rows, backup files, control state, fleet registry + secrets (keeps user accounts/settings/repos intact) |

### Channel Policy (bug-fix round)
Slash commands are gated by *where* they are invoked — channel role decides the
tier ceiling, the per-command tier must fit inside it (owners bypass the channel
check entirely):

| Tier | Commands | Default channels (name match, case-insensitive) |
|------|----------|--------------------------------------------------|
| public | `/help`, `/getstarted` | `welcome-about`, `pricing-plans`, `announcements` |
| customer | `/setup` `/run` `/stop` `/pause` `/resume` `/tune` `/channels` `/deals` `/status` `/reply` `/refresh` `/dashboard` `/shutdown` `/alt` `/renew` `/pause-billing` `/proofs` | `control`, `dashboard`, `farm-logs`, `deals`, `open-ticket`, `tickets` |
| vip | `/squad` `/script` `/vip` | `dm-inbox` |
| admin | `/admin …`, `/reset` | `admin-commands`, `admin-alerts`, `admin-chat`, `audit-logs` |

Override via env with channel IDs **or** names (comma-separated):
`PUBLIC_CHANNELS`, `CUSTOMER_CHANNELS`, `VIP_CHANNELS`, `ADMIN_CHANNELS`,
and `CUSTOMER_HUB_MARKER` (pattern that marks a customer's own hub room as
customer-tier anywhere). Unclassified channels allow **public tier only**.
On startup (and after `/admin sync-commands`) the guild is switched to
**private registration** so commands only exist where allowed — one API call
per command, and a human-readable summary is written to `#control` and the
log channel. `CUSTOMER_GUILD_ID=0` (self-serve servers) skips guild-wide
registration entirely, so every slash command stays invisible by design.

### Customer Commands (active subscription required)
`/setup`, `/run`, `/stop`, `/pause`, `/resume`, `/alt`, `/tune`, `/channels`,
`/deals`, `/squad`, `/status`, `/reply`, `/refresh`, `/dashboard`, `/help`,
`/shutdown`, `/renew`, `/pause-billing`, `/proofs`

### VIP Commands (VIP tier required)
`/squad`, `/script simulate`, `/script run`, `#dm-inbox` visibility

---

> **Safety:** This is user-account automation, not an official Discord bot
> feature. Use only accounts and servers you control. Never use it for
> harassment, fraud, spam, or unsolicited bulk messaging.

## Start here

- **Setup:** run [`python3 setup.py`](./setup.py) then [`SETUP_CONTROL.md`](./SETUP_CONTROL.md)
- **Customer guide:** [`SETUP_GUIDE.md`](./SETUP_GUIDE.md)
- **AI Co-Pilot:** [`SKILL.md`](./SKILL.md)
- **Roadmap:** [`ROADMAP.md`](./ROADMAP.md)

## Files

| Path | Runs where | Purpose |
|---|---|---|
| `send_ads.py` | Worker alt repos (Actions) | Canonical sender, heartbeat, deal scanner. |
| `.github/workflows/send_ads.yml` | Worker alt repos | Chained six-hour sender chunks, WARP routing. |
| `.github/workflows/self_check.yml` | Worker alt repos | Pre-flight validation. |
| `.github/workflows/control_bot.yml` | Main repo | 24/7 control bot with 8-chunk watchdog. |
| `.github/workflows/sync_to_alts.yml` | Main repo | Copies sender + workflows to all alt repos. |
| `control_bot/` | Main repo | Slash commands, live state, dashboard. |
| `setup.py` | Main repo | **One-command installer (main + 3 workers).** |

See [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) for full setup and operations.
