"""Owner commands for a running AIRLOCK backend (make airlock-mac), until the dashboard covers them. The owner code is typed here and never shown or stored in plain text.

    .venv/bin/python scripts/airlock.py status
    .venv/bin/python scripts/airlock.py arm [--auto-lock] [--auto-lock-apps] [--approve-device FINGERPRINT] [--approve-app BUNDLE_ID] [--freeze-tools]
    .venv/bin/python scripts/airlock.py disarm
    .venv/bin/python scripts/airlock.py lock          # dry run unless the backend was started with LOCK=real
    .venv/bin/python scripts/airlock.py preflight     # what a lock would do, and the Mac's settings. Changes nothing

With the team's SpacetimeDB:

    .venv/bin/python scripts/airlock.py spacetime-setup --workstation dhruv-m4-air   # writes data/security/spacetime.json and makes this Mac's own identity
    .venv/bin/python scripts/airlock.py owner-commands                                 # the owner's register, enroll and arm commands, filled in

Arming then belongs to the owner on the SpacetimeDB dashboard (or the printed command); this Mac follows. Nothing here uses or prints a token.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("LOCKEDIN_URL", "http://127.0.0.1:8077")


def show_status(s: dict) -> None:
    monitor = s.get("monitor") or {}
    usb, apps = monitor.get("usb") or {}, monitor.get("apps") or {}
    print(f"state: {s['state']} ({s['state_source']})   lock mode: {s['lock_mode']} via {s['lock_mechanism']}")
    print(f"USB: {usb.get('method')}, {usb.get('connected')} connected, error: {usb.get('error')}")
    print(f"apps: {apps.get('method')} ({apps.get('coverage')}), error: {apps.get('error')}")
    publisher = s.get("publisher") or {}
    print(f"events: {publisher.get('published')} published to {publisher.get('transport')}, last delivery {publisher.get('last_delivery')}")
    for w in s.get("warnings") or []:
        print(f"warning: {w['reason']}, {w.get('label')}: queued {w['queued_ms']} ms after the OS report, played: {w.get('played')}, audio: {w.get('audio')}")
    for lock in s.get("locks") or []:
        print(f"lock: {lock['status']} dry_run={lock['dry_run']} {lock.get('detail')}")
    if s.get("investigation"):
        i = s["investigation"]
        print(f"investigation: {i['status']} by {i['model_id']}, {i['approved_tool_calls']} approved tool calls, counts as Nemotron: {i['qualifies_as_nemotron']}")
    print(s["limits"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "arm", "disarm", "lock", "preflight", "spacetime-setup", "owner-commands", "camera-setup", "camera-probe"])
    ap.add_argument("--camera-role", default="jetson", help="the role Sid's module expects for the desk camera (to confirm with Sid)")
    ap.add_argument("--workstation", default=None)
    ap.add_argument("--url", default="https://maincloud.spacetimedb.com")
    ap.add_argument("--database", default="locki-w91bw")
    ap.add_argument("--role", default="mac", help="the device role Sid's module expects")
    ap.add_argument("--minutes", type=int, default=120)
    ap.add_argument("--auto-lock", action="store_true", help="opt in to automatic screen lock on an unknown USB device")
    ap.add_argument("--auto-lock-apps", action="store_true", help="opt in to automatic lock when a watched app opens as well")
    ap.add_argument("--approve-device", action="append", default=[])
    ap.add_argument("--approve-app", action="append", default=[])
    ap.add_argument("--freeze-tools", action="store_true", help="during an incident, stop protected tools except reading")
    args = ap.parse_args()
    if args.command == "spacetime-setup":
        return spacetime_setup(args)
    if args.command == "camera-setup":
        return camera_setup(args)
    if args.command == "camera-probe":
        return camera_probe(args)
    if args.command == "owner-commands":
        return owner_commands(args)
    try:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            if args.command == "status":
                show_status(c.get("/api/security").raise_for_status().json())
                return 0
            if args.command == "preflight":
                print(json.dumps(c.get("/api/security/lock/preflight").raise_for_status().json(), indent=1))
                return 0
            code = getpass.getpass("Owner code (arming sets it; disarming needs it): ")
            if args.command == "arm":
                policy = {"auto_lock": args.auto_lock, "auto_lock_on_watchlist_app": args.auto_lock_apps, "approved_devices": args.approve_device, "approved_apps": args.approve_app, "freeze_protected_tools": args.freeze_tools}
                r = c.post("/api/security/arm", json={"owner_code": code, "policy": policy})
            elif args.command == "disarm":
                r = c.post("/api/security/disarm", json={"owner_code": code})
            else:
                r = c.post("/api/security/lock", json={"owner_code": code})
            if r.status_code >= 400:
                print(f"refused: {r.json().get('detail', r.text)}")
                return 1
            if args.command == "lock":
                print(json.dumps(r.json(), indent=1))
            else:
                show_status(r.json())
            return 0
    except httpx.ConnectError:
        print(f"nothing is answering at {BASE}: start it with make airlock-mac")
        return 2


DATA = Path(__file__).resolve().parents[2] / "data" / "security"


def spacetime_setup(args) -> int:
    """Not a secret: where the database is and which workstation this Mac is. The agent's own token goes in its owner-only file, never here."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import agent_identity, load_or_create_token

    if not args.workstation:
        print("give --workstation, the id the owner registers")
        return 2
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "spacetime.json").write_text(json.dumps({"url": args.url, "database": args.database, "workstation_id": args.workstation}, indent=1))
    load_or_create_token(args.url, DATA)
    print(f"saved {DATA / 'spacetime.json'} (git-ignored). This Mac agent: device {load_identity(DATA).device_id}, identity {agent_identity(args.url, DATA)}")
    return owner_commands(args)


def owner_commands(args) -> int:
    """The owner's commands, filled in. The owner runs them with their own login; this script never does."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import agent_identity

    try:
        stored = json.loads((DATA / "spacetime.json").read_text())
    except (OSError, ValueError):
        print("run spacetime-setup first")
        return 2
    ws, url, db = stored["workstation_id"], stored["url"], stored["database"]
    device, identity = load_identity(DATA).device_id, agent_identity(url, DATA)
    server = "maincloud" if "maincloud" in url else url
    q = lambda v: "'" + json.dumps(v).replace("'", "'\\''") + "'"  # noqa: E731
    print("\nThe owner runs these, with their own SpacetimeDB login:\n")
    print(f"spacetime call --server {server} --no-config {db} register_workstation {q(ws)} {q('Dhruv M4 Air')}")
    print(f"spacetime call --server {server} --no-config {db} enroll_device {q(ws)} {q(device)} {q(identity)} {q(args.role)} {q('Mac security agent')}")
    # The module checks the baseline against the latest capture this Mac recorded, so the command uses that exact capture, read back from the database.
    from app.security.spacetime_adapter import load_or_create_token

    token = load_or_create_token(url, DATA)
    try:
        r = httpx.post(f"{url}/v1/database/{db}/sql", headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}, content=f"SELECT baseline_json, observed_at_ms FROM my_baseline_snapshots WHERE device_id = '{device}'", timeout=10)
        rows = r.json()[0]["rows"] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        rows = []
    if not rows:
        print("\nNo capture from this Mac in the database yet: start the backend (make airlock-mac) and run this again.")
        return 1
    rows = sorted(rows, key=lambda r: r[1], reverse=True)  # the module arms against the NEWEST capture; rows come back in no order
    captured, observed = rows[0][0], rows[0][1]
    session = f"{ws}-{int(time.time())}"
    print(f"\nTo arm with the Mac's latest capture ({len(json.loads(captured))} device ids, recorded {round(time.time() - observed / 1000)} s ago). Run it soon: the capture must be fresh.\n")
    print(f"spacetime call --server {server} --no-config {db} arm_session {q(ws)} {q(session)} {q(captured)} {q('[]')} {args.minutes * 60000} false")
    return 0



CAMERA = DATA.parent / "airlock_vision"


def _mac_spacetime() -> dict | None:
    try:
        return json.loads((DATA / "spacetime.json").read_text())
    except (OSError, ValueError):
        return None


def camera_setup(args) -> int:
    """The desk camera's own identity, and the owner's command to enroll it. Its events stay local until enabled."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import agent_identity, load_or_create_token

    stored = _mac_spacetime()
    if stored is None:
        print("run spacetime-setup for the Mac first")
        return 2
    device = load_identity(CAMERA, prefix="cam").device_id
    load_or_create_token(stored["url"], CAMERA)
    identity = agent_identity(stored["url"], CAMERA)
    conf = CAMERA / "spacetime.json"
    if not conf.exists():
        conf.write_text(json.dumps({"enabled": False}, indent=1) + "\n")
    server = "maincloud" if "maincloud" in stored["url"] else stored["url"]
    q = lambda v: "'" + json.dumps(v).replace("'", "'\\''") + "'"  # noqa: E731
    print(f"desk camera: device {device}, identity {identity} (its token is in {CAMERA}, owner-only)")
    print("\nThe owner runs this, with their own SpacetimeDB login (the role is a guess until Sid confirms it):\n")
    print(f"spacetime call --server {server} --no-config {stored['database']} enroll_device {q(stored['workstation_id'])} {q(device)} {q(identity)} {q(args.camera_role)} {q('Desk camera')}")
    print("\nThen: .venv/bin/python scripts/airlock.py camera-probe   (asks the module which camera kinds it takes; stores nothing)")
    return 0


def camera_probe(args) -> int:
    """Which camera kinds Sid's module takes from the enrolled camera. Every call names a session that does not exist, so the module
    refuses it and stores nothing; the reason it gives says whether the kind itself was acceptable."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import CAMERA_EVENT_KINDS, load_or_create_token

    stored = _mac_spacetime()
    if stored is None:
        print("run spacetime-setup first")
        return 2
    url, db, ws = stored["url"].rstrip("/"), stored["database"], stored["workstation_id"]
    device = load_identity(CAMERA, prefix="cam").device_id
    token = load_or_create_token(url, CAMERA)
    candidates = sorted(set(CAMERA_EVENT_KINDS.values()) | {"person_present", "person_detected", "hand_near_usb", "usb_zone_interaction", "hand_near_usb_zone", "keyboard_interaction", "camera_obstructed", "unrecognized_face", "owner_face_match"})
    accepted = []
    for kind in candidates:
        args_list = [ws, f"probe-no-such-session-{int(time.time())}", device, f"probe-{kind}-{int(time.time() * 1000)}", kind, int(time.time() * 1000), 0, 1.0, json.dumps({"zone": "desk"}), True, 1]
        r = httpx.post(f"{url}/v1/database/{db}/call/ingest_device_event", headers={"Authorization": f"Bearer {token}"}, json=args_list, timeout=10)
        text = r.text.strip().replace("\n", " ")[:160]
        verdict = "stored?" if r.status_code == 200 else ("kind refused" if "kind" in text.lower() and ("not permitted" in text.lower() or "unknown" in text.lower()) else "kind ok, refused later" if r.status_code == 530 else f"HTTP {r.status_code}")
        if verdict == "kind ok, refused later":
            accepted.append(kind)
        print(f"  {kind:<22} {verdict:<24} {text}")
    print(f"\nkinds the module seems to take from this camera: {accepted or 'none'}")
    print("If they differ from CAMERA_EVENT_KINDS, put \"camera_kinds\" in data/airlock_vision/spacetime.json; set \"enabled\": true to publish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
