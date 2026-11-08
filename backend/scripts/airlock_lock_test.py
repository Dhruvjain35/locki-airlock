"""The first real screen lock, on the owner's own Mac, with the owner watching. It never runs by itself, and it is not part of any check or test.

    .venv/bin/python scripts/airlock_lock_test.py                          # preflight only: what would happen. Changes nothing
    .venv/bin/python scripts/airlock_lock_test.py --lock --mechanism keystroke
    .venv/bin/python scripts/airlock_lock_test.py --lock --mechanism login_framework

With --lock it asks you to type LOCK MY MAC, then locks the screen once through the chosen mechanism. It reports requested, executed and confirmed only as each
becomes true, then waits for you to unlock normally with your password and records how you got back in. It changes no setting, and you can always unlock with
your normal password. Save your work first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OUT = Path(__file__).resolve().parents[2] / "data" / "airlock_runs"


def main() -> int:
    from app.security.contracts import LockRequestV1, LockStatus
    from app.security.mac_lock import LockAdapter, accessibility_trusted, screen_lock_setting
    from app.security.session import ResponsePolicy, new_local_session

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", action="store_true")
    ap.add_argument("--mechanism", choices=["keystroke", "login_framework"], default="keystroke")
    args = ap.parse_args()

    setting, trusted = screen_lock_setting(), accessibility_trusted()
    probe = LockAdapter(mode="dry_run", mechanism=args.mechanism)
    print("PREFLIGHT")
    print(f"  password after sleep or screen saver (macOS setting): {setting}" + ("  <- off: check below whether this lock asks for your password" if setting == "off" else ""))
    print(f"  Accessibility for the app running this (needed by keystroke, Apple's Control-Command-Q): {trusted}")
    print("  login_framework is an undocumented macOS call and needs no permission")
    print(f"  screen locked right now: {probe.is_locked()}")
    if args.mechanism == "keystroke" and trusted is False:
        print("\nkeystroke would fail: the app running this has no Accessibility permission. Grant it in System Settings, or use --mechanism login_framework.")
        if args.lock:
            return 2
    if not args.lock:
        print("\nNothing was locked. Add --lock to run the test.")
        return 0

    if input("\nThis locks your screen once. Save your work. Type LOCK MY MAC to go ahead: ").strip() != "LOCK MY MAC":
        print("Not locked.")
        return 1
    events: list[dict] = []
    adapter = LockAdapter(mode="real", mechanism=args.mechanism, on_status=lambda r: events.append({"status": r.status.value, "at_ms": int(time.time() * 1000), "detail": r.detail}))
    session = new_local_session({}, ResponsePolicy(auto_lock=False))
    request = LockRequestV1(session_id=session.session_id, action_id=f"owner-test-{int(time.time())}", expiry=int(time.time() * 1000) + 30000, reason="owner's manual lock test", owner_policy_hash=session.owner_policy_hash)
    t0 = time.monotonic()
    result = adapter.request(request, session, owner_initiated=True)
    end = time.monotonic() + 8
    while time.monotonic() < end and result.status not in (LockStatus.LOCK_CONFIRMED, LockStatus.LOCK_FAILED, LockStatus.LOCK_REFUSED):
        time.sleep(0.05)
    confirmed_after = round((time.monotonic() - t0) * 1000)
    print(f"\nresult: {result.status.value} ({result.detail})")
    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "mechanism": args.mechanism, "password_setting": setting, "accessibility": trusted, "statuses": events, "result": result.to_dict()}
    if result.status in (LockStatus.LOCK_CONFIRMED, LockStatus.LOCK_EXECUTED):
        print("Unlock the Mac normally now. This waits up to 10 minutes.")
        waited = time.monotonic()
        while time.monotonic() - waited < 600 and adapter.is_locked() is not False:
            time.sleep(0.5)
        record["unlocked_after_s"] = round(time.monotonic() - waited, 1) if adapter.is_locked() is False else None
        answer = input("Did unlocking ask for your password? (yes/no): ").strip().lower()
        record["password_asked"] = answer.startswith("y")
        record["confirm_ms"] = confirmed_after
        print(f"recovered after {record['unlocked_after_s']} s; password asked: {record['password_asked']}")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-lock-test.json"
    path.write_text(json.dumps(record, indent=1, default=str))
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
