# 01_DESCRIBE_THE_PROJECT.md — Full System Description

## 🎯 YOUR TASK

You have full access to the codebase. Your job is to **describe this system completely**—as if you are writing the ultimate specification document for a senior engineer who needs to rebuild it from scratch.

This is NOT a summary. This is a **complete, exhaustive description** of:
- What the system does.
- How it works.
- What every component does.
- What state exists and where it lives.
- What the failure modes are.
- What the business rules are.

The goal: **A senior engineer should be able to read this document and rebuild the entire system without ever looking at the existing code.**

---

## 📋 WHAT TO DESCRIBE

### 1. System Overview
- What is this system? (One paragraph.)
- Who uses it? (Admins, customers, VIPs, non-customers.)
- What problem does it solve?

### 2. Architecture
- **Components**: What are the major pieces? (Control bot, sender, database, Gist, GitHub, Discord.)
- **Data Flow**: How does data move between components?
- **State**: Where is state stored? (Database, Gist, in-memory, GitHub secrets, file system.)
- **External Dependencies**: What external services does it rely on? (Discord API, GitHub API, Gist API.)

### 3. User Roles & Permissions
- **Admin**: What can they do? What commands do they have?
- **VIP Customer**: What can they do? What commands do they have?
- **Customer**: What can they do? What commands do they have?
- **Non-Customer**: What can they do? What commands do they see?

### 4. Commands (Complete List)
For every slash command:
- **Name**: `/command`
- **Description**: What does it do?
- **Who can use it**: Admin / VIP / Customer / Non-customer.
- **Where can it be used**: Which channels/forums?
- **Parameters**: What inputs does it take?
- **Output**: What does it return?
- **Behavior**: What happens when it's called?

### 5. Database Schema
- What tables exist?
- What columns does each table have?
- What is the purpose of each table?
- What are the relationships between tables?

### 6. Gist Backup
- What is backed up to Gist?
- When is it backed up?
- How is it restored on startup?
- What happens if Gist fails?

### 7. GitHub Operations
- What repos are created? (Where, how, naming convention.)
- What secrets are set? (Which ones, where?)
- What workflows are dispatched? (When, how?)
- What is the round-robin logic for worker accounts?

### 8. Discord Operations
- What channels are created? (When, where, naming convention?)
- What forums are created? (When, where, naming convention?)
- What permissions are set? (Who can see what?)
- What threads are created? (When, where?)

### 9. Timer Engine
- What does it check? (Expiry, reminders.)
- When does it run? (Schedule, frequency.)
- What does it do when a subscription expires?
- What are the reminder thresholds? (7 days, 3 days, 1 day.)

### 10. Sender (`send_ads.py`)
- What does it do? (Post ads, deal scanner, anti-detection.)
- How does it work? (WARP, gateway, typing, AFK breaks.)
- What state does it maintain? (Channel registry, blocklist, variations.)
- What are the failure modes? (Bans, 403s, 404s, rate limits.)
- How does it handle bans? (Detection, time credit, re-setup.)

### 11. Security & Hardening
- How are commands protected?
- How are secrets handled?
- What is the 2FA policy?
- What is the PAT rotation policy?
- How is customer isolation enforced?

### 12. Business Rules
- Pricing: What are the tiers? ($5/mo, $1/extra alt, $7.5 VIP.)
- Refund policy: What is it?
- Ban credit: What is the policy?
- Alt limits: How many alts per customer?
- Channel limits: How many channels per alt?
- VIP features: What is unlocked by VIP?

### 13. Known Issues & Limitations
- What bugs currently exist?
- What are the known limitations?
- What edge cases are not handled?

### 14. Setup & Deployment
- What does `setup.py` do?
- What environment variables are required?
- What secrets are required?
- What are the steps to deploy?

### 15. Testing
- What tests exist?
- What do they test?
- Are they isolated? (Do they leave destructive changes?)

---

## 📝 OUTPUT FORMAT

Write this as a **structured document** with clear headings. Be exhaustive. Be precise.

---

## 🚀 START

**Read the entire codebase. Write the complete description. Leave nothing out.**