"""Taverns SDK error types."""

from __future__ import annotations


class TavernError(Exception):
    """Base exception for all Taverns SDK errors."""


class AuthenticationError(TavernError):
    """Raised when bot token authentication fails."""


class HTTPError(TavernError):
    """Raised when an API request fails."""

    def __init__(self, status: int, message: str, *, response_body: dict | None = None):
        self.status = status
        self.response_body = response_body
        super().__init__(f"HTTP {status}: {message}")


class RateLimitError(HTTPError):
    """Raised when rate limited (429). Contains retry_after in seconds."""

    def __init__(self, retry_after: float, message: str = "Rate limited"):
        self.retry_after = retry_after
        super().__init__(429, message)


class GatewayError(TavernError):
    """Raised on WebSocket gateway errors."""


class InteractionError(TavernError):
    """Raised when an interaction response fails."""
