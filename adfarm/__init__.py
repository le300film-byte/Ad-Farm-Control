"""adfarm — the reformed AdFarm control plane.

Layout (see ARCHITECTURE.md):
  core/       pure domain models, rules, errors, clock
  db/         SQLite database, repositories, token vault, Gist write-through backup
  github/     REST client, worker-account pool, secrets sealing, repos, workflows, control queue
  discord/    channel classification, forum provisioning, embed builders, reply objects
  security/   tiers, command/channel policy, guards, redaction
  telemetry/  heartbeat parsing, in-memory fleet state, webhook ingestion routing
  timers/     expiry / limitless-renewal engines + asyncio scheduler
  services/   use-cases (customers, alts, runs, tickets, alerts, bans)
  commands/   framework-agnostic slash-command handlers + discord.py registry
"""

__version__ = "9.0.0"
