"""Prints what the real Mac monitors see, as typed security events, while a temporary local session is armed. Reads the I/O Registry and
NSWorkspace only: it does not touch a USB device's contents, lock anything, speak or send anything off this Mac. Logs go to a temporary
folder unless --data-dir is given.

    .venv/bin/python scripts/airlock_watch.py                     # until Ctrl-C: plug in a harmless device, open an app
    .venv/bin/python scripts/airlock_watch.py --seconds 60
    .venv/bin/python scripts/airlock_watch.py --launch-test-app   # opens Calculator hidden, checks the event, closes it
    .venv/bin/python scripts/airlock_watch.py --poll              # the labelled fallbacks instead of the native watchers
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security.contracts import SecurityEventV1, SecurityKind  # noqa: E402
from app.security.identity import load_identity  # noqa: E402
from app.security.mac_apps import create_app_watcher  # noqa: E402
from app.security.mac_usb import create_usb_watcher  # noqa: E402
from app.security.monitor import Observation, SecurityMonitor  # noqa: E402
from app.security.publisher import LocalOnlyTransport, SecurityEventPublisher  # noqa: E402
from app.security.session import ResponsePolicy, new_local_session  # noqa: E402

TEST_APP = "com.apple.calculator"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--launch-test-app", action="store_true")
    ap.add_argument("--poll", action="store_true", help="use the polling fallbacks")
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="locki-airlock-") as tmp:
        directory = args.data_dir or Path(tmp)
        identity = load_identity(directory)
        publisher = SecurityEventPublisher(identity.device_id, LocalOnlyTransport(), directory=directory)
        state: dict = {"session": None}
        seen: list[Observation] = []
        done = threading.Event()

        def on_observation(o: Observation) -> None:
            seen.append(o)
            if o.event is None:
                return
            SecurityEventV1.model_validate(o.event.model_dump())
            ms = round((time.monotonic() - o.observed_mono) * 1000, 1)
            print(json.dumps({"attention": o.needs_attention, "reason": o.reason, "report_to_here_ms": ms, **o.event.model_dump(mode="json")}), flush=True)
            if o.kind == SecurityKind.APP_LAUNCHED and o.event.metadata.get("bundle_id") == TEST_APP:
                done.set()

        monitor = SecurityMonitor(
            identity, publisher, session=lambda: state["session"], on_observation=on_observation,
            usb_watcher=create_usb_watcher("poll" if args.poll else "iokit"), app_watcher=create_app_watcher("snapshot" if args.poll else "nsworkspace"),
        )
        monitor.start()
        snap = monitor.snapshot()
        print(f"USB: {snap['usb']['method']}, {snap['usb']['connected']} connected. Apps: {snap['apps']['method']} ({snap['apps']['coverage']}).", flush=True)
        for device in monitor.connected.values():
            print(f"  baseline: {device.vendor_id:04x}:{device.product_id:04x} {device.vendor_name or '-'} / {device.product_name or '-'} [{device.category}]", flush=True)
        state["session"] = new_local_session(monitor.fingerprints(), ResponsePolicy())
        print(f"armed a temporary local session, {state['session'].session_id}: nothing is locked, spoken or sent.", flush=True)

        code = 0
        try:
            if args.launch_test_app:
                code = launch_test(done, seen)
            elif args.seconds is not None:
                time.sleep(args.seconds)
            else:
                print("watching. Ctrl-C to stop.", flush=True)
                signal.pause()
        except KeyboardInterrupt:
            pass
        monitor.stop()
        publisher.close()
        snap = monitor.snapshot()
        print(f"events published: {publisher.published}, deduplicated: {snap['deduplicated']}, last report to published: {snap['last_publish_ms']} ms, last launch to notice: {snap['apps']['last_notice_ms']} ms")
        print(f"delivery: {publisher.snapshot()['last_delivery']}")
    return code


def launch_test(done: threading.Event, seen: list[Observation]) -> int:
    running = subprocess.run(["/usr/bin/pgrep", "-x", "Calculator"], capture_output=True, check=False).returncode == 0
    if running:
        print("Calculator is already open, so it cannot be used for this test: close it and run this again.")
        return 2
    t0 = time.monotonic()
    subprocess.run(["/usr/bin/open", "-g", "-j", "-b", TEST_APP], check=True)
    ok = done.wait(10.0)
    print(f"launch event {'seen' if ok else 'NOT seen'}, {round((time.monotonic() - t0) * 1000)} ms after running open", flush=True)
    pid = next((o.event.metadata.get("pid") for o in seen if o.event is not None and o.event.metadata.get("bundle_id") == TEST_APP), None)
    if pid:
        os.kill(int(pid), signal.SIGTERM)  # the one this test opened
    else:
        subprocess.run(["/usr/bin/pkill", "-x", "Calculator"], check=False)
    return 0 if ok else 1



if __name__ == "__main__":
    from app.security.device_lock import DeviceBusy, usb_monitor_lock

    try:
        with usb_monitor_lock(Path(__file__).resolve().parents[2] / "data"):
            sys.exit(main())
    except DeviceBusy as exc:
        print(exc)
        sys.exit(3)
