"""URL helpers.

Every ID placed in a request path goes through :func:`path_segment`, so an ID
that comes from user input (a slash-command option, message text) can never
change which route a request is sent to.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit


def path_segment(value: object) -> str:
    """Escape one URL path segment.

    Empty values, ``.`` and ``..`` are refused, because they would point the
    request at a different route.
    """
    if value is None:
        raise ValueError("Missing ID for a URL path (got None)")
    segment = str(value)
    if segment in ("", ".", ".."):
        raise ValueError(f"Invalid ID for a URL path: {segment!r}")
    return quote(segment, safe="")


def check_base_url(url: str, kind: str, *, allow_insecure: bool = False) -> None:
    """Check a custom API (``kind="api"``) or gateway (``kind="gateway"``) URL.

    The bot token travels with every request, so only ``https://`` (API) and
    ``wss://`` (gateway) are accepted, unless ``allow_insecure`` is set for local
    development.
    """
    if kind == "api":
        secure, insecure, label = "https", "http", "The API URL"
    else:
        secure, insecure, label = "wss", "ws", "The gateway URL"

    try:
        parts = urlsplit(url)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{label} is not a valid URL") from None
    scheme = parts.scheme.lower()
    if not parts.netloc:
        raise ValueError(f"{label} is not a valid URL")

    if scheme == secure:
        return
    if scheme == insecure:
        if allow_insecure:
            return
        raise ValueError(
            f"{label} uses {insecure}://, which would send the bot token unencrypted. "
            f"Use {secure}://, or pass allow_insecure=True for local development only."
        )
    raise ValueError(f"{label} must start with {secure}://")
