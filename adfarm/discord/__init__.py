"""Discord adapters: ports, channel classification, forum provisioning, embeds, replies."""
from .channels import ChannelClassifier
from .embeds import (account_embed, alt_status_embed, ban_notice_embed, customer_card, expiry_notice_embed, fleet_overview_embed, help_embed, reminder_embed)
from .forums import THREADS, VIP_THREADS, WEBHOOK_THREADS, ForumOutcome, ForumProvisioner
from .ports import ChannelRef, DiscordPort, Embed, ForumResult, ForumSpec, MessageRef
from .replies import Reply

__all__ = [
    "ChannelClassifier", "ForumProvisioner", "ForumOutcome", "THREADS", "VIP_THREADS", "WEBHOOK_THREADS", "DiscordPort", "ChannelRef", "MessageRef",
    "Embed", "ForumSpec", "ForumResult", "Reply", "help_embed", "account_embed", "alt_status_embed", "fleet_overview_embed", "customer_card",
    "reminder_embed", "expiry_notice_embed", "ban_notice_embed",
]
