# 🚀 AdFarm V8 — Customer Setup Guide (A → Z)

> **Audience:** Customers who have purchased an AdFarm V8 subscription.
> **Last updated:** 2026-09-03 (V8 final).
> **Companion:** [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) — the operator/admin guide.

---

## Table of Contents

1. [Welcome to AdFarm V8](#1-welcome-to-adfarm-v8)
2. [What You Get](#2-what-you-get)
3. [Before You Start](#3-before-you-start)
4. [Step 1 — Accept the Policy](#4-step-1--accept-the-policy)
5. [Step 2 — Pay (BEP-20 Crypto)](#5-step-2--pay-bep-20-crypto)
6. [Step 3 — Wait for Activation](#6-step-3--wait-for-activation)
7. [Step 4 — Run `/setup`](#7-step-4--run-setup)
8. [Step 5 — Run `/run` (Start Your Farm)](#8-step-5--run-run-start-your-farm)
9. [How to Get Your Alt Token](#9-how-to-get-your-alt-token)
10. [How to Find Channel IDs](#10-how-to-find-channel-ids)
11. [Daily Use — What Happens Automatically](#11-daily-use--what-happens-automatically)
12. [Your Commands](#12-your-commands)
13. [Understanding Your Dashboard](#13-understanding-your-dashboard)
14. [Deal Scanner (Arbitrage Alerts)](#14-deal-scanner-arbitrage-alerts)
15. [What Happens If Your Alt Gets Banned](#15-what-happens-if-your-alt-gets-banned)
16. [Renewing Your Subscription](#16-renewing-your-subscription)
17. [VIP Features](#17-vip-features)
18. [Rules & Policy](#18-rules--policy)
19. [FAQ & Troubleshooting](#19-faq--troubleshooting)
20. [Getting Help](#20-getting-help)

---

## 1. Welcome to AdFarm V8

AdFarm V8 is a **fully managed 24/7 advertising farm** for Roblox trading-game Discord servers. You bring the alt accounts and channel access; we handle everything else — the runners, the anti-detection, the deal scanning, the DMs, and the safety.

**You never need to:**
- ❌ Run anything on your PC or phone 24/7.
- ❌ Manage workflows or servers.
- ❌ Worry about slowmodes, shadowbans, or rate limits.
- ❌ Access GitHub or any technical tools.

**We handle:**
- ✅ 24/7 ad posting through GitHub Actions (cloud-hosted).
- ✅ Real browser fingerprinting (curl_cffi Chrome impersonation).
- ✅ Cloudflare WARP routing (no datacenter IPs).
- ✅ Anti-detection (typo edits, natural typing, AFK breaks, reactions).
- ✅ Deal scanning for arbitrage opportunities.
- ✅ Buyer DM forwarding.
- ✅ Automatic ban detection and replacement.

---

## 2. What You Get

| Feature | Included |
|---------|----------|
| 24/7 ad posting (sell or buy mode) | ✅ |
| Up to 4 alt accounts per subscription | ✅ |
| Natural anti-detection stack | ✅ |
| Deal scanner (passive arbitrage) | ✅ |
| Dashboard with live status | ✅ |
| Action logs in your private forum | ✅ |
| 48h auto-renew (∞ Limitless) | ✅ |
| Ban time-credit (48h full, then pro-rated) | ✅ |
| One-click re-setup on bans | ✅ |
| DM inbox forwarding (VIP) | ⭐ VIP |
| Squad batch operations (VIP) | ⭐ VIP |

---

## 3. Before You Start

### What you need:

1. **One or more Discord alt accounts** (NOT your main account).
   - Each alt must already be in the server(s) where you want to post.
   - Each alt must have permission to send messages in the trading channels.
   - ⚠️ Alts should be "aged" — at least 3-5 days of normal activity before using the bot.

2. **A way to copy your alt's Discord token** (see Step 4 / Section 9).

3. **The channel IDs** of the trading channels (see Section 10).

4. **Your payment ready** (BEP-20 USDT or BUSD via Trust Wallet).

### ⚠️ Important Warnings

- **Never use your main account.** We only support alt accounts. If you send us a main account token, it will be rejected.
- **Alt survival is not guaranteed.** We use every known anti-detection measure, but platform bans are outside our control.
- **No refunds** once the farm is provisioned and a run starts.
- **Crypto payments are final.**

---

## 4. Step 1 — Accept the Policy

Before any payment address is shared, you must read and accept our **Pre-Payment Policy Card**.

1. Go to the `#open-ticket` channel in the server.
2. Read the pinned policy card carefully.
3. Click **✅ I Agree — I've read the policy**.

The policy covers:
- No refunds after provisioning
- Time credit on bans (48h full, then pro-rated)
- Alt survival not guaranteed
- Main accounts never supported
- Crypto payments final
- Data stored (discord ID, username, repos, dates)
- No SLA (best-effort support)

> 📌 Your acknowledgement is recorded. An admin can only share the wallet address after you've accepted.

---

## 5. Step 2 — Pay (BEP-20 Crypto)

1. After accepting the policy, an admin will share the payment wallet address.
2. Open **Trust Wallet** (or any BEP-20 compatible wallet).
3. Send the agreed amount in **USDT or BUSD** on the **BSC (BNB Smart Chain)** network.
4. Copy the **transaction hash (TX hash)** — it looks like `0x1234abcd...`
5. Post the TX hash in your ticket.

### Verification

The admin will verify your payment on [BSCScan](https://bscscan.com) by checking:
- The correct amount was received.
- The TX is confirmed (not pending).
- The sender and recipient match.

Once verified, they'll mark your ticket as paid and proceed to activation.

---

## 6. Step 3 — Wait for Activation

After payment verification:

1. An admin runs `/admin activate @YourName days:30 alts:2` (or however many alts you purchased).
2. You'll receive a **welcome DM** from the bot with:
   - Instructions to run `/setup` in your new `#control` thread.
   - A pointer to the step-by-step token guide (section 9 of this document).
3. A private forum is created for you in the 🏢 **Customer Hub** category with:
   - **#control** — your main interaction point (run commands here)
   - **#dashboard** — live status cards updated every 5 minutes
   - **#farm-logs** — detailed action logs (every send, error, event)
   - **#deals** — arbitrage scanner alerts

> ⏱️ Activation usually completes within a few hours. If it takes longer, check #open-ticket or DM an admin.

---

## 7. Step 4 — Run `/setup`

This is the one-time setup wizard where you enter your alt tokens and channels.

### How it works:

1. **Go to your `#control` thread** (in the 🏢 Customer Hub).
2. **Type `/setup`** and press Enter.
3. **Step 1:** A modal asks *"How many alts do you want to set up? (1-4)"*
   - Enter the number of alts you purchased.
4. **Step 2:** For each alt, a modal appears asking for:
   - **Alt [n] Token:** Your Discord alt account token (see Section 9 for how to get this).
   - **Alt [n] Channel IDs:** Comma-separated channel IDs (see Section 10).
5. **Validation:** The bot validates each token (`GET /users/@me`) and channels before moving to the next.
6. **Completion:** The bot creates your alt repositories, uploads the sender code, and runs a self-check.

### What happens behind the scenes:

- ✅ Public alt repo created on GitHub (automated by the operator).
- ✅ `send_ads.py` + workflow files uploaded.
- ✅ Secrets set (USER_TOKEN, CHANNEL_IDS, webhooks).
- ✅ Push protection and secret scanning enabled.
- ✅ Self-check workflow runs to verify everything works.

> 📖 **Stuck on tokens?** Follow the step-by-step text guide in section 9 below, or open a ticket with the 🎫 button in `#open-ticket`.

---

## 8. Step 5 — Run `/run` (Start Your Farm)

Once setup is complete, start your ad farm:

1. **Type `/run`** in your `#control` thread.
2. The **guided launcher** appears with:
   - **Alt selector** — choose which alt to run.
   - **Mode** — Sell (💰) or Buy (🛒).
   - **Interval** — 3 or 5 minutes between posts.
   - **Runtime** — 6/12/18/24/48h or ∞ Limitless.
3. **Paste your raw ad message** (or question) — the bot automatically applies emojis and modifiers.
4. **Confirm** — review the preview and click Launch.

### ∞ Limitless Mode

- Runs for a **maximum of 48 hours per dispatch**.
- **Automatically re-dispatches** while your subscription is active.
- Each renewal is posted to your `#control` thread.
- If your subscription expires, no re-dispatch happens (stop-reason is posted).

> 🟡 **Warning shown at launch:** *"∞ Limitless runs for a maximum of 48 hours per dispatch. A new run will be required to continue."*

### What happens next:

- Your farm starts posting within ~60 seconds.
- Dashboard updates every 5 minutes with live status.
- Farm logs stream every action to your `#farm-logs` thread.
- Deal scanner runs passively in the background.

---

## 9. How to Get Your Alt Token

### Quick Steps (Desktop Browser):

1. Open **Chrome** or **Edge** and log into Discord as your **alt account**.
2. Press **F12** to open Developer Tools.
3. Click the **Application** tab (or **Storage** in Firefox).
4. In the left sidebar, expand **Local Storage** → click `https://discord.com`.
5. Find the entry with key `token`.
6. Copy the **value** (it looks like `MTIzNDU2Nzg5...`).

### Alternative (Network tab):

1. Press **F12** → **Network** tab → filter by `api`.
2. Refresh Discord (F5).
3. Click any request to `discord.com/api/...`
4. In **Request Headers**, find `authorization`.
5. Copy that value.

### ⚠️ Important:

- **Don't log out** of the alt after copying — logging out invalidates the token.
- **Keep it secret** — anyone with the token can fully control that alt.
- **If the token stops working:** You may have logged in on another device (new token issued). Re-extract and re-setup.


---

## 10. How to Find Channel IDs

### Enable Developer Mode:

1. Discord → **User Settings** (gear icon).
2. **Advanced** → turn ON **Developer Mode**.

### Copy Channel IDs:

1. Go to the server where you want to post ads.
2. **Right-click** on each target trading channel.
3. Click **Copy Channel ID** at the bottom of the menu.
4. Paste all IDs separated by commas: `1234567890,9876543210`

### Example:

```
#trading → 1541658382015135817
#💵・market → 1103759996468080752

Paste as: 1541658382015135817,1103759996468080752
```

---

## 11. Daily Use — What Happens Automatically

Once your farm is running, everything is automatic:

| What | How Often | Where You See It |
|------|-----------|------------------|
| Ad posting | Every 3-5 min | `#farm-logs` |
| Status update | Every 5 min | `#dashboard` |
| Deal scanning | Continuous | `#deals` |
| AFK breaks | 2-4 per 6h chunk | `#farm-logs` |
| Typo edits | ~18% of posts | `#farm-logs` |
| Auto-renew (∞ mode) | Every 48h | `#control` |
| Expiry reminders | 7d, 3d, 1d before | DM from bot |
| Ban detection | Instant | `#control` |

### You don't need to do anything unless:

- Your alt gets banned → see Section 15.
- You want to change settings → use `/tune`.
- Your subscription is expiring → use `/renew`.
- You want to stop → use `/stop`.

---

## 12. Your Commands

All commands are used in your private `#control` thread:

| Command | What it does |
|---------|-------------|
| `/run` | Start an ad run (guided launcher) |
| `/stop` | Stop the current run (takes ~30-45s) |
| `/status` | Show current alt status card |
| `/setup` | Set up (or re-setup) your alts |
| `/tune` | Open the interactive tuning panel |
| `/tune alt:1 price:2.50` | Quick price change |
| `/tune alt:1 message:New text` | Change your ad message |
| `/channels` | Manage your trading channels |
| `/channels action:add channel_id:123` | Add a new channel |
| `/deals` | Configure the deal scanner |
| `/deals keywords:Blade Ball, BB` | Set deal scanner keywords |
| `/help` | Full command reference |
| `/renew` | Open a renewal ticket |
| `/getstarted` | Quick-start guide embed |

### VIP-only commands:

| Command | What it does |
|---------|-------------|
| `/squad` | Batch operations across multiple alts |
| `/script simulate` | Dry-run a message before posting |
| `/script run` | Execute a scripted message |
| `#dm-inbox` | Forwarded buyer DMs |

---

## 13. Understanding Your Dashboard

Your `#dashboard` thread shows live status cards updated every 5 minutes:

### Status Colors

| Color | Meaning |
|-------|---------|
| 🟢 Green | Active / running / healthy |
| 🟡 Yellow | Paused (DM pause, caution mode, or controller pause) |
| 🔴 Red | Stopped / error / IP issue |
| 🔵 Blue | AFK break in progress |
| ⚫ Grey | Offline / waiting for first heartbeat |

### What the dashboard shows:

- **Alt name** and **mode** (💰 SELL or 🛒 BUY)
- **Sent count** and **error count**
- **Per-channel status** (alive/dead/slowmode)
- **Current rate** ($X.XX/1k)
- **Uptime** (how long the current run has been active)
- **Deal scanner** status (ON/OFF + edge threshold)

---

## 14. Deal Scanner (Arbitrage Alerts)

The deal scanner runs **passively** — it reads messages the bot already fetches (no extra API calls) and alerts you when someone is selling below your price or buying above it.

### Alert types:

- **🟢 SUPPLIER ALERT (Buy Low):** Someone is selling below your buy benchmark.
  - Example: *"SELLER DETECTED — Blade Ball 500 @ $1.80/1k — your rate $2.50/1k → +$0.70/1k margin (40% discount)"*

- **🔵 ARBITRAGE SALE (Sell High):** Someone is buying at high bids above your cost.
  - Example: *"BUYER DETECTED — buying Blade Ball @ $3.00/1k — your sell rate $2.50/1k → +$0.50/1k profit"*

### Configure it:

```
/deals keywords:Blade Ball, BB token, BB
/deals min_delta:0.10
/deals enabled:on
```

> 💡 The deal scanner is the **#1 retention engine** — customers who see supplier alerts within their first 24h stay significantly longer.

---

## 15. What Happens If Your Alt Gets Banned

Don't panic — this is handled automatically.

### Detection

When the bot detects a ban (HTTP 403, token invalidated, account deleted), it:

1. **Stops the run** for that alt immediately.
2. **Posts to your #control thread:**
   > ⚠️ **Your alt was banned.**
   > Don't panic — here's what happens next:
   > 1. You get **time credit** (full credit if banned within 48h of first run, otherwise pro-rated).
   > 2. We've renamed the old repo and prepared a fresh one.
   > 3. Click the button below to set up the replacement alt.
3. **Renames the old repo** to `<name>_BANNED_<timestamp>`.
4. **Creates a fresh repo** for the replacement alt.

### Re-setup

1. Click the **🔄 Re-setup Replacement Alt** button.
2. Enter the new alt's token (same channels are reused automatically).
3. Run `/run` again to start the replacement.

### Time Credit Policy

| When banned | Credit |
|-------------|--------|
| Within 48h of first run | **Full time credit** (added to your subscription) |
| After 48h | **Pro-rated** for unused time |

---

## 16. Renewing Your Subscription

### Before expiry (7d, 3d, 1d reminders)

The bot sends you a **DM reminder** at each threshold. To renew:

1. Type `/renew` in your `#control` thread.
2. A ticket is opened pre-filled with your customer ID.
3. Pay the renewal amount (same process as initial payment).
4. Admin verifies and runs `/admin extend @You days:30`.

### After expiry

- Your farm stops automatically.
- Your data is preserved in `customers.db`.
- Run `/renew` or DM an admin to reactivate.

### Pausing

Need a break? Use:
```
/pause-billing
```
This opens a ticket requesting a subscription pause. An admin reviews and approves extensions.

---

## 17. VIP Features

VIP customers get access to:

### DM Inbox (#dm-inbox thread)

All DMs your alt receives from buyers are forwarded to a private thread. You can:
- Read buyer messages in real-time.
- Use `/reply` to respond through the alt.
- The bot auto-pauses public posting for 2 minutes after each inbound DM.

### Squad Operations (`/squad`)

Group multiple alts into named squads and batch-control them:
- `/squad action:assign alt:1 squad_name:alpha`
- `/squad action:pause squad_name:alpha`
- `/squad action:price squad_name:alpha value:2.75`

### Script Commands

- `/script simulate` — Dry-run a message before posting.
- `/script run` — Execute a scripted message immediately.

---

## 18. Rules & Policy

### What we support:
- ✅ Fresh alt accounts only.
- ✅ Sell and buy ad modes.
- ✅ Roblox trading game servers.
- ✅ Up to 4 alts per customer.

### What we DON'T support:
- ❌ **Main accounts** — never. Don't ask.
- ❌ Harassment, spam, fraud, or unsolicited bulk messages.
- ❌ Non-trading channels or non-Roblox servers (case-by-case).
- ❌ Custom code modifications or API access.

### Your data:
- **Stored:** Discord ID, username, alt repo names, setup dates, subscription status, alt tokens (obfuscated, never logged).
- **Not stored:** Anything else. We never sell data.
- **Deletion:** Contact an admin at any time — every record tied to your Discord ID is deleted on request (usually within 24h).

---

## 19. FAQ & Troubleshooting

### "My farm stopped — what happened?"

Check your `#control` thread for the stop reason:
- Subscription expired → `/renew`.
- Alt banned → click re-setup button.
- Channel deleted → `/channels action:replace` to add a new one.
- IP health issue → wait for auto-recovery or `/run` again.

### "How do I change my price?"

```
/tune alt:1 price:2.75
```

### "How do I change my message?"

```
/tune alt:1 message:Selling Blade Ball cheap DM me fast
```

### "A channel got deleted — what do I do?"

```
/channels action:replace channel_id:OLD_ID new_channel_id:NEW_ID
```

### "How do I add a new channel?"

```
/channels action:add channel_id:1234567890
```

### "My token stopped working"

You probably logged in on another device (new token issued). Re-extract the token (Section 9) and run `/setup` again for that alt.

### "Posts aren't sending but no errors?"

You might be in **caution mode** (the bot detected deletions and backed off). Check `#dashboard` for the 🟡 status. Caution mode exits automatically after 3 successful sends.

### "Deal alerts aren't firing?"

- Make sure the deal scanner is enabled: `/deals enabled:on`.
- Set relevant keywords: `/deals keywords:Blade Ball, BB`.
- Lower the delta threshold: `/deals min_delta:0.03`.

### "How long does `/stop` take?"

~30-45 seconds. The stop command polls through Gist every 15 seconds. This is a documented SLA, not a bug. For emergency stops, use `/shutdown` (cancels workflows server-side).

---

## 20. Getting Help

### Self-service:
1. Check your `#control` thread for bot messages.
2. Check `#dashboard` for status.
3. Check `#farm-logs` for detailed action logs.
4. Run `/help` for the full command reference.
5. Run `/diagnose` for a deep root-cause diagnostic.

### Contact support:
- Open a ticket in `#open-ticket`.
- DM an admin directly.
- **No SLA** — support is best-effort, but we aim to respond within a few hours.

### Canned answers (for admins):
Common ticket topics have pre-written responses (see [`V8_RUNBOOKS.md`](./V8_RUNBOOKS.md) §7):
1. "Where do I find my token?" → Follow section 9 of this guide (text walkthrough).
2. "It says invalid token" → Re-extract, check for whitespace.
3. "My alt got banned" → Time credit + re-setup flow.
4. "How do I change my price?" → `/tune alt:1 price:X`.
5. "How do I renew?" → `/renew` command.

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────────┐
│                    YOUR DAILY CHECKLIST                       │
│                                                               │
│  Morning:   Check #dashboard (all green?)                     │
│  Midday:    Check #deals (any arbitrage wins?)                │
│  Evening:   Check #farm-logs (any errors or bans?)            │
│                                                               │
│  Weekly:    Check subscription days remaining                 │
│             Renew before the 7-day reminder if needed         │
│                                                               │
│  On Ban:    Don't panic → click re-setup → run /run again    │
│                                                               │
│  Commands:  /run  /stop  /status  /tune  /channels  /deals   │
│             /setup  /help  /renew  /diagnose                  │
└─────────────────────────────────────────────────────────────┘
```

---

*End of SETUP_GUIDE.md — see [`SETUP_CONTROL.md`](./SETUP_CONTROL.md) for the operator/admin guide.*
