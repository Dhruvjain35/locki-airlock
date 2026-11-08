"""Turns what the OS reports into typed security events. It decides nothing about what an event means: an unfamiliar device is
"unknown", a watchlisted app is a match. While disarmed, observations are counted only and nothing is published."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..missions.workspace import redact
from .contracts import SecurityEventV1, SecurityKind, SecuritySource
from .identity import DeviceIdentity
from .mac_apps import AppLaunch, AppWatcher, CodeSignature, Watchlist, code_signature, create_app_watcher
from .mac_usb import UsbChange, UsbDevice, UsbWatcher, create_usb_watcher
from .publisher import Deduper, SecurityEventPublisher
from .session import ArmedSession

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Observation:
    """An event plus what the local response needs. ``needs_attention`` follows the owner's policy; it does not mean malicious."""

    event: SecurityEventV1 | None  # None while disarmed: counted, not published
    kind: SecurityKind
    armed: bool
    needs_attention: bool
    label: str
    reason: str | None
    observed_mono: float


def _hex16(value: int) -> str:
    return f"0x{value & 0xffff:04x}"


def usb_metadata(device: UsbDevice, salt: bytes, status: str, detection: str, session: ArmedSession | None) -> dict:
    return {
        "fingerprint": device.fingerprint(salt),
        "has_serial": bool(device.serial),
        "vendor_id": _hex16(device.vendor_id),
        "product_id": _hex16(device.product_id),
        "vendor_name": device.vendor_name,
        "product_name": device.product_name,
        "device_class": device.device_class,
        "interface_classes": list(device.interface_classes),
        "category": device.category,
        "location_id": None if device.location_id is None else f"0x{device.location_id:08x}",
        "allowlist_status": status,
        "detection": detection,
        "armed": session is not None,
        "session_id": None if session is None else session.session_id,
    }


def app_metadata(launch: AppLaunch, signature: CodeSignature, watch_category: str | None, session: ArmedSession | None) -> dict:
    return {
        "name": launch.name,
        "bundle_id": launch.bundle_id,
        "executable_path": redact(launch.executable_path) if launch.executable_path else None,
        "pid": launch.pid,
        "launched_at": launch.launched_at_ms,
        "signature": signature.to_metadata(),
        "watchlist_match": watch_category is not None,
        "watchlist_category": watch_category,
        "detection": launch.detection,
        "armed": session is not None,
        "session_id": None if session is None else session.session_id,
    }


def device_label(device: UsbDevice) -> str:
    return device.product_name or device.vendor_name or f"USB device {_hex16(device.vendor_id)}:{_hex16(device.product_id)}"


@dataclass
class MonitorStats:
    usb_added: int = 0
    usb_removed: int = 0
    apps_launched: int = 0
    watchlist_matches: int = 0
    last_publish_ms: float | None = None
    last_launch_notice_ms: float | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=50))


class SecurityMonitor:
    def __init__(
        self,
        identity: DeviceIdentity,
        publisher: SecurityEventPublisher,
        *,
        session: Callable[[], ArmedSession | None],
        on_observation: Callable[[Observation], None] | None = None,
        usb_watcher: UsbWatcher | None = None,
        app_watcher: AppWatcher | None = None,
        watchlist: Watchlist | None = None,
        signature: Callable[[str | None], CodeSignature] = code_signature,
        dedupe_window_s: float = 2.0,
    ):
        self.identity = identity
        self.publisher = publisher
        self.session = session
        self.on_observation = on_observation
        self.usb_watcher = usb_watcher if usb_watcher is not None else create_usb_watcher()
        self.app_watcher = app_watcher if app_watcher is not None else create_app_watcher(on_problem=self._apps_problem)
        self.watchlist = watchlist if watchlist is not None else Watchlist()
        self.signature = signature
        self.dedupe = Deduper(dedupe_window_s)
        self.stats = MonitorStats()
        self.connected: dict[int, UsbDevice] = {}
        self.frontmost: dict | None = None
        self.running = False
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._start_watcher("usb", self.usb_watcher, self._on_usb)
        self._start_watcher("apps", self.app_watcher, self._on_app)

    def _start_watcher(self, name: str, watcher, handler) -> None:
        try:
            result = watcher.start(handler)
        except Exception as exc:
            fallback = self._fallback(name)
            self.errors[name] = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("%s watcher %s did not start (%s); using %s", name, watcher.method, exc, fallback.method if fallback else "nothing")
            if fallback is None:
                self._health(name, "degraded", watcher.method, self.errors[name])
                return
            try:
                result = fallback.start(handler)
            except Exception as again:
                self.errors[name] = f"{type(again).__name__}: {again}"[:200]
                self._health(name, "degraded", fallback.method, self.errors[name])
                return
            if name == "usb":
                self.usb_watcher = fallback
            else:
                self.app_watcher = fallback
            watcher = fallback
            self._health(name, "degraded", watcher.method, f"native watcher unavailable: {str(exc)[:120]}")
        else:
            self._health(name, "started", watcher.method, None)
        if name == "usb":
            with self._lock:
                self.connected = {d.registry_id: d for d in result}
        else:
            self.frontmost = result

    def _fallback(self, name: str):
        from .mac_apps import ProcessSnapshotWatcher
        from .mac_usb import PollingUsbWatcher

        if name == "usb" and not isinstance(self.usb_watcher, PollingUsbWatcher):
            return PollingUsbWatcher()
        if name == "apps" and not isinstance(self.app_watcher, ProcessSnapshotWatcher):
            return ProcessSnapshotWatcher()
        return None

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        for name, watcher in (("usb", self.usb_watcher), ("apps", self.app_watcher)):
            try:
                watcher.stop()
                self._health(name, "stopped", watcher.method, None)
            except Exception:
                log.exception("stopping the %s watcher failed", name)

    def _apps_problem(self, detail: str) -> None:
        if not self.running:
            return
        self.errors["apps"] = detail[:200]
        self._health("apps", "degraded", self.app_watcher.method, detail)
        fallback = self._fallback("apps")
        if fallback is not None:
            try:
                fallback.start(self._on_app)
                self.app_watcher = fallback
                self._health("apps", "recovered", fallback.method, "fell back to a process list; it can miss short-lived processes")
            except Exception as exc:
                self.errors["apps"] = f"{type(exc).__name__}: {exc}"[:200]

    def _health(self, monitor: str, status: str, method: str | None, detail: str | None) -> None:
        try:
            self.publisher.publish(self.publisher.build(SecuritySource.MAC_AGENT, SecurityKind.MONITOR_HEALTH, {"monitor": monitor, "status": status, "method": method, "detail": detail}))
        except Exception:
            log.exception("recording monitor health failed")

    def fingerprints(self) -> dict[str, int]:
        """What is connected now, as a baseline: each fingerprint and how many have it."""
        with self._lock:
            return dict(Counter(d.fingerprint(self.identity.salt) for d in self.connected.values()))

    # ------------------------------------------------------------------ observations
    def _active_session(self) -> ArmedSession | None:
        session = self.session()
        return None if session is None or session.expired() else session

    def _on_usb(self, change: UsbChange) -> None:
        device = change.device
        with self._lock:
            if change.kind == "added":
                self.connected[device.registry_id] = device
            else:
                self.connected.pop(device.registry_id, None)
        if change.kind == "added":
            self.stats.usb_added += 1
        else:
            self.stats.usb_removed += 1
        fingerprint = device.fingerprint(self.identity.salt)
        if self.dedupe.repeated(f"usb:{change.kind}:{fingerprint}:{device.location_id}"):  # a device that re-enumerates as it settles
            return
        kind = SecurityKind.USB_DEVICE_ADDED if change.kind == "added" else SecurityKind.USB_DEVICE_REMOVED
        session = self._active_session()
        if session is None:
            self._deliver(Observation(None, kind, False, False, device_label(device), None, change.observed_mono))
            return
        with self._lock:
            connected_like_it = sum(1 for d in self.connected.values() if d.fingerprint(self.identity.salt) == fingerprint)
        status = session.classify_usb(fingerprint, max(1, connected_like_it))
        event = self.publisher.build(SecuritySource.MAC_USB, kind, usb_metadata(device, self.identity.salt, status, change.detection, session), observed_at_ms=change.observed_at_ms)
        attention = change.kind == "added" and status == "unknown" and session.policy.warn_on_unknown_usb
        self._publish_and_deliver(event, kind, attention, device_label(device), "unknown_usb" if attention else None, change.observed_mono)

    def _on_app(self, launch: AppLaunch) -> None:
        self.stats.apps_launched += 1
        if self.dedupe.repeated(f"app:{launch.pid}:{launch.bundle_id or launch.executable_path}"):
            return
        session = self._active_session()
        if session is None:
            self._deliver(Observation(None, SecurityKind.APP_LAUNCHED, False, False, launch.name or "an application", None, launch.observed_mono))
            return
        self.watchlist.approved = set(session.policy.approved_apps)
        match = self.watchlist.match(launch.bundle_id, launch.name)
        if match is not None:
            self.stats.watchlist_matches += 1
        if launch.launched_at_ms is not None:
            self.stats.last_launch_notice_ms = float(launch.observed_at_ms - launch.launched_at_ms)
        signature = self.signature(launch.bundle_path or launch.executable_path)
        event = self.publisher.build(SecuritySource.MAC_APP, SecurityKind.APP_LAUNCHED, app_metadata(launch, signature, None if match is None else match.category, session), observed_at_ms=launch.observed_at_ms)
        attention = match is not None and session.policy.warn_on_watchlist_app
        self._publish_and_deliver(event, SecurityKind.APP_LAUNCHED, attention, launch.name or launch.bundle_id or "an application", "watchlist_app" if attention else None, launch.observed_mono)

    def _publish_and_deliver(self, event: SecurityEventV1, kind: SecurityKind, attention: bool, label: str, reason: str | None, mono: float) -> None:
        self.publisher.publish(event)
        self.stats.last_publish_ms = round((time.monotonic() - mono) * 1000, 2)
        self.stats.recent.append({"event_id": event.event_id, "seq": event.seq, "kind": kind.value, "observed_at": event.observed_at, "needs_attention": attention, "label": label})
        self._deliver(Observation(event, kind, True, attention, label, reason, mono))

    def _deliver(self, observation: Observation) -> None:
        if self.on_observation is None:
            return
        try:
            self.on_observation(observation)
        except Exception:
            log.exception("responding to a security observation failed")

    def snapshot(self) -> dict:
        s = self.stats
        with self._lock:
            connected = len(self.connected)
        return {
            "running": self.running,
            "usb": {"method": self.usb_watcher.method, "connected": connected, "added": s.usb_added, "removed": s.usb_removed, "error": self.errors.get("usb")},
            "apps": {"method": self.app_watcher.method, "launched": s.apps_launched, "watchlist_matches": s.watchlist_matches, "last_notice_ms": s.last_launch_notice_ms, "error": self.errors.get("apps"),
                     "coverage": "visible applications only" if self.app_watcher.method == "nsworkspace_notification" else "a process list; it can miss short-lived processes"},
            "deduplicated": self.dedupe.suppressed,
            "last_publish_ms": s.last_publish_ms,
            "recent": list(s.recent),
        }
