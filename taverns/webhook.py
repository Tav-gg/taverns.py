"""Webhook signature verification for Tavern bot webhooks.

Usage::

    from taverns import verify_webhook_signature

    @app.post("/webhook")
    def handle_webhook(request):
        is_valid = verify_webhook_signature(
            raw_body=request.data,
            signature=request.headers.get("X-Tavern-Signature"),
            secret=WEBHOOK_SECRET,
            timestamp=request.headers.get("X-Tavern-Timestamp"),
        )
        if not is_valid:
            return "Invalid signature", 401

        event = json.loads(request.data)
        # handle event...
        return "OK", 200
"""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_webhook_signature(
    raw_body: str | bytes,
    signature: str | None,
    secret: str,
    timestamp: str | None = None,
    max_age: int = 300,
) -> bool:
    """Verify a Tavern webhook request signature.

    Args:
        raw_body: The raw request body (string or bytes).
        signature: The ``X-Tavern-Signature`` header (format: ``sha256=<hex>``).
        secret: Your webhook signing secret.
        timestamp: The ``X-Tavern-Timestamp`` header (optional, for replay protection).
        max_age: Maximum age in seconds for timestamp freshness (default 300 = 5 min).

    Returns:
        True if the signature is valid and the timestamp is fresh (if provided).
    """
    if not signature:
        return False

    # Timestamp freshness check
    if timestamp is not None:
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False
        age = abs(time.time() * 1000 - ts) / 1000  # timestamp is in ms
        if age > max_age:
            return False

    # Parse signature: "sha256=<hex>"
    parts = signature.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        return False
    provided_hex = parts[1]

    # Compute expected signature
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
    expected_hex = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()

    # Timing-safe comparison
    return hmac.compare_digest(provided_hex, expected_hex)
