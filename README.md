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
  [`SETUP_CONTROL.md`](./SETUP_CONTROL.md). The bootstrap detects and reuses
  existing repositories, private channels, named webhooks, Gists, secrets,
  variables, and channel mappings where possible.
- **Single-alt deployment:** see [`SETUP_GUIDE.md`](./SETUP_GUIDE.md).
- **Architectural roadmap & quick wins:** see [`ROADMAP.md`](./ROADMAP.md).

## V6 architecture

- `send_ads.py` is the fail-closed sender. It verifies the route before
  Discord warmup, supports 3/5-minute intervals, and caps every runtime at 48
  hours.
- `control_bot/` is the owner-gated central control bot. Its `/run` component
  form preserves sell/buy, image yes/no, detailed/simple buy, interval 3/5,
  and runtime 6/12/18/24/48 choices.
- The control bot workflow chains six-hour Actions jobs into a 24-hour default
  operation, with 6/12/18/24/48-hour manual runs. It is designed to remain
  online around the clock while respecting Actions job limits.
- `/help` privately documents every registered command, arguments, examples,
  permissions, and effects. `/refresh`, `/status`, and `/dashboard` use fresh
  heartbeat and GitHub workflow state.
- Dashboard heartbeats and deal alerts have separate paths. Heartbeats update
  the live dashboard; deal matches use `DEAL_WEBHOOK_URL` and `#deals` and do
  not overwrite dashboard state. The scanner requires a configured item alias
  (default: Blade Ball / BB token / BB) and `/setdealkeywords` accepts a
  comma-separated replacement list.
- Runtime slash commands use the shared private control-Gist queue, one
  `control_<ALT_ID>.json` file per alt, so alts do not need to join the control
  server. Direct DM is only a fallback when the Gist transport is unavailable.
- Heartbeats are readable embeds rather than raw JSON blocks; they include
  current mode, rate, activity, keywords, channels, warnings, and latest issue.
  Each alt updates one heartbeat message instead of posting a new raw JSON block
  every interval.
- Log lines carry typed categories such as `INFO`, `CONTROL`, `DEAL`,
  `CAUTION`, `ERROR`, and `DEBUG`. `/logs` can filter by category.
- Owner authorization is fail-closed and accepts comma-separated `OWNER_IDS`.
  Missing or malformed owner configuration disables control commands.
- Owner-only `/altadd`, `/altupdate`, `/altlist`, and `/altremove` commands
  manage the aggregate alt registry; `/selfcheck`, `/runs`, and `/pingalt`
  make health and transport checks actionable, while `/clearlogs` controls
  local log retention. `/setdealscan` and `/setdealdelta` tune the separate
  item-aware scanner without changing ad posting. Tokens are written through GitHub CLI
  stdin and are never echoed or logged. `/altremove` keeps the repository by
  default and requires the literal `DELETE` confirmation for removal.
- `/diagnose` provides a deep root-cause Causal Event Explorer for an alt's
  operational history; `/topology` renders the live visual fleet topology and
  routing relationship graph; `/simulate` provides sandboxed dry-run evaluations;
  `/squad` organizes alts into logical pools; `/policy` applies pre-packaged
  operational profiles; `/canary` performs synthetic latency and health probes;
  and `/reply` relays operator replies directly into buyer DMs.
- `send_ads.py` features dynamic Chat Velocity cadence scaling with strict
  slowmode hard floor guarantees, multi-alt channel collision staggering ($90\text{s}$ spacing),
  fine-grained cascading circuit breakers, and automated buyer DM intent classification.

## Files

| Path | Runs where | Purpose |
|---|---|---|
| `send_ads.py` | Alt repositories (Actions) | Canonical V6 sender, heartbeat, typed logging, safe verification, and separate deal scanner. |
| `.github/workflows/send_ads.yml` | Alt repositories | Chained six-hour sender chunks, optional WARP routing, keyword channel resolution, and V6 inputs. |
| `.github/workflows/self_check.yml` | Alt repositories | Validates token, channels, webhooks, Gists, routing, and `send_ads.py --self-test`. |
| `.github/workflows/control_bot.yml` | Core repository | Owner-gated control bot chained continuously by default for 24 hours; supports 48 hours manually. |
| `.github/workflows/bootstrap.yml` | Core repository | Masked cloud alternative to the local bootstrap script, including multi-owner setup. |
| `.github/workflows/sync_to_alts.yml` | Core repository | Copies canonical V6 sender and workflows to configured alt repositories. |
| `control_bot/` | Core repository | Slash commands, private run UI, live state, typed logs, dashboard, and GitHub dispatch. |
| `setup.py` | Core repository | Dynamic/reusable Discord, GitHub, webhook, Gist, owner, secret, variable, and self-check provisioning. |

## Required safety configuration

- Keep `OWNER_IDS` populated with one or more trusted Discord IDs.
- Keep user tokens, bot tokens, GitHub tokens, webhook URLs, and Gist tokens
  out of chat, commits, screenshots, and ordinary workflow inputs.
- Keep `PROXY_CHECK` fail-closed. Do not disable it to bypass an egress failure.
- A message is considered deleted only after an exact-message verification
  returns HTTP 404. A buried recent-page result, transient network failure, or
  permission response does not create a caution strike or phantom state.

See [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) for multi-alt operation and
[`SETUP_GUIDE.md`](./SETUP_GUIDE.md) for environment details.
