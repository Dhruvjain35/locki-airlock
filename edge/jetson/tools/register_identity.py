"""Create the Jetson's own SpacetimeDB identity and save its token (chmod 600).

    python3 tools/register_identity.py --stdb-url http://<host>:3000 --out ~/.locki-edge/stdb_token

Give the printed identity to Sid: his module must enroll it as this workstation's
jetson_device_id, and ingest_device_event must check ctx.sender against it.
"""
import argparse
import json
import os
import sys
from urllib.request import Request, urlopen


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stdb-url", required=True)
    p.add_argument("--out", default=os.path.expanduser("~/.locki-edge/stdb_token"))
    p.add_argument("--force", action="store_true", help="overwrite an existing token (creates a NEW identity)")
    a = p.parse_args()
    out = os.path.expanduser(a.out)
    if os.path.exists(out) and not a.force:
        sys.exit("token already exists at %s (use --force to mint a new identity)" % out)
    resp = urlopen(Request(a.stdb_url.rstrip("/") + "/v1/identity", data=b"", method="POST"), timeout=10)
    data = json.loads(resp.read().decode("utf-8"))
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data["token"])
    print("identity: %s" % data["identity"])
    print("token saved: %s (0600) - never commit it" % out)


if __name__ == "__main__":
    main()
