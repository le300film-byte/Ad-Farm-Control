# 03_BUILD_NEW_SYSTEM.md — Build the New System from Scratch

## 🎯 YOUR TASK

You have read:
1. `01_DESCRIBE_THE_PROJECT.md` — The full system description.
2. `02_ANALYSE_AND_REDESIGN.md` — The industrial-grade architecture design.

Now you will **build the new system from scratch** inside a new folder.

---

## 📂 INSTRUCTIONS

1. **Create a new folder** called `new_reform/` in the project root.
2. **Build the entire system** inside this folder.
3. **Do NOT touch the legacy files.** The legacy system stays untouched.
4. **Use the architecture from `02_ANALYSE_AND_REDESIGN.md`** as your blueprint.
5. **Write clean, modular, well-documented code.**
6. **Write tests** for every module.
7. **Write a README** for the new system.

---

## ✅ WHAT TO BUILD

### 1. Core Modules
- Command handlers (customer, admin, VIP).
- Database layer (SQLite + Gist backup).
- GitHub dispatcher (worker round-robin).
- Discord operations (forums, threads, permissions).
- Timer engine (expiry, reminders).
- Security (permissions, channel-awareness).

### 2. Sender (`send_ads.py`)
- Keep the existing `send_ads.py` and just clean it up (it's battle-tested).
- Or rewrite it if it's too messy.

### 3. Tests
- Unit tests for every module.
- Integration tests for the whole system.
- Use temporary databases and mocks.
- No destructive changes.

### 4. Documentation
- README.md (how to set up and run).
- ARCHITECTURE.md (how the system is organized).
- SKILL.md (AI operator skill).

### 5. Setup
- A new `setup.py` that provisions the new system.

---

## 🚀 START

**Create the `new_reform/` folder. Build the new system from scratch. Do not touch the legacy code.**