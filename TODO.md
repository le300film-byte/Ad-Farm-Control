## 📋 `TODO.md` — Final Tweaks (Minimal)

### 🎯 EXECUTION INSTRUCTION FOR THE AI

```
Read this TODO.md. Apply each fix. No new features. Just polish the final 5 items.
```

---

### 1. Friend Can't See `/admin` Commands
**Problem:** The commands are hidden because `default_permissions=administrator` is set, but Discord requires *Server Administrator* permission (not `OWNER_IDS`).

**Fix:** In `adfarm/commands/registry.py`, remove the `default_permissions=discord.Permissions(administrator=True)` line from the `/admin` and `/help-admin` command definitions.

**File:** `adfarm/commands/registry.py`

**Quick Change:**
```python
# Change this:
@tree.command(name="admin", description="Operator tools (admin rooms only)", default_permissions=discord.Permissions(administrator=True))

# To this:
@tree.command(name="admin", description="Operator tools (admin rooms only)")
```

---

### 2. `/admin reset` Should Only Require One Admin
**Problem:** The multi-sig requires a second admin confirmation.

**Fix:** Remove the multi-sig check from `_reset` and `_shutdown_bot` in `adfarm/commands/admin.py`. Keep only the typed confirmation (`confirm:RESET`).

**File:** `adfarm/commands/admin.py`

**Quick Change:** Remove the `multisig.confirm` block; call the reset logic directly after the confirmation check.

---

### 3. Richer `/help-admin` and `SKILL.md` for Admins
**Problem:** The current `/help-admin` is flat; `SKILL.md` is generic.

**Fix:**
- **`/help-admin`:** Group commands by category (Customer Management, Alts/Repos, Ops, Destructive). Use bullet points instead of plain text.
- **`SKILL.md`:** Add a dedicated "Admin Operator Guide" section at the top with:
  - 10 most common admin commands (activate, extend, deactivate, health, etc.).
  - Quick troubleshooting: alt offline, ticket not working, heartbeat stale.
  - Admin-only commands reference.

**Files:** `adfarm/commands/admin.py`, `SKILL.md`

---

### 4. Add Fallback IP Provider (Fix `ipwho.is` Rate Limits)
**Problem:** The sender only uses `ipwho.is`. When rate-limited, all alts fail.

**Fix:** In `sender/send_ads.py`, modify `_lookup_egress()` to use a fallback chain: `ipwho.is` → `ipapi.co` → `ipinfo.io`.

**File:** `sender/send_ads.py`

**Quick Change:**
```python
payload = _fetch_json(f"https://ipwho.is/{ip}")
if not payload or payload.get("success") is False:
    payload = _fetch_json(f"https://ipapi.co/{ip}/json/")
```

---

### 5. Clear Error Message on Action Failure
**Problem:** When a dispatch or command fails, the logs show raw errors, and customers get a generic "⚠️ An external service failed."

**Fix:** In `adfarm/commands/registry.py`, catch `AdFarmError` and map it to a user-friendly message. Add a suggestion to wait or open a ticket.

**File:** `adfarm/commands/registry.py`

**Example:**
```python
except AdFarmError as exc:
    await inter.followup.send(
        f"❌ {exc.user_message}\n\n"
        "💡 If this keeps happening, wait 2-3 minutes and try again, or open a ticket.",
        ephemeral=True
    )
```

---

## ✅ Verification Checklist (After Fixes)

- [ ] Friend sees `/admin` commands in Discord.
- [ ] `/admin reset confirmation:RESET` works with one admin.
- [ ] `/help-admin` is grouped and readable.
- [ ] `SKILL.md` has an "Admin Operator Guide".
- [ ] IP check falls back to ipapi.co if ipwho.is rate-limits.
- [ ] Customers see helpful error messages on failures.

---

**🚀 Apply these 5 fixes, and you are fully done.**