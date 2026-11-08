"""The desk camera inside the security service: on only while armed, with its own device id.

The camera publishes from ``cam-...`` (data/airlock_vision/), not from the Mac's id, because what a device
may report is tied to the device. Its events come back to ``SecurityService.on_camera_event`` on the event
loop. If the camera or its models fail, the Mac's USB and app monitoring carry on untouched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .contracts import SecurityEventV1
from .identity import DeviceIdentity, load_identity
from .publisher import LocalOnlyTransport, SecurityEventPublisher, Transport

log = logging.getLogger(__name__)


class CameraWatch:
    def __init__(self, settings, *, on_event: Callable[[SecurityEventV1], None], session: Callable[[], object], transport: Transport | None = None,
                 vision_factory: Callable[[CameraWatch], object] | None = None, transport_factory: Callable[[], Transport | None] | None = None):
        self.settings = settings
        self.dir = settings.data_dir / "airlock_vision"
        self.on_event = on_event
        self.session = session
        self.transport = transport or LocalOnlyTransport()
        self._transport_factory = transport_factory  # called once, inside start(), off the event loop
        self._vision_factory = vision_factory
        self.identity: DeviceIdentity | None = None
        self.publisher: SecurityEventPublisher | None = None
        self.vision = None
        self.error: str | None = None
        self.started_at: float | None = None
        self._lock = threading.Lock()
        # "production": the owner template in data/airlock/owner_face.json, if any. "demo": only the demo session's own
        # template (None until enrolled), so a demo never silently reuses an older owner.
        self.template_source = "production"
        self.demo_template = None
        self.status_provider = None

    @staticmethod
    def spacetime_transport(settings, mac_settings: dict | None, directory):
        """The camera's own connection to Sid's module, once the owner has enrolled it and turned it on in
        data/airlock_vision/spacetime.json ({"enabled": true}). Until then its events stay on this Mac."""
        import json

        try:
            stored = json.loads((directory / "spacetime.json").read_text())
        except (OSError, ValueError):
            return None
        if not stored.get("enabled") or not mac_settings:
            return None
        from .spacetime_adapter import SpacetimeConfig, SpacetimeTransport, load_or_create_token

        url = mac_settings["url"].rstrip("/")
        device = load_identity(directory, prefix="cam").device_id
        config = SpacetimeConfig(url=url, database=mac_settings["database"], workstation_id=mac_settings["workstation_id"], device_id=device,
                                 token=load_or_create_token(url, directory), camera_kinds=stored.get("camera_kinds") if isinstance(stored.get("camera_kinds"), dict) else None)
        return SpacetimeTransport(config)  # its reducer contract is checked on first send, on the publisher's thread

    def _armed(self) -> tuple[bool, str | None]:
        s = self.session()
        return (s is not None, None if s is None else getattr(s, "session_id", None))

    def _build(self):
        if self._vision_factory is not None:
            return self._vision_factory(self)
        from ..vision.airlock.enrollment import load_template, template_path
        from ..vision.airlock.events import SecurityEventSink
        from ..vision.airlock.faces import FaceStage
        from ..vision.airlock.geometry import ZoneMap
        from ..vision.airlock.interaction import HandStage
        from ..vision.airlock.pipeline import AirlockVision

        s = self.settings
        zones = ZoneMap.load(s.data_dir / "airlock" / "zones.json")
        template = self.demo_template if self.template_source == "demo" else load_template(template_path(s.data_dir))
        faces = FaceStage(s.models_dir, template)
        hands = HandStage(s.models_dir, zones) if zones.calibrated and len(zones.zones) > 1 else None
        sink = SecurityEventSink(self.publisher, armed=self._armed, on_event=lambda event, _ev: self.on_event(event))
        vision = AirlockVision(s.models_dir, sink, zones=zones, camera_label=s.airlock_vision_label, faces=faces, hands=hands)
        vision.status_provider = self.status_provider
        return vision

    def use_template(self, template, source: str) -> None:
        """Switch the owner template the camera compares against: the demo's own, or the production one."""
        from ..vision.airlock.enrollment import load_template, template_path

        with self._lock:
            self.template_source = source
            self.demo_template = template if source == "demo" else None
            if self.vision is not None and getattr(self.vision, "faces", None) is not None:
                self.vision.faces.set_template(template if source == "demo" else load_template(template_path(self.settings.data_dir)))

    def start_enrollment(self, enroller, on_enrolled) -> None:
        if self.vision is None:
            raise RuntimeError("the camera is not running")
        self.vision.enroll_progress = enroller.progress(None)
        self.vision.on_enrolled = on_enrolled
        self.vision.enroller = enroller

    def reobserve(self) -> None:
        """Forget what the trackers had settled, so the next frames confirm presence and faces again as new observations.
        Used when a session arms while the camera is already on: nothing is invented, the camera just looks again."""
        if self.vision is not None:
            self.vision.reobserve()  # done on the camera's own thread at its next frame

    def stop_enrollment(self) -> None:
        if self.vision is not None:
            self.vision.enroller = None
            self.vision.enroll_progress = None

    def start(self) -> None:
        """Blocking (models load, the camera opens): run it on a worker thread."""
        with self._lock:
            from .device_lock import camera_lock

            if getattr(self, "_device_lock", None) is None:
                self._device_lock = camera_lock(self.settings.data_dir)
            self._device_lock.acquire()  # raises DeviceBusy if a script or another backend is using the camera
            try:
                if self._transport_factory is not None and isinstance(self.transport, LocalOnlyTransport):
                    factory, self._transport_factory = self._transport_factory, None
                    try:
                        self.transport = factory() or self.transport
                    except Exception as exc:  # the camera still runs; its events stay on this Mac
                        log.warning("desk camera: no database connection: %s", exc)
                if self.publisher is None:
                    self.identity = load_identity(self.dir, prefix="cam")
                    self.publisher = SecurityEventPublisher(self.identity.device_id, self.transport, directory=self.dir)
                if self.vision is None:
                    self.vision = self._build()
                self.vision.start(self.settings.airlock_vision_source)
                self.started_at, self.error = time.time(), None
            except Exception as exc:
                log.exception("desk camera did not start")
                self.error = f"{type(exc).__name__}: {exc}"[:240]
                self._device_lock.release()
                raise

    def stop(self) -> None:
        with self._lock:
            if self.vision is not None:
                self.vision.stop()  # models stay loaded for the next arm
            self.started_at = None
            if getattr(self, "_device_lock", None) is not None:
                self._device_lock.release()

    def close(self) -> None:
        with self._lock:
            if self.vision is not None:
                self.vision.close()
                self.vision = None
            if getattr(self, "_device_lock", None) is not None:
                self._device_lock.release()
            if self.publisher is not None:
                self.publisher.close()
                self.publisher = None

    @property
    def running(self) -> bool:
        return self.vision is not None and self.vision.running

    def snapshot(self) -> dict:
        snap = None
        if self.vision is not None:
            try:
                snap = self.vision.snapshot()
            except Exception:
                snap = None
        return {"device_id": None if self.identity is None else self.identity.device_id, "running": self.running, "error": self.error,
                "started_at": self.started_at, "source": self.settings.airlock_vision_source, "label": self.settings.airlock_vision_label, "pipeline": snap}
