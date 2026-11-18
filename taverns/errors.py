"""Taverns SDK error types."""

from __future__ import annotations

import re

# An upper-case error token such as ``CANNOT_DM_USER``
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]+")


def _error_code(body: dict | None) -> str | None:
    """The error code of an answer body: ``code``, or an ``error`` that is a code."""
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if isinstance(code, str) and code:
        return code
    error = body.get("error")
    if isinstance(error, str) and _ERROR_CODE_RE.fullmatch(error):
        return error
    return None


class TavernError(Exception):
    """Base exception for all Taverns SDK errors."""


class AuthenticationError(TavernError):
    """Raised when bot token authentication fails."""


class HTTPError(TavernError):
    """Raised when an API request fails.

    Attributes:
        status: The HTTP status.
        response_body: The JSON answer, when it was an object.
        code: The error code the API sent, such as ``"CANNOT_DM_USER"``
            (see :class:`DMErrorCode`), or ``None``.
    """

    def __init__(self, status: int, message: str, *, response_body: dict | None = None):
        self.status = status
        self.response_body = response_body
        self.code: str | None = _error_code(response_body)
        super().__init__(f"HTTP {status}: {message}")


class RateLimitError(HTTPError):
    """Raised when rate limited (429).

    Requests over the general limit are sent again for you first; this is raised
    when the retries run out. Requests over a direct message limit
    (``code == "BOT_DM_RATE_LIMITED"``) are not sent again: this is raised at once,
    with the limit in ``scope``.

    Attributes:
        retry_after: Seconds to wait before trying again.
        scope: Which limit was hit, when the API says. For direct messages:
            ``user_burst``, ``user_hourly``, ``unanswered``, ``global``,
            ``open_burst`` or ``open_daily``.
    """

    def __init__(
        self,
        retry_after: float,
        message: str = "Rate limited",
        *,
        scope: str | None = None,
        response_body: dict | None = None,
    ):
        self.retry_after = retry_after
        self.scope = scope
        super().__init__(429, message, response_body=response_body)


class DMErrorCode:
    """Error codes of failed direct message requests (``HTTPError.code``)."""

    CANNOT_DM_USER = "CANNOT_DM_USER"
    """The bot may not message this person (one answer for every reason)."""
    BOT_DM_DISABLED = "BOT_DM_DISABLED"
    """Direct messages are turned off for this app."""
    BOT_DM_REVIEW_REQUIRED = "BOT_DM_REVIEW_REQUIRED"
    """Opening new DMs needs the app to pass review (public apps in more than 100 taverns)."""
    DM_CHANNEL_NOT_FOUND = "DM_CHANNEL_NOT_FOUND"
    """The channel is not one of this bot's direct messages."""
    DM_MESSAGE_NOT_FOUND = "DM_MESSAGE_NOT_FOUND"
    """The message does not exist in this direct message."""
    NOT_YOUR_MESSAGE = "NOT_YOUR_MESSAGE"
    """The bot can edit and delete only its own messages."""
    BOT_DM_RATE_LIMITED = "BOT_DM_RATE_LIMITED"
    """A direct message limit was hit; see ``RateLimitError.scope`` and ``retry_after``."""


class GatewayError(TavernError):
    """Raised on WebSocket gateway errors."""


class InteractionError(TavernError):
    """Raised when an interaction response fails."""
