# Ad Farm Control — five-step setup

This guide is the short path for the current architecture. The core repository
contains the official Discord control bot, the canonical `send_ads.py`, the
alt workflows, and the bootstrap utility.

By the end you will have:

- one private control server with `#control`, `#dashboard`, `#dm-inbox`, and
  `#farm-logs`;
- one official Discord bot running in a GitHub Actions job;
- one private repository per configured alt, with its own Actions job and
  Cloudflare WARP step;
- two private Gists for the blocklist and live control overrides; and
- three webhooks total: DM inbox, dashboard, and consolidated farm logs.

> **Important:** Discord user-account automation is not an official Discord
> bot feature and can violate Discord's Terms of Service. Use only accounts
> and servers you control, expect accounts to be restricted, and never use
> this system for harassment, spam, fraud, or unsolicited bulk messages.

## What changed

- `python setup.py` is the primary setup path.
- GitHub CLI authentication supplies one shared GitHub token. No separate
  tokens need to be created for dispatch, sync, and Gists.
- `/run` has no slash-command arguments. It opens a two-page modal containing
  the ten run fields. Discord allows only five text-input rows per modal, so
  the second form opens immediately after the first is submitted.
- Seller/buyer presentation is derived from the latest alt heartbeat
  `ad_type` (`sell` or `buy`), not from a static mapping.
- `TUNING_JSON` is an optional JSON secret. All tuning values have safe code
  defaults, and an explicitly supplied individual environment value wins over
  the JSON value.
- Deal alerts go to the dashboard webhook; they do not require a fourth
  webhook or a separate channel.

## Before you begin

You need:

1. A GitHub account with access to this core repository and permission to
   create private repositories in the chosen username or organization.
2. GitHub CLI 2.x installed and authenticated:

   ```bash
   gh auth login
   gh auth status
   ```

3. An official Discord application with a bot user. Enable **Message Content
   Intent** in the Developer Portal. Invite the bot to your private control
   server with the `bot` and `applications.commands` scopes and permissions to
   view/send messages, embed links, read history, manage messages, manage
   channels, and manage webhooks.
4. The numeric ID of your private control server. The cloud bootstrap requires
   this non-secret routing value; local interactive setup can discover it when
   Discord returns exactly one visible server, or prompt for it otherwise.
   Enable Discord developer mode if you need to copy IDs.
5. The alt user tokens you are authorized to use. The bootstrap validates each
   token with `GET /users/@me` and derives its Discord ID automatically. Do not
   paste tokens into chat, commit them, or put them in a normal workflow input.
6. The numeric IDs of the trading channels where each alt is already allowed
   to post. The bootstrap uses one comma-separated channel list for all alts;
   use per-run channel inputs later if your installation needs a different
   layout.

## Step 1 — Prepare Discord and GitHub CLI

Create the private Discord server, the official bot application, and the bot
invite described above. Give the bot access to the server before continuing;
the Discord API can create channels and webhooks inside an existing server but
cannot create the server itself.

Authenticate `gh` and make sure the checkout is the core repository. The
bootstrap refreshes the `workflow` and `gist` scopes when run locally:

```bash
gh auth login
gh auth status
cd /path/to/adfarm-core
```

The GitHub account represented by `gh auth token` is used for the alt repos,
workflow dispatch, file sync, and the two private Gists. The script never
prints that token.

## Step 2 — Run the local bootstrap (recommended)

From the repository root:

```bash
python3 setup.py
```

The script will:

1. verify `gh` and run `gh auth refresh -s workflow,gist`;
2. ask for the official bot token, your Discord ID, trading channel IDs, and
   up to four alt tokens;
3. validate the official bot and every alt through `/users/@me`;
4. discover the control server, or ask for its ID when the bot is in several
   servers;
5. create or reuse the four private control channels;
6. create or reuse exactly three webhook destinations;
7. create private `altN-sell`/`altN-buy` repositories under the selected
   GitHub owner and upload `send_ads.py`, `send_ads.yml`, and
   `self_check.yml` through the Contents API;
8. create the blocklist and control Gists;
9. set core secrets, alt variables, and alt secrets; and
10. dispatch each alt self-check, report every result, and continue by default
    if one fails.

The prompts also ask for a friendly alt name and its initial repository mode.
The mode is only a naming/default hint: the live `ad_type` in each heartbeat
is what the dashboard uses.

The script finishes by printing a bot invite URL and the next Actions steps.
If a rerun finds an existing repository, channel, or named webhook, it reuses
it where the API exposes the resource and does not create a duplicate. Existing
GitHub secrets and variables are preserved by default. To intentionally replace
them, add `--force`; to make any self-check failure fatal, add
`--abort-on-failure`:

```bash
python3 setup.py --force --abort-on-failure
```

In interactive mode, the script asks before replacing an existing secret or
variable. In non-interactive mode, existing values are preserved unless
`--force` is supplied.

### Required alt configuration collected by the script

The trading channel IDs are not the newly created control-channel IDs. Supply
IDs such as `123...,456...` for the actual marketplace channels. Optional
channel names in the same order, such as `trading,market`, enable the existing
404 auto-discovery safety path.

For a non-interactive local run, values can be supplied as environment
variables, but tokens should still be entered through a protected shell
mechanism. The supported names are `BOT_TOKEN`, `OWNER_ID`, `GUILD_ID`,
`ALT_COUNT`, `ALT_TOKEN_1` through `ALT_TOKEN_4`, `ALT_NAME_1` through
`ALT_NAME_4`, `ALT_TYPE_1` through `ALT_TYPE_4`, `CHANNEL_IDS`,
`CHANNEL_NAMES`, `GITHUB_OWNER`, and `TUNING_JSON`.

## Adding an alt later

To add another alt after the initial setup, use the **🧰 Bootstrap Ad Farm**
workflow again (or rerun `python3 setup.py` locally) with `alt_count`/`ALT_COUNT`
set to the new total, up to four. Enter the new alt token and optional name when
prompted. Existing repositories, channels, webhooks, and ordinary secret/variable
values are preserved by default; use **force** only when you intentionally want
to replace an existing value. The aggregate alt mappings are refreshed so the
control bot sees the larger installation. Existing alt repositories retain
their Gist IDs; a new alt needs the shared Gist IDs supplied through the
optional `GIST_ID` and `CONTROL_GIST_ID` environment values (or
`BOOTSTRAP_GIST_ID` and `BOOTSTRAP_CONTROL_GIST_ID` secrets in the cloud
workflow). During an interactive local rerun, enter those existing IDs when
prompted. Otherwise the bootstrap creates private Gists for it. The bootstrap
then creates the new
`altN-sell`/`altN-buy` repository, configures its mappings, uploads the three
workflow files, and runs its self-check.

Do not paste tokens into workflow inputs or commit them. For the cloud path,
store the additional masked `BOOTSTRAP_ALT_TOKEN_N` secret before dispatching
the bootstrap workflow. Keep the existing `BOOTSTRAP_ALT_TOKEN_1` through
`BOOTSTRAP_ALT_TOKEN_N` secrets available because the non-interactive workflow
validates every selected alt; add `BOOTSTRAP_GIST_ID` and
`BOOTSTRAP_CONTROL_GIST_ID` when the new alt should use the existing shared
Gists.

## Step 3 — Confirm the alt workflows

The bootstrap places the workflow files in every alt repository and runs
`self_check.yml`. A successful check validates, as applicable:

- the alt token and its Discord identity;
- each configured marketplace channel;
- the three shared webhook URLs;
- both Gists and trusted Discord IDs;
- WARP/proxy routing; and
- the `send_ads.py --self-test` suite.

If a check fails, fix the exact item in the corresponding repository secret
or variable and run **Actions → Self-Check Secrets → Run workflow** again.
Typical causes are a channel the alt cannot see, an expired user token, or a
bot that lacks Manage Webhooks in the control server.

When the canonical `send_ads.py` or an alt workflow changes in the core repo,
`sync_to_alts.yml` updates the configured alt repositories. It uses the same
`GH_TOKEN` secret that the bootstrap obtains from `gh auth token`.

## Step 4 — Start the control bot on GitHub Actions

The official bot runs only in the core repository's **🤖 Control Bot**
workflow. It is intentionally a six-hour Actions chunk; alt jobs are
independent and continue posting or forwarding webhook events when the
control-bot chunk ends.

1. Open the core repository's **Actions** tab.
2. Select **🤖 Control Bot → Run workflow**.
3. Select `6` hours and run it.
4. Wait for the bot to log in and sync guild commands.

The optional cron in `control_bot.yml` starts a daily chunk. Cancel the
workflow from Actions when you want the official bot offline. While it is
offline, webhook heartbeats, logs, deal alerts, and DM forwarding can still
arrive; slash commands and dashboard editing require the bot to be online.

The bot requires these core settings, all created by the bootstrap:

| Setting | Purpose |
|---|---|
| `BOT_TOKEN` | Official Discord bot token |
| `GUILD_ID`, `CONTROL_CH_ID`, `DASHBOARD_CH_ID`, `LOG_CH_ID` | Control server routing |
| `OWNER_IDS` | Authorized Discord operators |
| `GH_TOKEN`, `ALT_GITHUB_OWNER`, `ALT_REPOS` | GitHub dispatch and sync |
| `ALT_DISCORD_IDS`, `ALT_NAMES` | DM routing and display names |
| `TUNING_JSON` | Optional shared bot tuning object |

## Step 5 — Run and operate the farm

In `#control`, type `/run`. The command opens page 1 with:

- `alt_id` — `1` through `4`;
- `ad_type` — `sell` or `buy`;
- `sell_rate` — for example `2.5$` (in buy mode this is the token rate);
- `sell_extra` — text appended to a sell ad; and
- `buy_rate_rap` — for example `1.8`.

Submit page 1. Page 2 asks for:

- `buy_style` — `detailed` or `simple`;
- `buy_simple_text` — the complete simple-buy message;
- `interval_min` — `3` or `5`;
- `total_hours` — `6`, `12`, `18`, `24`, or `48`; and
- `attach_image` — `yes` or `no`.

The form rejects unknown alts, invalid prices, unsupported durations,
unsupported intervals, invalid modes, overlong messages, and a missing
simple-buy message. After the final submit, the bot cancels the latest run for
that alt, dispatches `send_ads.yml`, and posts the run ID/result in `#control`.

Other commands are still available:

| Command | Function |
|---|---|
| `/status` | Unified dashboard, or detailed status for one alt |
| `/pause` / `/resume` | Send the corresponding remote command |
| `/stop` | Gracefully stop and cancel the alt workflow |
| `/setprice` | Change the current rate |
| `/setmode` | Change `sell`/`buy` for the current run |
| `/setmessage` | Change the current ad text |
| `/sync` | Reload the blocklist and control Gist on all alts |
| `/logs` | Show buffered lines for an alt |
| `/dashboard` | Force an immediate dashboard refresh |
| `/help` | Ephemeral list of every registered command and its description |

`CONTROL_GIST_ID` is optional. Running alts read that private Gist for
persistent or broadcast overrides; the official control bot does not write it.
The slash commands above use direct DMs and keep their changes in the current
run. Edit the Gist only through GitHub or another explicitly authorized
integration, then use `/sync` to force a reload. Without the Gist ID/token,
blocklist persistence and direct-DM control continue independently.

The summary uses the latest heartbeat mode: 💰 for `sell`, 🛒 for `buy`, and
❔ until an alt has reported a mode. All action messages in `#farm-logs` use
the alt's `ALT_NAME` as the webhook username, so one log webhook can still
be separated by alt.

## Optional cloud bootstrap

The repository also includes `.github/workflows/bootstrap.yml`. Use it when
you cannot run Python locally:

1. In the core repository, add these **repository secrets** before dispatching
   the workflow:

   | Secret | Value |
   |---|---|
   | `BOOTSTRAP_GH_TOKEN` | the value from `gh auth token`, with `workflow` and `gist` access |
   | `BOOTSTRAP_BOT_TOKEN` | official bot token |
   | `BOOTSTRAP_ALT_TOKEN_1` … `BOOTSTRAP_ALT_TOKEN_4` | alt tokens for the selected count |
   | `BOOTSTRAP_TUNING_JSON` | optional JSON object |

   GitHub masks these secret values. Do not enter tokens into the visible
   `workflow_dispatch` form: GitHub does not mask ordinary free-text inputs.
2. Open **Actions → 🧰 Bootstrap Ad Farm → Run workflow**.
3. Enter your Discord ID, control server ID, alt count, and trading channel
   IDs/names. These are non-secret routing values. Select **force** only when
   existing secrets/variables should be replaced; select **abort_on_failure**
   only when a failed alt self-check should stop setup.
4. Run it and review the job's self-check summary. Failures are reported and
   the workflow continues by default.

The cloud workflow maps those masked secrets to the same
`setup.py --non-interactive` flow and passes the selected `--force` and
`--abort-on-failure` flags. It skips the interactive scope-refresh prompt, so
refresh the local token scopes before saving `BOOTSTRAP_GH_TOKEN`.
The bot must already be invited to the target control server.

## Tuning JSON

No tuning secret is required. The sender and control bot use code defaults.
To change shared tuning in one place, set the optional `TUNING_JSON` secret in
the core and alt repositories (the local bootstrap copies it):

```json
{
  "DASHBOARD_REFRESH_SEC": 300,
  "OFFLINE_AFTER_SEC": 900,
  "CMD_COOLDOWN_SEC": 5,
  "HEARTBEAT_INTERVAL_SEC": 300,
  "WARMUP_POSTS": 3,
  "RANDOM_REACT": true,
  "TYPO_EDIT_CHANCE": 0.18,
  "IMAGE_JITTER": true,
  "DEAL_SCAN_ENABLED": true,
  "DEAL_ALERT_DELTA": 0.05,
  "IP_HEALTH_CHECK_INTERVAL_MIN": 30,
  "IP_HEALTH_PAUSE_MIN": 10,
  "CAUTION_WINDOW": 3,
  "CAUTION_FAIL_THRESHOLD": 2,
  "CAUTION_EXIT_STREAK": 3,
  "CAUTION_INTERVAL_MULT": 2.0,
  "PANIC_CHECK_INTERVAL_SEC": 120,
  "NEW_LOCATION_TIMEOUT_SEC": 30,
  "RATELIMIT_PREADJUST": true,
  "RATELIMIT_JITTER": 0.05,
  "DM_PAUSE_MINUTES": 2,
  "FORWARD_OWN_DMS": true,
  "DISCORD_LOCALE": "en-US",
  "DISCORD_TIMEZONE": "America/New_York"
}
```

Values may be strings or native JSON numbers/booleans. An explicitly present
individual Actions environment setting takes precedence, which preserves the
existing workflow-input behavior. Never put `BOT_TOKEN`, user tokens,
webhook URLs, or the GitHub token in this object.

## Security and troubleshooting

- Keep the control server private and restrict `OWNER_IDS` to trusted users.
- Keep webhook URLs and user tokens out of commits, screenshots, issue
  reports, and normal workflow inputs.
- If `/run` is missing, restart the control-bot workflow and wait for guild
  command sync. Check that the bot is in the configured server.
- If `/run` reports a GitHub error, verify `GH_TOKEN`, `ALT_GITHUB_OWNER`, the
  `ALT_REPOS` mapping, and that the alt repository contains `send_ads.yml`.
- If the dashboard shows an alt as ❔, wait for its first structured heartbeat.
- If an alt self-check returns 401, replace its `USER_TOKEN` secret. A 403 on
  a channel means that alt cannot see or post in that marketplace channel.
- If webhooks fail, recreate them with the bot's Manage Webhooks permission;
  the bootstrap can be rerun after correcting permissions.
- If WARP routing fails, rerun the self-check or the alt workflow. Do not
  continue a run whose routing check reports a known cloud datacenter.
- Alts do not need to join the private control server. They send data to the
  three webhooks and receive remote commands by direct message.

## Architecture at a glance

| Component | Location | Function |
|---|---|---|
| Official control bot | Core `control_bot.yml` Actions job | Slash commands, dashboard, DM routing, dispatch |
| Alt sender | One alt repo per account | Marketplace posting, WARP, gateway, heartbeats |
| Self-check | Each alt repo | Token, channels, webhooks, Gists, routing, self-test |
| Sync | Core `sync_to_alts.yml` | Copies canonical sender/workflows to each alt |
| Dashboard webhook | `#dashboard` | Heartbeats and deal alerts |
| Log webhook | `#farm-logs` | All action logs, labelled by webhook username |
| DM webhook | `#dm-inbox` | Buyer and alt DM forwarding |
| Blocklist Gist | Private GitHub Gist | Cross-run blocked-variation state |
| Control Gist | Private GitHub Gist | Runtime pause/rate/mode/message overrides |

The sender jobs are independent of the official bot's six-hour runtime. This
keeps posting and webhook delivery separate from command availability while
making the setup reproducible from one bootstrap script.
