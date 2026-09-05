"""The service agreement shown to customers before their first run.

Single source of truth (P1-5): ``TicketService.policy_embed`` renders it, ``/getstarted``
quotes it, and ``POLICY_VERSION`` is what ``policy_acks`` records — so changing the wording
here automatically re-asks every customer to confirm the new version.

V9.1 shipped a risk-first policy ("you accept the ban risk", "bans within 48 h get credit",
"violations end the subscription without refund"). That is the operator's internal risk model,
not an onboarding message; it scared off buyers at the exact moment they were about to pay.
This version states the same facts in service terms.
"""
from __future__ import annotations

from ..core.rules import POLICY_VERSION  # noqa: F401  (re-exported)

POLICY_TITLE = "📜 AdFarm V9 — Service Agreement"

POLICY_TEXT = (
    "**AdFarm V9 — Service Agreement**\n"
    "1. Crypto payments only — BEP-20 (USDT/BUSD).\n"
    "2. Data stored: Discord ID, username, repos, dates.\n"
    "3. Account setup: main accounts are not supported.\n"
    "4. Support: best-effort, we aim to respond quickly.\n"
    "5. No illegal goods, harassment, or mass-mention.\n"
    "6. Alt tokens are stored encrypted and only used for re-provisioning.\n\n"
    "Click ✅ below to confirm you understand the above and would like to proceed.\n"
    "We'll share payment details right after."
)

#: Label of the acknowledgement button rendered by the registry (``PolicyAckView``).
POLICY_ACCEPT_LABEL = "✅ I understand — let's proceed"

