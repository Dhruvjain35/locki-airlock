"""From OS report to typed event: armed versus disarmed, allowlists, the watchlist, deduplication, fallbacks, and that nothing is called malicious."""

from __future__ import annotations

import json
import time

from app.security.contracts import SecurityEventV1, SecurityKind
from app.security.identity import DeviceIdentity, load_identity
from app.security.mac_apps import AppLaunch, CodeSignature
from app.security.mac_usb import UsbChange, UsbDevice
from app.security.monitor import SecurityMonitor
from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.security.session import ResponsePolicy, new_local_session

IDENT = DeviceIdentity("mac-test", b"salt")
KNOWN = UsbDevice(1, 0x05e3, 0x0608, product_name="USB2.0 Hub", device_class=9, interface_classes=(9,), location_id=0x100000)
STICK = UsbDevice(2, 0x0781, 0x5581, "SanDisk", "Ultra", serial="SECRET123", device_class=0, interface_classes=(8,), location_id=0x130000)


class FakeUsb:
    method = "iokit_notification"

    def __init__(self, devices=(), fail=False):
        self.devices, self.fail, self.stopped = list(devices), fail, False

    def start(self, on_change):
        if self.fail:
            raise OSError("IOKit is not available")
        self.on_change = on_change
        return self.devices

    def stop(self):
        self.stopped = True

    def plug(self, device, kind="added"):
        self.on_change(UsbChange(kind, device, int(time.time() * 1000), time.monotonic(), "iokit_notification"))


class FakeApps:
    method = "nsworkspace_notification"

    def __init__(self):
        self.stopped = False

    def start(self, on_launch):
        self.on_launch = on_launch
        return {"name": "Chrome"}

    def stop(self):
        self.stopped = True

    def launch(self, bundle_id="com.apple.calculator", name="Calculator", pid=99, path="/System/Applications/Calculator.app/Contents/MacOS/Calculator"):
        now = int(time.time() * 1000)
        self.on_launch(AppLaunch(name, bundle_id, path, path, pid, now - 40, now, time.monotonic(), "nsworkspace_notification"))


def build(tmp_path, policy=None, armed=True, devices=(KNOWN,)):
    transport = FixtureTransport()
    publisher = SecurityEventPublisher(IDENT.device_id, transport, directory=tmp_path, is_fixture=True)
    usb, apps = FakeUsb(devices), FakeApps()
    state = {"session": None}
    seen = []
    monitor = SecurityMonitor(IDENT, publisher, session=lambda: state["session"], on_observation=seen.append, usb_watcher=usb, app_watcher=apps, signature=lambda p: CodeSignature("signed", "com.apple.calculator", None, "Software Signing"), dedupe_window_s=0.2)
    monitor.start()
    if armed:
        state["session"] = new_local_session(monitor.fingerprints(), policy or ResponsePolicy())
    return monitor, publisher, transport, usb, apps, seen, state


def security_events(publisher, transport):
    publisher.flush()
    return [e for e in transport.events if e.kind != SecurityKind.MONITOR_HEALTH]


def test_an_unknown_usb_while_armed_is_a_typed_event_that_needs_attention(tmp_path):
    monitor, publisher, transport, usb, _, seen, state = build(tmp_path)
    usb.plug(STICK)
    event = security_events(publisher, transport)[0]
    SecurityEventV1.model_validate(event.model_dump())
    m = event.metadata
    assert event.kind == SecurityKind.USB_DEVICE_ADDED and event.source == "mac_usb" and event.confidence is None and event.device_id == "mac-test"
    assert m["allowlist_status"] == "unknown" and m["category"] == "storage" and m["vendor_id"] == "0x0781" and m["product_id"] == "0x5581"
    assert m["detection"] == "iokit_notification" and m["armed"] is True and m["session_id"] == state["session"].session_id and m["has_serial"] is True
    assert seen[-1].needs_attention and seen[-1].reason == "unknown_usb" and seen[-1].label == "Ultra"
    monitor.stop()


def test_the_serial_never_appears_in_the_event_the_log_or_the_observation(tmp_path):
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path)
    usb.plug(STICK)
    event = security_events(publisher, transport)[0]
    monitor.stop()
    assert "SECRET123" not in event.model_dump_json() and "SECRET123" not in repr(seen)
    for path in tmp_path.glob("events-*.jsonl"):
        assert "SECRET123" not in path.read_text()


def test_nothing_is_called_malicious(tmp_path):
    monitor, publisher, transport, usb, apps, seen, _ = build(tmp_path)
    usb.plug(STICK)
    apps.launch("com.teamviewer.TeamViewer", "TeamViewer")
    text = " ".join(e.model_dump_json() for e in security_events(publisher, transport)) + repr(seen)
    monitor.stop()
    for word in ("malware", "malicious", "attacker", "hacker", "virus", "compromised", "intruder"):
        assert word not in text.lower()


def test_a_device_from_the_baseline_coming_back_is_not_an_alert(tmp_path):
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path)
    usb.plug(KNOWN, "removed")
    time.sleep(0.25)
    usb.plug(KNOWN)
    events = security_events(publisher, transport)
    assert [e.metadata["allowlist_status"] for e in events] == ["baseline", "baseline"]
    assert not any(o.needs_attention for o in seen)
    monitor.stop()


def test_an_approved_device_is_informational(tmp_path):
    policy = ResponsePolicy(approved_devices=[STICK.fingerprint(IDENT.salt)])
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path, policy)
    usb.plug(STICK)
    assert security_events(publisher, transport)[0].metadata["allowlist_status"] == "allowlisted"
    assert not seen[-1].needs_attention
    monitor.stop()


def test_a_removal_is_recorded_without_an_alert(tmp_path):
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path)
    usb.plug(STICK, "removed")
    assert security_events(publisher, transport)[0].kind == SecurityKind.USB_DEVICE_REMOVED and not seen[-1].needs_attention
    monitor.stop()


def test_while_disarmed_nothing_is_published_or_flagged(tmp_path):
    monitor, publisher, transport, usb, apps, seen, _ = build(tmp_path, armed=False)
    usb.plug(STICK)
    apps.launch()
    assert security_events(publisher, transport) == []
    assert len(seen) == 2 and all(o.event is None and not o.needs_attention and not o.armed for o in seen)
    assert monitor.stats.usb_added == 1 and monitor.stats.apps_launched == 1
    log = "".join(p.read_text() for p in tmp_path.glob("events-*.jsonl"))
    assert "Ultra" not in log and "Calculator" not in log, "what a disarmed Mac does is not recorded"
    monitor.stop()


def test_an_expired_session_counts_as_disarmed(tmp_path):
    monitor, publisher, transport, usb, _, seen, state = build(tmp_path)
    state["session"] = new_local_session({}, ResponsePolicy(), duration_s=0, now_ms=0)
    usb.plug(STICK)
    assert security_events(publisher, transport) == [] and not seen[-1].armed
    monitor.stop()


def test_a_repeated_report_counts_once_until_the_window_passes(tmp_path):
    monitor, publisher, transport, usb, _, _, _ = build(tmp_path)
    usb.plug(STICK)
    usb.plug(STICK)
    usb.plug(STICK)
    assert len(security_events(publisher, transport)) == 1 and monitor.dedupe.suppressed == 2
    time.sleep(0.25)
    usb.plug(STICK)
    assert len(security_events(publisher, transport)) == 2
    monitor.stop()


def test_an_app_on_the_watchlist_is_a_match_not_an_accusation(tmp_path):
    monitor, publisher, transport, _, apps, seen, _ = build(tmp_path)
    apps.launch("com.teamviewer.TeamViewer", "TeamViewer")
    m = security_events(publisher, transport)[0].metadata
    assert m["watchlist_match"] is True and m["watchlist_category"] == "remote_access" and m["signature"]["status"] == "signed" and m["detection"] == "nsworkspace_notification"
    assert seen[-1].needs_attention and seen[-1].reason == "watchlist_app"
    monitor.stop()


def test_an_ordinary_app_is_recorded_without_an_alert(tmp_path):
    monitor, publisher, transport, _, apps, seen, _ = build(tmp_path)
    apps.launch("com.apple.Safari", "Safari", path="/Applications/Safari.app/Contents/MacOS/Safari")
    assert security_events(publisher, transport)[0].metadata["watchlist_match"] is False and not seen[-1].needs_attention
    monitor.stop()


def test_an_app_the_owner_approved_is_not_flagged(tmp_path):
    monitor, publisher, transport, _, apps, seen, _ = build(tmp_path, ResponsePolicy(approved_apps=["com.teamviewer.TeamViewer"]))
    apps.launch("com.teamviewer.TeamViewer", "TeamViewer")
    assert security_events(publisher, transport)[0].metadata["watchlist_match"] is False and not seen[-1].needs_attention
    monitor.stop()


def test_the_home_folder_is_shortened_in_paths(tmp_path):
    from pathlib import Path

    monitor, publisher, transport, _, apps, _, _ = build(tmp_path)
    apps.launch("com.example.x", "X", path=f"{Path.home()}/Apps/X.app/Contents/MacOS/X")
    assert security_events(publisher, transport)[0].metadata["executable_path"].startswith("~/Apps/")
    monitor.stop()


def test_the_owner_can_turn_warnings_off_without_stopping_the_record(tmp_path):
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path, ResponsePolicy(warn_on_unknown_usb=False))
    usb.plug(STICK)
    assert len(security_events(publisher, transport)) == 1 and not seen[-1].needs_attention
    monitor.stop()


def test_when_iokit_fails_it_falls_back_and_says_so(tmp_path, monkeypatch):
    from app.security import mac_usb

    monkeypatch.setattr(mac_usb, "ioreg_snapshot", lambda timeout_s=5.0: [KNOWN])
    monkeypatch.setattr(mac_usb.PollingUsbWatcher.__init__, "__defaults__", (0.05, lambda: [KNOWN]))
    transport = FixtureTransport()
    publisher = SecurityEventPublisher(IDENT.device_id, transport, directory=tmp_path, is_fixture=True)
    monitor = SecurityMonitor(IDENT, publisher, session=lambda: None, usb_watcher=FakeUsb(fail=True), app_watcher=FakeApps())
    monitor.start()
    assert monitor.usb_watcher.method == "ioreg_poll" and "IOKit" in monitor.snapshot()["usb"]["error"]
    assert monitor.snapshot()["usb"]["connected"] == 1
    publisher.flush()
    health = [e.metadata for e in transport.events if e.kind == SecurityKind.MONITOR_HEALTH and e.metadata["monitor"] == "usb"]
    assert health[0]["status"] == "degraded" and health[0]["method"] == "ioreg_poll"
    monitor.stop()


def test_a_failing_response_does_not_stop_the_monitor(tmp_path):
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path)
    monitor.on_observation = lambda o: (_ for _ in ()).throw(RuntimeError("the response failed"))
    usb.plug(STICK)
    assert len(security_events(publisher, transport)) == 1
    monitor.stop()


def test_snapshot_reports_real_counts_and_coverage(tmp_path):
    monitor, publisher, _, usb, apps, _, _ = build(tmp_path)
    usb.plug(STICK)
    apps.launch()
    snap = monitor.snapshot()
    monitor.stop()
    assert snap["usb"]["connected"] == 2 and snap["usb"]["added"] == 1 and snap["apps"]["launched"] == 1 and snap["apps"]["coverage"] == "visible applications only"
    assert snap["apps"]["last_notice_ms"] == 40.0
    assert json.dumps(snap)


def test_the_identity_is_random_private_and_stable(tmp_path):
    first = load_identity(tmp_path / "s")
    assert first == load_identity(tmp_path / "s") and first.device_id.startswith("mac-")
    assert (tmp_path / "s" / "identity.json").stat().st_mode & 0o077 == 0
    assert first != load_identity(tmp_path / "other")


def test_one_more_than_the_baseline_held_is_new_even_if_it_looks_identical(tmp_path):
    hub = UsbDevice(10, 0x05e3, 0x0608, product_name="USB2.0 Hub", location_id=0x100000)
    same_again = UsbDevice(11, 0x05e3, 0x0608, product_name="USB2.0 Hub", location_id=0x120000)
    monitor, publisher, transport, usb, _, seen, state = build(tmp_path, devices=(hub,))
    assert state["session"].baseline == {hub.fingerprint(IDENT.salt): 1}
    usb.plug(same_again)
    assert security_events(publisher, transport)[0].metadata["allowlist_status"] == "unknown" and seen[-1].needs_attention
    monitor.stop()


def test_a_baseline_device_replugged_is_still_baseline(tmp_path):
    hub = UsbDevice(10, 0x05e3, 0x0608, product_name="USB2.0 Hub", location_id=0x100000)
    replug = UsbDevice(12, 0x05e3, 0x0608, product_name="USB2.0 Hub", location_id=0x100000)
    monitor, publisher, transport, usb, _, seen, _ = build(tmp_path, devices=(hub,))
    usb.plug(hub, "removed")
    time.sleep(0.25)
    usb.plug(replug)
    assert security_events(publisher, transport)[-1].metadata["allowlist_status"] == "baseline" and not seen[-1].needs_attention
    monitor.stop()
