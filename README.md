# Discord Ad Sender — V6

This repository contains the canonical V6 sender and official control-bot
source for a GitHub Actions deployment. It supports one to four configured
alts, each isolated in its own private repository and workflow.

> **Safety:** This is user-account automation, not an official Discord bot
> feature. Use only accounts and servers you control. Never use it for
> harassment, fraud, spam, or unsolicited bulk messaging, and follow Discord's
> rules and the law in your jurisdiction.

## Start here

- **Multi-alt deployment:** run [`setup.py`](./setup.py), then review
  [`SETUP_CONTROL.md`](./SETUP_CONTROL.md). The interactive bootstrap automatically
  guides you through all configuration options (`--quick`, `--force`, `--forums`,
  `--upgrade-forums`, `--abort-on-failure`) with plain-English explanations.
- **Single-alt deployment:** see [`SETUP_GUIDE.md`](./SETUP_GUIDE.md).
- **AI Co-Pilot & Operator Skill:** see [`SKILL.md`](./SKILL.md) for prompt injection and complete command/keyword reference.
- **Architectural roadmap & innovations:** see [`ROADMAP.md`](./ROADMAP.md).

## V6 Architecture & Core Features

- `send_ads.py` is the fail-closed sender. It verifies egress routes before
  Discord warmup, supports 3/5-minute intervals, and caps every runtime at 48 hours.
- **Hierarchical Subcommand Architecture:** Commands are cleanly grouped into
  logical domains (`/fleet`, `/alt`, `/channel`, `/tune`, `/deals`, `/squad`)
  while preserving direct top-level commands (`/status`, `/run`, `/pause`,
  `/resume`, `/analytics`, etc.) for complete backwards compatibility.
- **Visual Fleet Analytics & Speed Matrix (`/analytics` / `/fleet analytics`):**
  Renders live ASCII progress gauges (`[▰▰▰▰▰▰▰▰▱▱]`), delivery reliability percentages,
  slowmode utilization, and inter-channel interval timelines.
- **Refined Directional Arbitrage Deal Scanner:**
  - **Supplier Arbitrage (`🟢 SUPPLIER ALERT`):** Detects other users selling under-market (`price <= buy_benchmark - delta`) and calculates discount profit margins.
  - **Premium Buyer Arbitrage (`🔵 ARBITRAGE SALE`):** Detects buyers offering high bids (`price >= sell_benchmark + delta`) and calculates net profit margins.
  - **Noise Filter:** Rejects lowball buyer bids and overpriced sellers.
- **Fleet Squad Batch Management (`/squad`):** Group alts into named pools (e.g. `Alpha Sellers`) and execute batch controls (`/squad pause`, `/squad resume`, `/squad policy`, `/squad price`) with aggregated health metrics.
- **Interactive Setup Configuration:** Running `python setup.py` interactively prompts for every CLI flag (`--quick`, `--force`, `--forums`, `--upgrade-forums`, `--abort-on-failure`) with explanations.
- **AFK Break Visibility:** Alts log visible status notices to `#farm-logs` when entering and returning from natural 10–30 min AFK breaks, and continuously poll control Gists every 15s.
- **Fuzzy & Emoji-Resistant Channel Discovery:** Strips unicode symbols and decorative formatting to match channels like `「💵」・trade-market` or `trading﹒☆˚₊࿔`.
- **Cascading Circuit Breakers & Chat Velocity:** Isolated per-channel failure tracking and dynamic posting delay auto-scaling based on server chat velocity.

## Files

| Path | Runs where | Purpose |
|---|---|---|
| `send_ads.py` | Alt repositories (Actions) | Canonical V6 sender, heartbeat, typed logging, safe verification, and directional deal scanner. |
| `.github/workflows/send_ads.yml` | Alt repositories | Chained six-hour sender chunks, Cloudflare WARP routing, keyword channel resolution, and V6 inputs. |
| `.github/workflows/self_check.yml` | Alt repositories | Pre-flight validation of tokens, channels, webhooks, Gists, WARP egress, and test suites. |
| `.github/workflows/control_bot.yml` | Core repository | Owner-gated control bot chained continuously for 24/7 operations; supports 6–48h runs. |
| `.github/workflows/bootstrap.yml` | Core repository | Masked cloud alternative to the local bootstrap script, including multi-owner setup. |
| `.github/workflows/sync_to_alts.yml` | Core repository | Copies canonical V6 sender and workflows to configured alt repositories. |
| `control_bot/` | Core repository | Hierarchical slash commands, private run UI, live state, typed logs, visual analytics, dashboard, and GitHub dispatch. |
| `setup.py` | Core repository | Interactive bootstrap with explanations for all safety flags, Discord/GitHub resource provisioning. |

## Required safety configuration

- Keep `OWNER_IDS` populated with one or more trusted Discord IDs.
- Keep user tokens, bot tokens, GitHub tokens, webhook URLs, and Gist tokens
  out of chat, commits, screenshots, and ordinary workflow inputs.
- Keep `PROXY_CHECK` fail-closed. Do not disable it to bypass an egress failure.
- A message is considered deleted only after an exact-message verification
  returns HTTP 404. Transient network failures do not create caution strikes.

See [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) for multi-alt operation and
[`SETUP_GUIDE.md`](./SETUP_GUIDE.md) for environment details.
