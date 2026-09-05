"""Typed error hierarchy. Command handlers map these to user-facing replies."""
from __future__ import annotations


class AdFarmError(Exception):
    """Base class; ``user_message`` is safe to show in Discord."""

    user_message = "❌ Something went wrong. Please try again or contact an admin."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.user_message)
        if message:
            self.user_message = message


class NotAuthorized(AdFarmError):
    user_message = "❌ You are not authorized to use this command."


class WrongChannel(AdFarmError):
    user_message = "❌ This command is not available in this channel."


class SubscriptionExpired(AdFarmError):
    user_message = "❌ Your subscription has expired. Contact an admin to renew."


class NoSubscription(AdFarmError):
    user_message = "❌ You do not have an active subscription. You are not authorized to use this command."


class VipRequired(AdFarmError):
    user_message = "❌ This feature requires VIP. Ask an admin to upgrade your plan."


class NotFound(AdFarmError):
    user_message = "❓ Not found."


class ValidationError(AdFarmError):
    user_message = "❌ Invalid input."


class ConflictError(AdFarmError):
    user_message = "⚠️ The operation conflicts with the current state."


class ExternalServiceError(AdFarmError):
    """GitHub / Discord / Gist failure. ``detail`` is for logs, never for users."""

    user_message = "⚠️ An external service failed. The action was not completed."

    def __init__(self, detail: str, status: int | None = None):
        super().__init__()
        self.detail = detail
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.detail} (status={self.status})"


class ConfigurationError(AdFarmError):
    user_message = "⚠️ The bot is not fully configured for this action. An admin has been notified."
