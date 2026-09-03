# 🔧 AdFarm V8 — One-Command Setup (Main + 3 Workers)

> **One command. 7 inputs. Everything automated.**
> You run `python3 setup.py` and the script handles the rest.
>
> **Companion:** [`SETUP_GUIDE.md`](./SETUP_GUIDE.md) — the customer-facing guide.

---

## 🏗️ Architecture (Why 4 Accounts?)

```
┌──────────────────────────────────────────────┐
│  MAIN ACCOUNT (your personal GitHub)         │
│  • Core repo (adfarm-core-AI)                │
│  • Control bot (runs 24/7 on Actions)        │
│  • Gist backup (customers.db persistence)    │
│  • NO customer alt repos live here           │
└────────────────────┬─────────────────────────┘
                     │ creates repos under workers
     ┌───────────────┼───────────────┐
     ▼               ▼               ▼
┌─────────┐   ┌─────────┐   ┌─────────┐
│ Worker 1│   │ Worker 2│   │ Worker 3│
│ alt repos│   │ alt repos│   │ alt repos│
│ (public) │   │ (public) │   │ (public) │
└─────────┘   └─────────┘   └─────────┘
```

**Why separate workers?**
- **Isolation:** If one worker gets flagged, the other 2 keep running.
- **Free minutes:** Public repos on worker accounts = unlimited GitHub Actions minutes.
- **Security:** Customer tokens never touch your main account.
- **Scale:** Round-robin across 3 accounts distributes load.

---

## 🧂 What You Need

### Prerequisites (one-time, ~5 min):

| # | What | How |
|---|------|-----|
| 1 | **GitHub CLI** installed | `brew install gh` (macOS) / `sudo apt install gh` (Ubuntu) / `winget install GitHub.cli` (Windows) |
| 2 | **Main account** authenticated | `gh auth login && gh auth refresh -s repo,workflow,gist` |
| 3 | **3 fresh GitHub accounts** created | [github.com/signup](https://github.com/signup) — use different emails (e.g. `you+worker1@gmail.com`) |
| 4 | **Discord bot** created | [discord.com/developers](https://discord.com/developers/applications) → New App → Bot → Reset Token → Enable Message Content Intent → Invite to server |

### What the script asks you for (7 things):

| # | Input | How to get it |
|---|-------|---------------|
| 1 | Discord Bot Token | Discord Developer Portal → Bot → Reset Token |
| 2 | Your Discord User ID | Developer Mode ON → right-click yourself → Copy User ID |
| 3 | Discord Server ID | Developer Mode ON → right-click server → Copy Server ID |
| 4 | Crypto wallet address | Your BEP-20 USDT/BUSD address from Trust Wallet |
| 5 | Worker 1 username + token | Username you chose + PAT (script opens the page for you) |
| 6 | Worker 2 username + token | Same |
| 7 | Worker 3 username + token | Same |

> 💡 **For workers 5-7:** The script opens `github.com/settings/tokens/new?scopes=repo,workflow` in your browser. You just need to: log in as that worker → click "Generate token" → copy → paste back. Takes ~30 seconds per worker.

---

## 🚀 The One Command

```bash
git clone https://github.com/YOUR-USERNAME/adfarm-core-AI.git
cd adfarm-core-AI
python3 setup.py
```

---

## 📋 What Happens (Step by Step)

```
╔══════════════════════════════════════════════════════════════╗
║           🚀  AdFarm V8 — Zero-Friction Setup  🚀           ║
║   Main account:  core repo + control bot                     ║
║   3 Workers:     customer alt repos (round-robin)            ║
╚══════════════════════════════════════════════════════════════╝

Step 1/9: Checking GitHub CLI (main account)
  ✅ GitHub CLI authenticated
  ✅ Main account: @YourUsername
  ✅ Repository: YourUsername/adfarm-core-AI

Step 2/9: Discord + billing inputs
  🔑 Discord Bot Token: ****
  📝 Your Discord User ID(s): 123456789
  📝 Your Discord Server ID: 987654321
  📝 Crypto wallet address: 0x1234...
  ✅ Inputs collected

Step 3/9: Worker accounts (host customer alt repos)
  Your system uses 3 worker GitHub accounts to host customer alt repos.
  This gives you isolation — if one worker gets flagged, the others keep running.

  Worker 1 of 3:
  📝 Username: adfarm-worker1
  🌐 Opening token creation page...
  🔑 Paste the token: ****
  ✅ Worker 1: @adfarm-worker1 — token valid

  Worker 2 of 3:
  📝 Username: adfarm-worker2
  🌐 Opening token creation page...
  🔑 Paste the token: ****
  ✅ Worker 2: @adfarm-worker2 — token valid

  Worker 3 of 3:
  📝 Username: adfarm-worker3
  🌐 Opening token creation page...
  🔑 Paste the token: ****
  ✅ Worker 3: @adfarm-worker3 — token valid
  ✅ 3 worker(s) configured

Step 4/9: Verifying Discord bot
  ✅ Bot: AdFarm V8 Control#1234
  ✅ Bot is in: My Control Server

Step 5/9: Creating Discord server structure
  ✅ Created #admin-alerts
  ✅ Created #admin-chat
  ✅ Created #audit-logs
  ✅ Created #open-ticket
  ✅ Created #announcements
  ✅ 🏢 Customer Hub category ready

Step 6/9: Creating backup Gist
  ✅ Backup Gist: abc123def456

Step 7/9: Setting GitHub secrets (main repo)
  ✅ Secret: BOT_TOKEN
  ✅ Secret: GH_TOKEN
  ✅ Secret: WORKER_TOKENS
  ✅ Secret: WORKER_GITHUB_OWNERS
  ✅ Secret: WORKER_1_TOKEN
  ✅ Secret: WORKER_2_TOKEN
  ✅ Secret: WORKER_3_TOKEN
  ✅ Secret: GIST_ID
  ✅ Secret: PAYMENT_ADDRESS
  [... all 20+ secrets set automatically ...]

Step 8/9: Database + workflows
  ✅ Database initialised (schema v2)
  ✅ Workflow: control_bot.yml
  ✅ Workflow: send_ads.yml

Step 9/9: Final touches
  ✅ Policy card pinned

══════════════════════════════════════════════════════════════
  🎉  AdFarm V8 Setup Complete!

  🤖 Bot:       AdFarm V8 Control#1234
  👤 Main GH:    YourUsername (control bot + Gist)
  🏭 Workers (3):
     Worker 1: @adfarm-worker1 (customer alt repos)
     Worker 2: @adfarm-worker2 (customer alt repos)
     Worker 3: @adfarm-worker3 (customer alt repos)

  🚀 NEXT: Push to start the bot:
     git add . && git commit -m '🚀 V8 setup' && git push origin main

  Then onboard a customer:
     /admin activate @CustomerName days:30 alts:2
══════════════════════════════════════════════════════════════
```

---

## 🏁 After Setup: Start the Bot

```bash
git add .
git commit -m "🚀 V8 setup complete"
git push origin main
```

The `control_bot.yml` workflow triggers automatically. Bot starts within 30 seconds.

### Verify:
1. **GitHub Actions** → "🤖 V8 Control Bot" running
2. **Discord** → Bot online (green dot)
3. **Discord** → `/help` shows V8 command reference
4. **Discord** → `/admin list` shows "No customers"

---

## 👤 Onboard Your First Customer

```
1. Customer accepts policy → clicks ✅ in #open-ticket
2. You share wallet:        /admin payment-address @Customer
3. Customer pays (BEP-20) → posts TX hash
4. You verify on BSCScan
5. You activate:            /admin activate @Customer days:30 alts:2
6. Customer runs /setup → enters alt tokens + channels
7. Customer runs /run → farm starts posting
```

**What happens behind the scenes:**
- `/admin activate` creates customer repos on a **worker account** (round-robin)
- The worker's token is used to create the repo, set secrets, upload workflows
- Customer never touches GitHub — everything is automated

---

## 🔧 Admin Commands

| Command | What It Does |
|---------|-------------|
| `/admin list` | Show all customers + status |
| `/admin activate @User days:30 alts:2` | Onboard a new customer |
| `/admin extend @User days:30` | Extend subscription |
| `/admin deactivate @User` | Shut down a customer |
| `/admin shutdown confirm:ALL` | Emergency kill all (2-admin confirm) |
| `/admin repo-sync` | Push code to all customer repos |
| `/admin payment-address @User` | Share wallet (policy-gated) |
| `/admin verify-tokens` | Audit all worker tokens |
| `/admin expiry-alerts` | Dry-run reminder path |
| `/admin pin-policy` | Pin ToS in #open-ticket |

---

## 🔄 Worker Token Rotation (Every 90 Days)

When a worker token expires:
1. Log into that worker account on GitHub
2. Go to Settings → Developer settings → Tokens → Regenerate
3. Update the secret:
   ```bash
   gh secret set WORKER_1_TOKEN --body "ghp_new_token_here"
   ```
4. Done — the bot picks up the new token on next operation.

---

## 🆘 Troubleshooting

| Problem | Fix |
|---------|-----|
| `gh` not found | Install from [cli.github.com](https://cli.github.com) |
| Worker token invalid | Regenerate: log into worker account → Settings → Tokens → Regenerate |
| Bot not in server | Re-invite via OAuth2 URL from Developer Portal |
| `/admin` not visible | Only OWNER_IDS users can see admin commands |
| Customer repo creation fails | Check worker token has `repo` scope |
| Gist backup failing | Check main account has `gist` scope (`gh auth refresh -s gist`) |

---

## ⚙️ Advanced Options

```bash
# Non-interactive (env vars):
BOT_TOKEN=xxx OWNER_IDS=123 GUILD_ID=456 \
WORKER_1_USER=w1 WORKER_1_TOKEN=t1 \
WORKER_2_USER=w2 WORKER_2_TOKEN=t2 \
WORKER_3_USER=w3 WORKER_3_TOKEN=t3 \
python3 setup.py --quick

# Overwrite existing:
python3 setup.py --force
```

---

*One command. 3 workers. You're live. Go get customers.* 🚀
