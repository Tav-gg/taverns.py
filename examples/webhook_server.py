"""Webhook receiver — verifies signatures and handles events.

Requires: pip install flask

Run (either way):
    WEBHOOK_SECRET=your_secret flask --app webhook_server run --port 8080
    WEBHOOK_SECRET=your_secret python webhook_server.py

The secret is checked when this file is imported, so the server refuses to start
without it however it is launched (flask, python, gunicorn, ...).
"""

import json
import os

from flask import Flask, request

from taverns import verify_webhook_signature

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    raise SystemExit(
        "WEBHOOK_SECRET is not set. Set it to the webhook signing secret of your bot; "
        "without it, anyone could send this server fake events."
    )

app = Flask(__name__)


@app.post("/webhook")
def handle_webhook():
    raw_body = request.get_data()

    # Verify the signature and that the delivery is recent (5 minutes)
    is_valid = verify_webhook_signature(
        raw_body=raw_body,
        signature=request.headers.get("X-Tavern-Signature"),
        secret=WEBHOOK_SECRET,
        timestamp=request.headers.get("X-Tavern-Timestamp"),
    )
    if not is_valid:
        return "Invalid signature", 401

    # Parse event
    payload = json.loads(raw_body)
    event = payload.get("event", "unknown")
    data = payload.get("data", {})
    delivery_id = request.headers.get("X-Tavern-Delivery-Id", "?")

    print(f"[{delivery_id}] Received {event}: {json.dumps(data)[:200]}")

    # Handle events
    if event == "tavern_message":
        content = data.get("content", "")
        sender = data.get("senderId", "?")
        print(f"  Message from {sender}: {content}")

    elif event == "interaction_create":
        command_name = data.get("commandName", "")
        print(f"  Interaction: /{command_name}")

    return "OK", 200


if __name__ == "__main__":
    app.run(port=8080)
