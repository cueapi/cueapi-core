"""Minimal CueAPI alert-webhook receiver.

Verifies the HMAC signature and prints the alert. Drop this behind a
reverse proxy with HTTPS and point your user's ``alert_webhook_url``
at the ``/cueapi-alerts`` path. Replace ``print`` with a forwarder to
your channel of choice (Slack, Discord, ntfy, SMTP relay, etc).

Retrieve your signing secret via ``GET /v1/auth/alert-webhook-secret``.
"""

import hashlib
import hmac
import json
import os

from flask import Flask, abort, request

SECRET = os.environ["CUEAPI_ALERT_WEBHOOK_SECRET"].encode()

app = Flask(__name__)


@app.post("/cueapi-alerts")
def receive() -> tuple[str, int]:
    ts = request.headers.get("X-CueAPI-Timestamp", "")
    sig = request.headers.get("X-CueAPI-Signature", "")
    body = request.get_data()  # raw bytes — sorted-keys JSON
    expected = hmac.new(SECRET, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, f"v1={expected}"):
        abort(401)
    alert = json.loads(body)
    print(f"[{alert['severity']}] {alert['alert_type']}: {alert['message']}")
    return "", 204


if __name__ == "__main__":
    app.run(port=8080)
