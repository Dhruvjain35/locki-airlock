"""The owner's phone channel for LOCKI security dispatch (ntfy). Run by the owner, from backend/:

    .venv/bin/python scripts/airlock_notify.py setup-ntfy     # makes a private topic and writes data/notify/phone.json (owner-only)
    .venv/bin/python scripts/airlock_notify.py status         # what is configured (the topic is never printed again)
    .venv/bin/python scripts/airlock_notify.py test           # sends one test notification and prints the provider's receipt

The topic is the secret: anyone who knows it can read these notifications. setup-ntfy prints it once, for subscribing in the ntfy app,
and keeps it in data/notify/phone.json with mode 600 (data/ is never in Git). A receipt means ntfy accepted the message; only the owner
can say whether the phone showed it. Restart the backend after setup so it loads the channel.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.security.dispatch import NtfyChannel, PermanentError, TransientError, load_phone_channel  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["setup-ntfy", "status", "test"])
    p.add_argument("--server", default="https://ntfy.sh", help="an ntfy server (https); default https://ntfy.sh")
    p.add_argument("--replace", action="store_true", help="replace an existing phone channel")
    a = p.parse_args()
    path = Settings().data_dir / "notify" / "phone.json"

    if a.command == "setup-ntfy":
        if path.exists() and not a.replace:
            print(f"{path} already exists; pass --replace to make a new topic (the old one stops receiving)")
            return 1
        topic = "locki-" + secrets.token_urlsafe(18)
        NtfyChannel(topic, a.server)  # validates the server and the topic
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"provider": "ntfy", "server": a.server, "topic": topic}, f)
        os.chmod(path, 0o600)
        print("Phone channel written to", path, "(mode 600).")
        print("In the ntfy app on your phone: Subscribe to topic ->", topic, "on", a.server)
        print("This topic is shown only now. Keep it private. Then restart the backend so it loads the channel, and run: test")
        return 0

    channel, error = load_phone_channel(path)
    if channel is None:
        print("no phone channel:", error or f"{path} does not exist (run setup-ntfy)")
        return 1
    if a.command == "status":
        print(json.dumps(channel.describe()))
        return 0
    try:
        receipt = channel.send("LOCKI test notification", "A test from LOCKI security dispatch. No incident.", "default")
    except (TransientError, PermanentError) as exc:
        print("not accepted:", exc)
        return 1
    print(f"accepted by ntfy, receipt {receipt}. Check that your phone showed it: only you can confirm that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
