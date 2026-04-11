"""Webhook receiver — verifies signatures and handles events.

Requires: pip install flask

Run: WEBHOOK_SECRET=your_secret flask --app webhook_server run --port 8080
"""

import json
import os

from flask import Flask, request

from taverns import verify_webhook_signature

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


@app.post("/webhook")
def handle_webhook():
    # Verify signature
    is_valid = verify_webhook_signature(
        raw_body=request.data,
        signature=request.headers.get("X-Tavern-Signature"),
        secret=WEBHOOK_SECRET,
        timestamp=request.headers.get("X-Tavern-Timestamp"),
    )
    if not is_valid:
        return "Invalid signature", 401

    # Parse event
    payload = json.loads(request.data)
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
    if not WEBHOOK_SECRET:
        print("Set WEBHOOK_SECRET environment variable")
        raise SystemExit(1)
    app.run(port=8080)
