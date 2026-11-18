from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from taverns import verify_webhook_signature

SECRET = "test-webhook-secret"
ROOT = Path(__file__).resolve().parent.parent


def delivery(timestamp: int | None = None, secret: str = SECRET) -> tuple[bytes, str, str]:
    ts = int(time.time() * 1000) if timestamp is None else timestamp
    body = json.dumps(
        {"event": "tavern_message", "data": {"content": "hi"}, "timestamp": ts, "botApplicationId": "app-1"},
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, signature, str(ts)


def test_valid_delivery_is_accepted():
    body, signature, ts = delivery()
    assert verify_webhook_signature(body, signature, SECRET, ts) is True
    assert verify_webhook_signature(body.decode(), signature, SECRET, ts) is True
    assert verify_webhook_signature(bytearray(body), signature, SECRET) is True
    assert verify_webhook_signature(body, signature.upper().replace("SHA256=", "sha256="), SECRET) is True


@pytest.mark.parametrize("secret", ["", None, b""])
def test_empty_or_missing_secret_raises(secret):
    body, signature, _ = delivery(secret="x")
    with pytest.raises(ValueError, match="secret is empty"):
        verify_webhook_signature(body, signature, secret)


def test_parsed_body_is_refused():
    body, signature, _ = delivery()
    with pytest.raises(TypeError, match="raw request body"):
        verify_webhook_signature(json.loads(body), signature, SECRET)


def test_wrong_secret_or_tampered_body_is_rejected():
    body, signature, _ = delivery()
    assert verify_webhook_signature(body, signature, "another-secret") is False
    assert verify_webhook_signature(body.replace(b"hi", b"ho"), signature, SECRET) is False


def test_freshness_uses_the_signed_body_timestamp():
    old_body, old_signature, old_ts = delivery(int(time.time() * 1000) - 10 * 60 * 1000)
    fresh = str(int(time.time() * 1000))
    # Replay of an old delivery with a fresh (unsigned) header, or with no header
    assert verify_webhook_signature(old_body, old_signature, SECRET, fresh) is False
    assert verify_webhook_signature(old_body, old_signature, SECRET) is False
    # A header that does not match the signed timestamp
    body, signature, ts = delivery()
    assert verify_webhook_signature(body, signature, SECRET, str(int(ts) + 1)) is False
    assert verify_webhook_signature(body, signature, SECRET, int(ts)) is True
    # max_age 0 / None turns the check off; a larger window accepts it
    assert verify_webhook_signature(old_body, old_signature, SECRET, max_age=0) is True
    assert verify_webhook_signature(old_body, old_signature, SECRET, max_age=None) is True
    assert verify_webhook_signature(old_body, old_signature, SECRET, old_ts, max_age=3600) is True


@pytest.mark.parametrize(
    "body", [b'{"event":"x"}', b'{"timestamp":"123"}', b'{"timestamp":true}', b"[]", b"not json", b"\xff\xfe"],
)
def test_signed_body_without_numeric_timestamp_fails_freshness(body):
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, signature, SECRET) is False
    assert verify_webhook_signature(body, signature, SECRET, max_age=0) is True


def test_garbage_and_non_ascii_signatures_return_false():
    body, signature, ts = delivery()
    hex_digest = signature[len("sha256="):]
    headers = [
        None, "", "sha256=", "sha256", "md5=" + hex_digest, hex_digest, "sha256=" + hex_digest[2:],
        "sha256=" + hex_digest + "00", "sha256=" + "zz" * 32, "sha256=é" + hex_digest[1:],
        "sha256=" + "ÿ" * 64, "sha256=\U0001F600", "sha256=" + "٠" * 64, 12345, b"sha256=00",
    ]
    for header in headers:
        assert verify_webhook_signature(body, header, SECRET) is False, header
    assert verify_webhook_signature(body, signature, SECRET, "é") is False


def _run_example(args: list[str], secret: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "WEBHOOK_SECRET"}
    env["PYTHONPATH"] = str(ROOT)
    if secret is not None:
        env["WEBHOOK_SECRET"] = secret
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT / "examples", env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_example_server_refuses_to_start_without_a_secret():
    pytest.importorskip("flask")
    # Imported by any launcher (python, flask, gunicorn): the check runs at import
    for args in (["-c", "import webhook_server"], ["-m", "flask", "--app", "webhook_server", "routes"]):
        for secret in (None, ""):
            result = _run_example(args, secret)
            assert result.returncode != 0, args
            assert "WEBHOOK_SECRET is not set" in result.stderr, result.stderr
    result = _run_example(["-m", "flask", "--app", "webhook_server", "routes"], SECRET)
    assert result.returncode == 0, result.stderr
    assert "/webhook" in result.stdout
