# 📨 Discord Ad Sender — Setup Guide (v5.5.1, single-alt standalone)

Posts one ad at a time (SELL or BUY) to your Discord marketplace channels,
running entirely on **GitHub Actions cloud** (no PC/phone required 24/7).
Mimics a real person on Chrome: real TLS fingerprint (curl_cffi
impersonate=Chrome), real WebSocket gateway (account appears online),
natural typing/typo-edit/jitter/reactions, per-channel cooldown awareness,
auto AFK breaks, shadowban/caution detection, IP health monitoring,
optional deal scanning, and optional auto-recovery when channels get
deleted and recreated.

> **Running multiple alts (2 sellers + 2 buyers) with a unified control
> dashboard and slash commands?** Use the control bot instead — see
> [`SETUP_CONTROL.md`](./SETUP_CONTROL.md). That guide covers the full
> central server; this page is the **single-alt standalone** flow.

---

## ⚠️ Risk / honest talk first

Self-bots violate Discord's Terms of Service. Use throwaway alt accounts,
don't run on your main, and don't be surprised if an account eventually
gets banned. The code is designed to be as human-like as possible (real
browser fingerprint, gateway presence, typing, reactions, AFK breaks,
jitter, caution mode), but there is **no such thing as undetectable** —
you trade time/convenience for risk. By using this you accept that.

Use at your own risk. Don't spam. Don't hit the same channel faster than
its slowmode. Don't post faster than a real human would.

---

## 📌 1. What you need

- A Discord **alt account** (not your main).
- The alt already **in the server(s)** you want to post in, with permission
  to send messages in the target channels.
- A **GitHub account** (free tier is enough — public repo is fine, private
  is even safer so your workflow isn't public).
- One or more text channels (e.g. `#trading`, `#💵・market`) to post in.

### Anti-detection stack (the short version)

- Cloudflare WARP baked into the workflow (the outbound route is verified
  before posting and known cloud datacenter providers are rejected; WARP is
  VPN-class, not a guarantee of residential egress).
- curl_cffi `impersonate="chrome"` (real JA3/HTTP2 fingerprint, identical
  to Chrome).
- Real WebSocket gateway connection so the account shows **Online** with
  the correct status.
- Typing indicator + random delay before each post, scaled to message length.
- ~18% chance of a "typo edit" 5–22s after posting (natural correction).
- Random idle reactions on other people's messages (cooldown-gated).
- Per-channel slowmode awareness + rate-limit bucket handling (429-safe).
- IP health check: pauses automatically if WARP drops onto a datacenter IP.
- Shadowban/caution mode: backs off interval after consecutive silent
  failures, exits after N survives.
- Random AFK breaks (configured defaults: 2–4 per 6h chunk, 10–30 min
  each) — goes idle, then continues after.
- "Warmup": first 3 posts per channel are text-only (no image) to mimic a
  real user warming up.
- Auto-learn blocklist: remembers which variations got deleted and stops
  reusing them (persisted in a Gist across runs; survives workflow
  restarts).
- v5.4 auto-discovery: after a real posting attempt receives HTTP 404 for a
  configured channel, the bot searches the guild for a same-name channel and
  asks a trusted user (via reaction) to confirm the new ID. Startup probes do
  not trigger discovery, and an empty trusted-ID list always fails closed.
- v5.5 remote control: accept `!setprice`, `!setmode`, `!pause`, `!stop`,
  `!sync`, `!status` DMs from trusted IDs; push structured heartbeats to a
  webhook; sync runtime overrides from a shared gist.
- v5.5.1: deal alerts use `DASHBOARD_WEBHOOK_URL`; all control webhooks
  use 20s timeout (future-proof `WEBHOOK_TIMEOUT` constant).

---

## 2. Getting your alt's token (desktop browser)

1. Open Discord in Chrome/Edge/Firefox and **log in as the alt**.
2. Press **F12** → **Network** tab → filter by `api`.
3. Refresh Discord (F5).
4. Click any request to `discord.com/api/...` → **Request Headers** → copy
   the value of the `authorization` header (a long ~70-char token).
5. **Keep this secret.** Anyone who has it can fully control the alt.

> ⚠️ Don't log out of the alt on your browser afterwards — logging out
> invalidates the token. Just close the tab.

---

## 3. Getting channel IDs

Enable Developer Mode: **User Settings → Advanced → Developer Mode** (on).
Right-click each target channel → **Copy Channel ID**. The example Sevilla
alts use:

- `#trading` → `1541658382015135817` (180s slowmode)
- `#💵・market` → `1103759996468080752` (300s slowmode)

Also copy the channel **name** (lowercase, without leading emoji, e.g.
`trading` and `market`) — used by v5.4 auto-discovery.

---

## 4. Aging the alt (DO THIS BEFORE RUNNING)

**Brand-new / freshly-joined accounts get banned almost instantly.**

Before running the bot:
1. Join the target servers and sit idle for at least **3–5 days**.
2. Browse channels manually, react to a few posts, send a couple of
   legitimate messages, scroll, click around. Build a small history so the
   account looks like a real human.
3. Join a few non-trading servers too — being in only one server is a
   red flag.

Skipping aging is the #1 reason people get insta-banned.

---

## 5. Installation (GitHub Actions)

### Step 1 — Create the repository

1. GitHub → **New repository**. Name it anything (e.g. `discord-ad-sender`).
   **Private** recommended.
2. Upload these files from this folder into the repo:
   - `send_ads.py`
   - `.github/workflows/send_ads.yml`

You can drag-and-drop them into the GitHub web UI, or `git push`. Make
sure the `.github/workflows/` folder structure matches exactly (GitHub is
picky about that).

### Step 2 — Add GitHub Secrets

Go to **Repo → Settings → Secrets and variables → Actions → New repository
secret** for each of the following.

#### Required

| Secret | Example value | Notes |
|---|---|---|
| `USER_TOKEN` | `abc123...xyz` | The alt's auth token from step 2. |
| `CHANNEL_IDS` | `1541658382015135817,1103759996468080752` | Comma-separated target channel IDs. |

#### 📌 WARP is automatic — no secret required

The workflow installs Cloudflare WARP and connects to it before starting
the bot. It verifies the measured outbound organization and aborts if the
IP is a known cloud datacenter or outside `ALLOWED_COUNTRIES`. You don't
need to supply a proxy. If you *do* want to use your own proxy instead of
WARP, set `HTTPS_PROXY` (or `HTTP_PROXY`) as a secret, for example
`http://user:pass@host:port`; the workflow validates the proxy's actual
egress before posting.

#### Recommended (optional but strongly suggested)

These all have safe defaults — set them only if you want non-default
behavior.

| Secret | Default | What it does |
|---|---|---|
| `LOG_WEBHOOK_URL` | _(none)_ | Discord webhook URL that receives a stream of plain-text action log lines (startup, sends, failures, DMs, shutdown) — great for monitoring. |
| `DASHBOARD_WEBHOOK_URL` | _(none)_ | Discord webhook URL that receives rich 💓 heartbeat embeds every 5 min (status, sent/err counts, per-channel alive/dead, rate, uptime). |
| `WEBHOOK_TIMEOUT` | `20` | v5.5.1 timeout (seconds) for log/dashboard/deal webhooks. |
| `DM_WEBHOOK_TIMEOUT` | `20` | v5.5.1 timeout (seconds) for the DM-forward webhook. |

#### 📌 DM forwarding — see buyer DMs without logging in

| Secret | Default | What it does |
|---|---|---|
| `DM_WEBHOOK_URL` | _(none)_ | Discord webhook URL. All DMs the alt receives get forwarded here as embeds, including attachments. The bot auto-pauses public posting for `DM_PAUSE_MINUTES` (default 2) after every inbound DM so you can reply safely. |
| `DM_PAUSE_MINUTES` | `2` | Minutes to pause public posts after a buyer DMs. |
| `FORWARD_OWN_DMS` | `true` | Also forward messages *the alt sends* (so you can see both sides of the convo). |

#### 📌 Auto-learn — remembers which messages got blocked

| Secret | Default | What it does |
|---|---|---|
| `GIST_TOKEN` | _(none)_ | Shared GitHub token from `gh auth token` with `gist` scope. Required for blocklist persistence. |
| `GIST_ID` | _(none)_ | Secret gist ID (create an empty secret gist named `blocked_variations.json`; first run creates the file). |
| `BLOCKED_STRIKES` | `2` | How many times a variation must fail to be auto-blocked. |
| `BLOCKED_SAFETY_STOP` | `5` | Kill the run after this many NEW variations get blocked in a row (something's wrong — e.g. channel is dead). |

#### 📌 Geo-country check

| Secret | Default | What it does |
|---|---|---|
| `ALLOWED_COUNTRIES` | _(empty)_ | Optional comma-separated ISO codes (e.g. `FR,ES,NL,DE,IE,GB,PT,MA,IT`). If WARP/proxy exits in a country NOT on this list, the run aborts before posting. Empty disables only the country allow-list; outbound IP and provider verification remain mandatory. |

#### 📌 Deal scanner (v5.3)

Passively scans messages it already fetches (no extra API calls) and
alerts when someone posts a rate better than yours by ≥ delta.

| Secret | Default | What it does |
|---|---|---|
| `DEAL_SCAN_ENABLED` | `true` | On/off. |
| `DEAL_ALERT_DELTA` | `0.05` | Edge threshold ($ per 1k) to trigger an alert. |
| `DEAL_MY_RATE` | `0` (auto) | Your current rate. Default 0 = auto-extracted from the ad text at runtime. |

#### 📌 Auto channel discovery (v5.4)

If a channel returns 404 (deleted/recreated), the bot browses the guild
for a same-name channel and DMs the channel a reaction-confirmation
request.

| Secret | Default | What it does |
|---|---|---|
| `CHANNEL_NAMES` | _(empty)_ | Comma-separated names matching `CHANNEL_IDS` positionally (e.g. `trading,market`). When empty, discovery is off. |
| `CONFIRM_USER_IDS` | _(empty)_ | Comma-separated Discord IDs whose ✅ reaction confirms a replacement. Empty means discovery is disabled (fail-closed); it never authorizes everyone. |
| `CONFIRM_TIMEOUT` | `60` | Seconds to wait for a reaction before giving up. |

#### 📌 Remote control (v5.5)

Lets you issue DM `!commands` to the alt from trusted Discord user IDs
(including the central control bot if you use one).

| Secret | Default | What it does |
|---|---|---|
| `CONTROLLER_USER_IDS` | _(empty)_ | Comma-separated Discord IDs allowed to send `!setprice`, `!pause`, `!resume`, `!stop`, `!sync`, `!status`, `!setmessage`, `!setmode` via DM. |
| `CONTROL_GIST_ID` | _(empty)_ | Optional shared gist ID for broadcast overrides. The sender reads it; edit it in GitHub or through a separately authorized integration. The official control bot's slash commands use direct DMs and do not write this gist. |
| `HEARTBEAT_INTERVAL_SEC` | `300` | Seconds between heartbeat pushes to `DASHBOARD_WEBHOOK_URL` (min 60). |
| `SYNC_GIST_INTERVAL_SEC` | `45` | Seconds between gist polls (min 15). |
| `CONTROL_CMD_PREFIX` | `!` | Command prefix character. |
| `ALT_ID` | `0` | Numeric alt ID shown on heartbeats (use `1`–`N` if you have a control dashboard; drives personality jitter when ≥1). |
| `ALT_NAME` | `Alt{ALT_ID}` | Friendly name shown on the dashboard. |
| `PERSONALITY_JITTER` | `0.12` | v5.5.1 — per-ALT_ID deterministic nudge of typo-chance/react-chance/AFK frequency within ±12% so multiple alts don't share an identical behavioral fingerprint. Set `0` to disable. |

> Per-run choices (`ad_type`, rate, message, interval, hours, image,
> channel override) are **inputs on the workflow_dispatch form**, not
> secrets. You pick them each time you click "Run workflow" (or they're
> pre-set by the control bot when it dispatches via slash command).

---

## 6. Running the bot

### Triggering a run

1. **Repo → Actions → "Send Ads" → Run workflow**.
2. Fill in the form:
   - **Ad type**: `sell` or `buy`.
   - **[sell]** `sell_rate` (e.g. `2.5$`) and `sell_extra` text.
   - **[buy detailed]** `buy_rate`, `buy_rate_rap`; or **[buy simple]** `buy_simple_text`.
   - **[optional]** `channel_1` / `channel_2` to post ONLY to those channels for
     this run (overrides `CHANNEL_IDS`). Add `channel_1_name` / `channel_2_name`
     if you want v5.4 auto-discovery for the override channel(s).
   - **interval_min**: minutes between posts per channel (3 or 5 — choose
     5 if you're being cautious).
   - **total_hours**: how long to run (6/12/18/24/48).
   - **attach_image**: `yes` or `no` (image is attached after warmup).
3. Click **Run workflow**.

The runner spins up in ~30s, WARP connects, the bot warms up for ~1-2
minutes, then starts the channel cycle. Each run is self-contained —
when `total_hours` is reached it posts a shutdown summary and exits
cleanly.

### First-run recommendation

Set `total_hours: 6`, `interval_min: 5`, `attach_image: no`. Watch the
logs (workflow logs + `LOG_WEBHOOK_URL` if you set it) to make sure the
IP is in your expected country, the channels were found, and a couple of
posts land. Then enable image and longer runs.

### 🎯 What to expect (normal behavior)

Logs (in the Actions tab or in your log webhook) will look like:

```
[14:02:11] 🔌 IP HEALTH: WARP connected (org: Cloudflare, country: ES) ✅
[14:02:18] ✅ Auth OK — logged in as yodonttryme46 (id 1004...)
[14:02:42] 🔎 Channel check: #trading (180s slowmode) — ALIVE ✅
[14:02:42] 🔎 Channel check: #💵・market (300s slowmode) — ALIVE ✅
[14:03:05] 🟢 STARTED v5.5.1
[14:03:05] 📋 Channels active: 2/2 (target: 5 min/ch ±jitter)
[14:08:14] 📝 [sell|#trading] variation #12 (len=142) — typing 5.3s → posted ✅
[14:08:20] ✏️  Typo edit: "quick" → "quik" → "quick"
```

Warnings (yellow) like rate limit 429 (handled automatically), caution
mode entry, or IP recheck are normal and recoverable. Red errors usually
mean the token is invalid, the channel is dead, or WARP failed.

---

## 7. What the bot does during a run

### Phase 1 — IP safety (before any Discord traffic)

- Connects WARP (if no `HTTPS_PROXY` was set).
- Queries `api.ipify.org` and verifies the provider/country through IP
  metadata services using the same configured session/proxy as the sender.
- Aborts if datacenter (Microsoft/Azure/Amazon/Google/OVH/Hetzner/etc.).
- Aborts if outside `ALLOWED_COUNTRIES` (if set).

### Phase 2 — Browser warmup (~1–2 min)

- Hits `https://discord.com/` and `https://discord.com/app` with
  Chrome-impersonated TLS to warm cookies/session, like a real user
  opening the app.

### Phase 3 — Auth + WebSocket gateway

- Verifies token via `GET /api/v9/users/@me`.
- Connects the v9 gateway (so account appears Online, receives DMs +
  message events in real time).
- Sends presence update (idle/online with status text).

### Phase 4 — Channel browsing (~1–2 min)

- Fetches each configured channel and reads its slowmode/read access. It
  does not post during startup probing.
- If the first real posting attempt for a channel returns 404, auto-discovery
  can run (v5.4): it browses the guild's channel list, fuzzy-matches by name,
  sends a confirmation message, and waits for a trusted ✅/❌ reaction. An
  empty `CONFIRM_USER_IDS` list disables recovery rather than authorizing
  everyone.

### Phase 5 — Main posting loop

Cycles through active channels forever (or until `total_hours`):

1. Pick next channel. Compute sleep interval = `interval_min × 60 ±
   jitter(25%)`. In caution mode, interval × `CAUTION_INTERVAL_MULT`
   (default 2×).
2. Sleep in 15s chunks so pause/stop/IP-bad events are picked up quickly.
3. Build a random variation of the ad text (over 100 sell / ~65 buy unique
   variants, auto-generated from your MESSAGE at startup).
4. If image is enabled and warmup posts are done, pick an image from the
   ad copy set, strip EXIF, attach as multipart.
5. Send typing indicator for `len(msg)/250` seconds.
6. POST to `/api/v9/channels/{cid}/messages`.
7. If success → ~18% chance of a typo-edit 5–22s later; sometimes react
   to other users at random during idle cooldown.
8. If 404 → trigger auto-discovery. If 403 → that channel is dead/muted
   for the rest of the run. If 429 → respect retry-after. Other errors are
   counted and backed off; silent post-deletion verification drives caution
   mode at its configured threshold.
9. Every 300s push a 💓 heartbeat to `DASHBOARD_WEBHOOK_URL`. Push a
   periodic dashboard summary about every 30 minutes.
10. Scan the last 20 messages in the channel for deals (if enabled).
11. Randomly take AFK breaks — by default 2–4 per 6-hour chunk, 10–30 min
   each.

### Phase 6 — Shutdown

When `total_hours` elapses, or on `/stop`/panic/DM-stop, sends a final
🏁 shutdown summary to the dashboard, closes the gateway cleanly, and
the workflow job exits.

---

## 8. Dashboard color code

| Color | Hex | Meaning |
|---|---|---|
| 🟢 Green | `0x57F287` | Active / running / good |
| 🟡 Yellow | `0xFEE75C` | Paused (controller, DM, caution) |
| 🔴 Red | `0xED4245` | Stopped / error / IP-pause |
| 🔵 Blurple | `0x5865F2` | AFK break / cycle summary |
| ⚫ Grey | `0x2F3136` | Offline / fallback |

---

## 9. Remote DM commands (v5.5)

If you set `CONTROLLER_USER_IDS` to include your main account's Discord
ID (or the control bot's application ID), you can DM the alt:

| DM command | Effect |
|---|---|
| `!setprice 2.7` | Update rate in-memory (regex auto-updates ad text). |
| `!setmode sell` or `!setmode buy` | Switch ad type on the fly. |
| `!setmessage <full text>` | Switch to entirely new ad copy. |
| `!pause` | Pause public posting (status goes 🟡). |
| `!resume` | Resume. |
| `!stop` | Trigger panic → shut down the run cleanly. |
| `!sync` | Force a gist reload now. |
| `!status` | Reply with a short status embed (rate, sent/err, uptime, active channels). |

Slash-command runtime overrides (price/mode/message/pause) are applied
**in-memory** to the current run — they don't persist across workflow runs (the
next `/run` starts with whatever you filled into the workflow form). That's
intentional: direct-DM tweaks don't overwrite your saved defaults.

If you configure `CONTROL_GIST_ID`, the sender also polls that private Gist for
an optional persistent or broadcast override object such as
`{"paused":true,"alt_id":2}`. The official control bot treats this Gist as a
read path; it does not write it. Edit the Gist only through GitHub or another
explicitly authorized integration, and use `/sync` to force all running alts to
reload it. If `CONTROL_GIST_ID` or `GIST_TOKEN` is absent, the sender simply
continues with direct-DM control and no Gist override path.

---

## 10. Updating the bot

When a new version of `send_ads.py` or `.github/workflows/send_ads.yml`
comes out:

**Single-alt**: commit the new files to your repo's `main` branch. The
next workflow run picks up the new code automatically.

**Multi-alt (with the central control bot)**: see
[`SETUP_CONTROL.md`](./SETUP_CONTROL.md) §13 — the `sync_to_alts.yml`
workflow pushes updates to every alt repo automatically with one commit.

---

## 10.5 Self-check workflow

Also included: `.github/workflows/self_check.yml`. Run it manually
(Actions → "🔍 Self-Check Secrets" → Run workflow) after you set up
secrets or after an update. It may take a few minutes while routing retries
and the sender self-test complete, and validates
end-to-end: `USER_TOKEN` authenticates, channels are reachable, webhook
URLs work, gist access works, IP is not a datacenter, all trusted IDs
look like snowflakes, and `send_ads.py`'s built-in self-test passes. It
fails the job with a red X if anything is wrong so you catch broken
tokens/webhooks immediately instead of wondering why a run is ⚫ offline.

---

## 11. Troubleshooting

**Fails on startup with "IP health: datacenter"?**
WARP failed to connect or the measured route was not verifiably safe. Cancel
the run, wait 1 min, run again. If it keeps happening, check the
`WARP-status` log lines — sometimes the WARP service needs a moment to
connect.

**"Channel not found" / 403 on send?**
The alt doesn't have permission to see/post in that channel. Confirm the
alt joined the server and has the right role. If the channel was deleted,
set `CHANNEL_NAMES` so v5.4 auto-discovery finds the new one.

**Posts aren't sending but no errors?**
You're probably in **caution mode** or **DM pause** — check the heartbeat
status. Caution means consecutive sends have been silently deleted; the
bot doubles the interval and waits for 3 successful sends to exit.

**"Message blocked" / instant delete?**
You're likely shadowbanned or the auto-mod is filtering your ad. Try a
different variation, drop any image, raise the interval, or stop for a
few hours. The auto-learn blocklist will automatically retire the
specific variation that got deleted.

**Token stops working mid-run?**
You logged into the alt on another device/browser and got a new token.
The old one invalidates. Update the `USER_TOKEN` secret and re-run.

**Deal alerts not firing?**
- `DEAL_SCAN_ENABLED` must be `true` (default is true).
- If `DEAL_MY_RATE` is unset (default), the bot extracts your rate from
  the ad text by regex — works for standard `$2.35/1k` / `2.35$`
  formats; if you write rates unusually, set `DEAL_MY_RATE` explicitly.
- The edge must be ≥ `DEAL_ALERT_DELTA` (default $0.05/k).
- Alerts go to `DASHBOARD_WEBHOOK_URL` (make sure that webhook is set).

**Workflows won't start / "Workflow not found"?**
Make sure the file is exactly at `.github/workflows/send_ads.yml`
(capitalization and path matter). It shows up in the Actions tab as
"Send Ads".

---

## 12. Operational notes (multi-alt)

If you later expand to multiple alts via the control bot (see
[`SETUP_CONTROL.md`](./SETUP_CONTROL.md)), keep these rules in mind:

- Give each alt a **different base MESSAGE** (different wording, emoji
  placement, formatting). Even small differences get multiplied by the
  variation generator into a hundred unique variants per alt.
- Give each alt a **different image** (re-saved with at least one pixel
  of difference or a tiny overlay). The bot already strips EXIF,
  randomizes filenames, jitters ~30 random pixels ±1 RGB, and randomizes
  JPEG quality on every send — but if the source file is byte-identical
  and the jitter RNG happens to overlap, the outputs can still look
  similar across alts.
- **Stagger `/run`** by 5–10 minutes between alts; don't blast all of
  them off at the exact same second.
- Use different supported `interval_min` values per alt (mix 3 and 5
  min), not all 3 min.
- Don't set all alts to the exact same rate. Offset buyers from sellers
  by at least a few cents.
- See `SETUP_CONTROL.md` §14 for the full multi-alt opsec checklist.

## 13. Environment variable quick reference

See [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) §12 for the full
multi-alt-oriented table. The short version for single-alt use:

- **Required:** `USER_TOKEN`, `CHANNEL_IDS`.
- **Webhooks (optional):** `LOG_WEBHOOK_URL`, `DASHBOARD_WEBHOOK_URL`,
  `DM_WEBHOOK_URL` (deal alerts use the dashboard webhook).
- **Timeouts (v5.5.1, optional):** `WEBHOOK_TIMEOUT` (default 20),
  `DM_WEBHOOK_TIMEOUT` (default 20).
- **DM pause:** `DM_PAUSE_MINUTES` (default 2), `FORWARD_OWN_DMS` (true).
- **Blocklist:** `GIST_TOKEN`, `GIST_ID`, `BLOCKED_STRIKES` (2),
  `BLOCKED_SAFETY_STOP` (5).
- **Geo:** `ALLOWED_COUNTRIES`.
- **Deals:** `DEAL_SCAN_ENABLED` (true), `DEAL_ALERT_DELTA` (0.05),
  `DEAL_MY_RATE` (0=auto).
- **Discovery (v5.4):** `CHANNEL_NAMES`, `CONFIRM_USER_IDS`,
  `CONFIRM_TIMEOUT` (60).
- **Control (v5.5):** `CONTROLLER_USER_IDS`, `CONTROL_GIST_ID`,
  `HEARTBEAT_INTERVAL_SEC` (300), `SYNC_GIST_INTERVAL_SEC` (45),
  `CONTROL_CMD_PREFIX` (!), `ALT_ID` (0), `ALT_NAME`.
- **Safety/steering:** `INTERVAL_MIN` (per-run input), `TOTAL_RUN_MIN`
  (per-run input), `WARMUP_POSTS` (3), `RANDOM_REACT` (true),
  `TYPO_EDIT_CHANCE` (0.18), `IMAGE_JITTER` (true), `IP_HEALTH_PAUSE_MIN`
  (10), `CAUTION_WINDOW` (3), `CAUTION_FAIL_THRESHOLD` (2),
  `CAUTION_EXIT_STREAK` (3), `CAUTION_INTERVAL_MULT` (2.0),
  `PANIC_TRUSTED_IDS`, `NEW_LOCATION_TIMEOUT_SEC` (30),
  `ENABLE_GATEWAY` (true).

You almost never need to change the safety/steering defaults from their
values above — they're tuned for conservative behavior.
