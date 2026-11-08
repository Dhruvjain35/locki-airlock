"""The USB and app watchers: how OS output is read, the fallbacks, and (on a Mac) the real IOKit and NSWorkspace calls. A real device being
plugged in or a real app opening is checked by scripts/airlock_watch.py, not here."""

from __future__ import annotations

import ast
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.security import mac_apps, mac_usb
from app.security.mac_apps import (
    NSWorkspaceAppWatcher,
    ProcessSnapshotWatcher,
    Watchlist,
    bundle_from_path,
    code_signature,
    launch_from_helper,
    parse_codesign,
)
from app.security.mac_usb import (
    IOKitUsbWatcher,
    PollingUsbWatcher,
    UsbDevice,
    devices_from_ioreg,
    iokit_snapshot,
    ioreg_snapshot,
)

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS")
SECURITY_DIR = Path(mac_usb.__file__).parent


def sample_ioreg() -> bytes:
    hub_child = {
        "IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": 22, "idVendor": 0x0781, "idProduct": 0x5581, "kUSBVendorString": "SanDisk", "kUSBProductString": "Ultra", "kUSBSerialNumberString": "SECRET123",
        "bDeviceClass": 0, "bDeviceSubClass": 0, "bDeviceProtocol": 0, "locationID": 0x130000,
        "IORegistryEntryChildren": [{"IOObjectClass": "IOUSBHostInterface", "idVendor": 0x0781, "idProduct": 0x5581, "bInterfaceClass": 8}, {"IOObjectClass": "AppleUSBHostCompositeDevice", "idVendor": 0x0781, "idProduct": 0x5581}],
    }
    hub = {"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": 11, "idVendor": 0x05e3, "idProduct": 0x0608, "USB Product Name": "USB2.0 Hub", "bDeviceClass": 9, "locationID": 0x100000,
           "IORegistryEntryChildren": [{"IOObjectClass": "IOUSBHostInterface", "idVendor": 0x05e3, "idProduct": 0x0608, "bInterfaceClass": 9}, {"IOObjectClass": "AppleUSB20Hub", "IORegistryEntryChildren": [hub_child]}]}
    return plistlib.dumps([hub])


def test_ioreg_output_gives_devices_behind_hubs_and_not_their_drivers():
    devices = devices_from_ioreg(sample_ioreg())
    assert [d.registry_id for d in devices] == [11, 22]
    stick = devices[1]
    assert (stick.vendor_id, stick.product_id, stick.vendor_name, stick.product_name) == (0x0781, 0x5581, "SanDisk", "Ultra")
    assert stick.interface_classes == (8,) and stick.category == "storage" and devices[0].category == "hub"


def test_no_devices_is_an_empty_list():
    assert devices_from_ioreg(b"") == [] and devices_from_ioreg(b"  \n") == []


def test_a_serial_stays_out_of_logs_and_only_its_hash_is_used():
    stick = devices_from_ioreg(sample_ioreg())[1]
    assert "SECRET123" not in repr(stick)
    assert "SECRET123" not in stick.fingerprint(b"salt") and len(stick.fingerprint(b"salt")) == 32
    assert stick.fingerprint(b"salt") != stick.fingerprint(b"other"), "salted, so it cannot be looked up elsewhere"


@pytest.mark.parametrize(
    "classes, device_class, category",
    [((8,), 0, "storage"), ((3,), 0, "input"), ((3, 8), 0, "input"), ((1,), 0, "audio"), ((14,), 239, "video"), ((), 9, "hub"), ((255,), 255, "vendor_specific"), ((), None, "unknown"), ((6, 255), 0, "phone_or_camera")],
)
def test_a_device_is_classified_from_what_it_reports(classes, device_class, category):
    assert UsbDevice(1, 1, 1, device_class=device_class, interface_classes=classes).category == category


def test_a_device_that_is_also_a_keyboard_reads_as_input():
    assert UsbDevice(1, 1, 1, interface_classes=(3, 8)).category == "input"


def test_polling_reports_arrivals_and_removals_and_survives_a_failed_read():
    d1, d2 = UsbDevice(1, 1, 1), UsbDevice(2, 2, 2)
    frames = iter([[d1], RuntimeError("ioreg hiccup"), [d1, d2], [d2], [d2], [d2]])

    def snap():
        try:
            frame = next(frames)
        except StopIteration:
            return [d2]
        if isinstance(frame, Exception):
            raise frame
        return frame

    changes = []
    w = PollingUsbWatcher(interval_s=0.01, snapshot=snap)
    assert w.start(changes.append) == [d1]
    end = time.monotonic() + 2
    while len(changes) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    w.stop()
    assert [(c.kind, c.device.registry_id, c.detection) for c in changes] == [("added", 2, "ioreg_poll"), ("removed", 1, "ioreg_poll")]


SIGNED = """Executable=/Applications/X.app/Contents/MacOS/X
Identifier=com.example.x
Format=app bundle with Mach-O thin (arm64)
Authority=Developer ID Application: Example (ABC123)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
TeamIdentifier=ABC123
"""


def test_a_code_signature_is_read_without_overclaiming():
    s = parse_codesign(SIGNED, 0)
    assert (s.status, s.identifier, s.team_id, s.authority) == ("signed", "com.example.x", "ABC123", "Developer ID Application: Example (ABC123)")
    assert parse_codesign("/tmp/x: code object is not signed at all\n", 1).status == "unsigned"
    assert parse_codesign("Identifier=a.out\nSignature=adhoc\nTeamIdentifier=not set\n", 0).status == "adhoc"
    assert parse_codesign("Identifier=a.out\nSignature=adhoc\nTeamIdentifier=not set\n", 0).team_id is None
    assert parse_codesign("some other failure", 1).status == "unknown"
    assert code_signature(None).status == "unknown" and code_signature("relative/path").status == "unknown"


def test_the_watchlist_matches_examples_and_respects_owner_approval():
    w = Watchlist()
    assert w.match("com.teamviewer.TeamViewer").category == "remote_access"
    assert w.match("com.apple.calculator").category == "demo_test_app"
    assert w.match("com.apple.Safari") is None and w.match(None, None) is None
    w.approved = {"com.teamviewer.TeamViewer"}
    assert w.match("com.teamviewer.TeamViewer") is None


def test_the_helper_message_becomes_a_typed_launch():
    launch = launch_from_helper({"name": "X", "bundle_id": "com.x", "executable_path": "/a/X", "bundle_path": "/a", "pid": 7, "launched_at": 100.5, "observed_at": 101.0}, 5.0)
    assert (launch.launched_at_ms, launch.observed_at_ms, launch.pid, launch.detection) == (100500, 101000, 7, "nsworkspace_notification")


def test_the_process_list_fallback_only_reports_new_apps_and_labels_itself(tmp_path):
    app = tmp_path / "Thing.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.thing"}))
    exe = str(app / "MacOS" / "Thing")
    frames = iter([{1: ("old", "/bin/old", 1.0)}, {1: ("old", "/bin/old", 1.0), 2: ("daemon", "/usr/sbin/d", 2.0), 3: ("Thing", exe, 3.0)}])

    def listing():
        return next(frames, {1: ("old", "/bin/old", 1.0)})

    got = []
    w = ProcessSnapshotWatcher(interval_s=0.01, list_processes=listing)
    w.start(got.append)
    end = time.monotonic() + 2
    while not got and time.monotonic() < end:
        time.sleep(0.01)
    w.stop()
    assert [(g.pid, g.bundle_id, g.detection) for g in got] == [(3, "com.example.thing", "process_snapshot")]
    assert bundle_from_path("/usr/bin/x") == (None, None)


def test_nothing_in_the_security_package_touches_a_devices_contents():
    forbidden = ("/Volumes", "diskutil", "hdiutil", "mount ", "os.listdir", "os.walk", "scandir", "IOMedia")
    for path in SECURITY_DIR.glob("*.py"):
        text = path.read_text()
        for word in forbidden:
            assert word not in text, f"{path.name} mentions {word}"


def test_nothing_in_the_security_package_can_move_the_robot_or_touch_firmware():
    for path in SECURITY_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in modules if "robot" in m or "safety" in m or "zeus" in m}, path.name
        text = path.read_text().lower()
        for word in ("motor", "zeus", "avrdude", "rt.transport", "safety.", "request_motion"):
            assert word not in text, f"{path.name} mentions {word}"


# ------------------------------------------------------------------ real OS calls, read only
@mac_only
def test_real_iokit_and_ioreg_see_the_same_devices():
    salt = b"t"
    assert sorted(d.fingerprint(salt) for d in iokit_snapshot()) == sorted(d.fingerprint(salt) for d in ioreg_snapshot())


@mac_only
def test_real_iokit_watcher_starts_with_the_current_devices_and_stops_cleanly():
    w = IOKitUsbWatcher()
    changes = []
    baseline = w.start(changes.append)
    assert sorted(d.registry_id for d in baseline) == sorted(d.registry_id for d in iokit_snapshot())
    time.sleep(0.05)
    w.stop()
    assert not w._thread.is_alive() and not w._worker.is_alive()


@mac_only
def test_real_helper_starts_and_exits_when_its_parent_closes_it():
    w = NSWorkspaceAppWatcher()
    w.start(lambda launch: None)
    proc = w._proc
    assert proc.poll() is None
    w.stop()
    assert proc.poll() is not None


@mac_only
def test_real_helper_runs_isolated_with_only_the_standard_library():
    text = mac_apps.HELPER.read_text()
    assert "from app" not in text and "import app" not in text
    out = subprocess.run([sys.executable, "-I", "-c", "import ctypes, json, os, sys, threading, time"], capture_output=True, check=False)
    assert out.returncode == 0


@mac_only
def test_real_code_signature_of_a_system_app():
    s = code_signature("/System/Applications/Calculator.app")
    assert s.status == "signed" and s.identifier == "com.apple.calculator"
