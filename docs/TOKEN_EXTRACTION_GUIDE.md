# 🎥 Token Extraction Guide — 3-Minute Walkthrough Script

> **TODO 0.5.** This is the *text version* of the phone-recorded video. The
> video itself must be recorded by a founder with a phone (`SETUP_VIDEO_URL`
> points at this file until the MP4 exists). The script below is designed to be
> read directly into the camera — each section is one shot.

---

## What you need

- The **browser** (Chrome/Edge/Firefox) where you are logged into the alt
  account on **roblox.com**.
- **Discord in Developer Mode** on the same device or a second device.

---

## Shot 1 — Open DevTools (15s)

> "Open the alt account in your browser. Press **F12** or right-click → **Inspect**."

- Show the page with the alt logged in.

---

## Shot 2 — Application → Local Storage (30s)

> "At the top of DevTools click the **Application** tab. On the left, open
> **Local Storage** and click the roblox.com entry."

- Show the tree: `Application → Storage → Local Storage → https://www.roblox.com`.

---

## Shot 3 — Find and copy the token (45s)

> "Scroll through the key-value list until you find the entry called **_.ROBLOSECURITY_**
> (or **RobloxSecurityToken**). The value on the right is your token — it starts
> with `_|W...` . Double-click it, select the **whole value**, and copy it.
> **Do not share this with anyone but us.**"

- Show the row highlighted, the value partially visible (blur the middle).

---

## Shot 4 — Developer Mode + channel ID (45s)

> "Now open Discord and go to **User Settings → Advanced**, and turn on
> **Developer Mode**. Right-click the trading channel you want the bot to post
> in, and choose **Copy Channel ID**. Paste it into the setup form. If you want
> several channels, copy each ID and separate them with commas."

- Show Settings → Advanced toggling Developer Mode on.
- Show right-click → Copy Channel ID.

---

## Shot 5 — Where to paste (30s)

> "Back in our server, run **/setup**, tell us how many alts, then paste the
> token and the channel ID for each alt. That's it — we take it from there."

---

## ⚠️ Safety warnings (displayed as a card at start/end of video)

- Your token is **account access**. Only share it inside our `/setup` modal.
- Never post a token in a public channel, a DM, or a support ticket.
- If your token leaks, rotate it immediately: log out of the account and log
  back in — the old `.ROBLOSECURITY` value dies with the session.

---

## Companion quick-reference

| Step | Action |
|------|--------|
| 1 | F12 → **Application** tab |
| 2 | **Local Storage → roblox.com** |
| 3 | Find `_.ROBLOSECURITY` → copy the **full value** |
| 4 | Discord: enable **Developer Mode**, right-click channel → **Copy Channel ID** |
| 5 | Run `/setup` and paste (one modal per alt; 1–4 alts) |
