"""Webhook signature verification for Tavern bot webhooks.

Taverns signs the raw request body with HMAC-SHA256 and your webhook secret and
sends the hex digest in the ``X-Tavern-Signature`` header as ``sha256=<hex>``.
The body also carries ``timestamp`` (Unix time in milliseconds); because it is
part of the signed body, it is the one used for the freshness check.

Usage::

    import os
    from taverns import verify_webhook_signature

    WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

    @app.post("/webhook")
    def handle_webhook():
        is_valid = verify_webhook_signature(
            raw_body=request.get_data(),
            signature=request.headers.get("X-Tavern-Signature"),
            secret=WEBHOOK_SECRET,
            timestamp=request.headers.get("X-Tavern-Timestamp"),
        )
        if not is_valid:
            return "Invalid signature", 401

        event = json.loads(request.get_data())
        # handle event...
        return "OK", 200
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time

_SIGNATURE_RE = re.compile(r"sha256=([0-9a-f]{64})", re.IGNORECASE | re.ASCII)


def verify_webhook_signature(
    raw_body: str | bytes,
    signature: str | None,
    secret: str,
    timestamp: str | int | None = None,
    max_age: float | None = 300,
) -> bool:
    """Verify a Tavern webhook request.

    Args:
        raw_body: The raw request body exactly as received (bytes or str), not parsed JSON.
        signature: The ``X-Tavern-Signature`` header (format: ``sha256=<hex>``).
        secret: Your webhook signing secret. An empty secret raises ``ValueError``.
        timestamp: The ``X-Tavern-Timestamp`` header (optional). When given, it must
            match the timestamp inside the signed body.
        max_age: Maximum age in seconds (default 300 = 5 min). The age is taken from the
            ``timestamp`` inside the signed body. ``0`` or ``None`` turns the check off.

    Returns:
        True if the signature is valid and the request is fresh.

    Raises:
        ValueError: if ``secret`` is empty.
        TypeError: if ``raw_body`` is not bytes or str.
    """
    if not isinstance(secret, (str, bytes)) or not secret:
        raise ValueError(
            "verify_webhook_signature: the webhook secret is empty. "
            "Pass the signing secret you set for your bot."
        )
    key = secret.encode("utf-8") if isinstance(secret, str) else secret

    if isinstance(raw_body, str):
        body = raw_body.encode("utf-8")
    elif isinstance(raw_body, (bytes, bytearray, memoryview)):
        body = bytes(raw_body)
    else:
        raise TypeError(
            "verify_webhook_signature: raw_body must be the raw request body (bytes or str), "
            "not parsed JSON"
        )

    if not isinstance(signature, str):
        return False
    match = _SIGNATURE_RE.fullmatch(signature.strip())
    if not match:
        return False

    # Timing-safe comparison of ASCII bytes (never raises on odd input)
    provided = match.group(1).lower().encode("ascii")
    expected = hmac.new(key, body, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(provided, expected):
        return False

    if not max_age or max_age <= 0:
        return True

    # Freshness: use the timestamp inside the signed body, never the header alone
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    signed_ts = payload.get("timestamp") if isinstance(payload, dict) else None
    if isinstance(signed_ts, bool) or not isinstance(signed_ts, (int, float)):
        return False
    if not math.isfinite(signed_ts):
        return False

    if timestamp is not None:
        try:
            header_ts = float(str(timestamp).strip())
        except ValueError:
            return False
        if header_ts != signed_ts:
            return False

    return abs(time.time() * 1000 - signed_ts) <= max_age * 1000
