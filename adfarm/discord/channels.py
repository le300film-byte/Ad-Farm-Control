"""ChannelClassifier — turns a ``ChannelRef`` into ``security.ChannelInfo``.

Order of evidence (most reliable first):
1. ids stored in the DB (customer forum ids, thread ids) → own/other hub;
2. ids configured in settings (admin alerts/chat/audit, ticket, proofs);
3. configured names (PUBLIC/TICKET/ADMIN channel names);
4. category naming conventions (``admin`` / customer-hub marker).
"""
from __future__ import annotations

from typing import Callable, Optional

from ..config import Settings
from ..core.models import Customer
from ..security.guards import ChannelInfo
from ..security.policy import ChannelKind
from .ports import ChannelRef


class ChannelClassifier:
    def __init__(self, settings: Settings, customer_by_forum: Callable[[str], Optional[Customer]]):
        self.settings = settings
        self.customer_by_forum = customer_by_forum
        self._admin_ids = {x for x in (settings.admin_alerts_channel_id, settings.admin_commands_channel_id, settings.admin_chat_channel_id, settings.audit_log_channel_id) if x}
        self._public = {n.lower() for n in settings.public_channel_names}
        self._ticket = {n.lower() for n in settings.ticket_channel_names}
        self._admin = {n.lower() for n in settings.admin_channel_names}

    def classify(self, ref: Optional[ChannelRef]) -> ChannelInfo:
        if ref is None or ref.kind == "dm" or not ref.guild_id:
            return ChannelInfo(channel_id=getattr(ref, "id", ""), kind_hint=ChannelKind.DM, name=getattr(ref, "name", ""))

        # 1. customer hubs by id
        for candidate in (ref.id, ref.parent_id):
            if candidate:
                customer = self.customer_by_forum(candidate)
                if customer is not None:
                    return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.OWN_HUB, hub_owner_id=customer.discord_id, name=ref.name)

        # 2. configured ids
        if ref.id in self._admin_ids or ref.parent_id in self._admin_ids:
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.ADMIN, name=ref.name)
        if self.settings.ticket_channel_id and ref.id == self.settings.ticket_channel_id:
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.TICKET, name=ref.name)

        # 3. names
        names = [n.lower().lstrip("#") for n in (ref.name, ref.category_name) if n]
        for name in names:
            if name in self._admin:
                return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.ADMIN, name=ref.name)
        if names and names[0] in self._ticket:
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.TICKET, name=ref.name)
        if names and names[0] in self._public:
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.PUBLIC, name=ref.name)

        # 4. conventions
        for name in names:
            if "admin" in name:
                return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.ADMIN, name=ref.name)
        if self.settings.customer_hub_category_id and ref.category_id == self.settings.customer_hub_category_id:
            # inside the hub category but not a known customer forum ⇒ somebody else's / orphaned hub
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.OTHER_HUB, name=ref.name)
        marker = self.settings.customer_hub_marker
        if marker and any(marker in n for n in names):
            return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.OTHER_HUB, name=ref.name)
        return ChannelInfo(channel_id=ref.id, kind_hint=ChannelKind.UNKNOWN, name=ref.name)
