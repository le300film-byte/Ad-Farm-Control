# Discord Ad Sender — v5.5.1

This repository contains the canonical sender and the official control-bot
source for a GitHub Actions deployment. It supports one or more configured
alts, each isolated in its own private repository and workflow.

## Start here

- **Multi-alt deployment:** run [`setup.py`](./setup.py), then review
  [`SETUP_CONTROL.md`](./SETUP_CONTROL.md). The bootstrap creates the private
  Discord channels, three shared webhooks, alt repositories, Gists, secrets,
  and self-check runs.
- **Single-alt deployment without the control bot:** see
  [`SETUP_GUIDE.md`](./SETUP_GUIDE.md).

## Files

| Path | Runs where | Purpose |
|---|---|---|
| `send_ads.py` | Alt repositories (Actions) | Canonical self-bot sender and heartbeat/log/webhook client. |
| `.github/workflows/send_ads.yml` | Alt repositories | Six-hour sender chunks with optional WARP routing. |
| `.github/workflows/self_check.yml` | Alt repositories | Validates token, channels, webhooks, Gists, routing, and sender self-tests. |
| `.github/workflows/control_bot.yml` | Core repository only | Official Discord control bot. Start it manually or use its optional schedule; each run is a six-hour chunk. |
| `.github/workflows/bootstrap.yml` | Core repository | Cloud alternative to the local bootstrap script. |
| `.github/workflows/sync_to_alts.yml` | Core repository only | Copies the canonical sender and alt workflows to configured repositories. |
| `control_bot/` | Core repository | Slash commands, modal run form, dashboard state, and GitHub dispatch code. |

## Current architecture highlights

- `/run` opens a two-page modal because Discord permits five text-input rows
  per modal. It validates all ten run fields before dispatching an alt; `/help`
  privately lists every registered command and its description.
- The latest heartbeat `ad_type` drives seller/buyer dashboard presentation;
  there is no static role secret.
- `TUNING_JSON` is an optional shared tuning object. Code defaults remain
  available when it is absent.
- The three shared webhooks are DM inbox, dashboard, and consolidated farm
  logs. Deal alerts use the dashboard webhook, and log messages use the alt
  name as their webhook username.
- `GH_TOKEN` is populated from `gh auth token` and is reused for workflow
  dispatch, repository sync, and Gists.

## Reference defaults

- Heartbeat 300s · Gist sync 45s · dashboard refresh 300s · offline threshold 900s.
- Dashboard colors: 🟢 `0x57F287` active · 🟡 `0xFEE75C` paused · 🔴 `0xED4245` stopped · 🔵 `0x5865F2` AFK · ⚫ `0x2F3136` offline.
