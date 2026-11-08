"""The AIRLOCK response on this Mac: real observations go in; a warning, a record and (if the owner opted in) a screen lock come out.

The warning path uses no model and no network, so it works with both gone. When SpacetimeDB is connected it owns arming and incident
state, and this service follows it. Until then the owner arms here, and everything says source="local".
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING

from ..telemetry.events import EventSource, EventType
from ..voice.tts import PRIORITY_REPLY
from .contracts import (
    AgentStepV1,
    IncidentState,
    IncidentUpdateV1,
    LockRequestV1,
    LockStatus,
    SecurityEventV1,
    SecurityKind,
    SecuritySource,
)
from .identity import DeviceIdentity, load_identity
from .mac_lock import LockAdapter, LockResult
from .modes import ModeEngine, SecurityMode, VisualState
from .monitor import Observation, SecurityMonitor
from .publisher import LocalOnlyTransport, SecurityEventPublisher, Transport
from .session import ArmedSession, ResponsePolicy, new_local_session

if TYPE_CHECKING:
    from ..runtime import LockedInRuntime

log = logging.getLogger(__name__)

MIN_CODE_LENGTH = 4
CODE_ATTEMPTS = 5
CODE_LOCKOUT_S = 60.0
BASELINE_MIN_GAP_S = 3.0
BASELINE_REFRESH_S = 10.0  # arm_session needs a capture at most 30 s old: refresh well inside that
HEARTBEAT_S = 5.0  # Sid's module marks a component offline after 15 s without one
CAMERA_HEARTBEAT_S = 4.0  # Sid's camera contract asks for every 3-5 s
WARNING_REPEAT_S = 15.0  # the same kind of warning, spoken again this soon, only annoys people
# An incident the server last changed longer ago than this is shown, never spoken: a restart or reconnect re-delivers every row, and the spoken-once set is
# only in memory. 120 s is the module's own window for admitting an observation, and the Mac's clock agreed with maincloud's to within ~0.2 s on 2026-11-07.
INCIDENT_VOICE_MAX_AGE_MS = 120_000
LOCAL_LOCK_TTL_MS = 30_000
MAX_EVIDENCE = 200
# The desk camera. These only choose which true sentence LOCKI says; SpacetimeDB's module decides incidents.
PHYSICAL_WINDOW_S = 20.0  # camera observations this recent give a USB warning its context
PRESENCE_FACE_WAIT_S = 1.5  # someone arrived: wait this long for the owner's face (about 1 s) before the watchful line
CAMERA_DOWN = {"camera_obstructed", "camera_offline", "camera_unavailable"}
INTERACTION_ALERTS = {"keyboard_interaction", "laptop_interaction", "protected_device_interaction"}
PRESENCE_KINDS = {"person_in_zone", "person_left_zone", "zone_clear"}
FACE_KINDS = {"owner_face_match", "unrecognized_face", "face_uncertain", "no_face_visible"}


def _numbers_in_run(run) -> set:
    from ..intelligence.dialogue import numbers_in

    return numbers_in(json.dumps([s.result for s in run.steps], default=str))


def _hash_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class SecurityError(ValueError):
    """A refusal that is safe to show and to say."""


class SecurityService:
    def __init__(self, rt: LockedInRuntime, *, transport: Transport | None = None, monitor: SecurityMonitor | None = None, lock: LockAdapter | None = None):
        self.rt = rt
        settings = rt.settings
        self.enabled = settings.security_enabled
        self.dir = settings.data_dir / "security"
        self.identity: DeviceIdentity | None = None
        self.transport = transport or LocalOnlyTransport()
        self.publisher: SecurityEventPublisher | None = None
        self.monitor = monitor
        self.lock = lock or LockAdapter(mode=settings.security_lock_mode, mechanism=settings.security_lock_mechanism, on_status=self._lock_status)
        if lock is not None:
            lock.on_status = self._lock_status
        self.session: ArmedSession | None = None
        self.incident: dict | None = None
        self.evidence: deque[SecurityEventV1] = deque(maxlen=MAX_EVIDENCE)
        self.warnings: deque[dict] = deque(maxlen=50)
        self.locks: deque[dict] = deque(maxlen=50)
        self.pending_arm: dict | None = None
        self._last_warned: dict[str, float] = {}
        self._code_hash: bytes | None = None
        self._code_salt: bytes | None = None
        self._failures = 0
        self._locked_out_until = 0.0
        self._was_locked = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_incident_update: IncidentUpdateV1 | None = None
        self.investigations: deque[dict] = deque(maxlen=20)
        self.review_requests: deque[dict] = deque(maxlen=50)
        self._agent = None
        self._tools = None
        self._nemotron = None
        self._investigating: asyncio.Task | None = None
        self.subscriber = None
        self.spacetime_error: str | None = None
        self._last_lock_check = 0.0
        self._last_baseline = 0.0
        self._baseline_pending = False
        self._last_beat = 0.0
        self._server_actions: set[str] = set()
        self._voiced_incidents: set[str] = set()
        self.camera = None  # CameraWatch, built on the first arm when settings.airlock_vision is on
        self.physical: deque[tuple[float, SecurityEventV1]] = deque(maxlen=300)  # (monotonic time, camera event)
        self._presence_pending: tuple[float, SecurityEventV1] | None = None
        self._camera_down_said = -1e9
        self._last_camera_beat = 0.0
        self._presence_said_visit = -1
        self._presence_quiet = False  # said once: quiet while anyone stays in view, until the view is clear
        self._usb_lock = None  # held while this process watches USB, so a second watcher refuses
        self._camera_tasks: set[asyncio.Task] = set()
        self._camera_reasons: set[str] = set()  # "armed" and/or "demo": the camera runs while either wants it
        self.modes = ModeEngine(on_change=self._mode_changed)
        self.demo = None  # DemoService, built on first use
        rt.bus.add_listener(self._on_bus_event)

    def _on_bus_event(self, ev) -> None:
        if ev.event_type == EventType.VOICE_TTS_FINISHED:
            self.speech_finished(ev.payload or {})

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.enabled:
            try:
                await self.start_monitoring()
            except SecurityError as exc:  # another process already watches USB: say so instead of watching twice
                log.warning("security monitoring not started: %s", exc)
                self._publish_state("monitoring_blocked", detail=str(exc)[:200])

    def _prepare(self) -> None:
        if self.publisher is not None:
            return
        self.identity = load_identity(self.dir)
        self._connect_spacetime()
        self.publisher = SecurityEventPublisher(self.identity.device_id, self.transport, directory=self.dir)
        self.rt.tool_gate.on_decision = self._tool_decision
        if self.monitor is None:
            settings = self.rt.settings
            from .mac_apps import create_app_watcher
            from .mac_usb import create_usb_watcher

            self.monitor = SecurityMonitor(
                self.identity, self.publisher, session=lambda: self.session, on_observation=self._from_watcher_thread,
                usb_watcher=create_usb_watcher(settings.security_usb_watcher), app_watcher=create_app_watcher(settings.security_app_watcher),
            )
        else:
            self.monitor.identity, self.monitor.publisher, self.monitor.on_observation = self.identity, self.publisher, self._from_watcher_thread
            self.monitor.session = lambda: self.session

    def spacetime_settings(self) -> dict | None:
        """url, database and workstation from the settings, else from data/security/spacetime.json. None means not connected."""
        s = self.rt.settings
        found = {"url": s.spacetime_url, "database": s.spacetime_database, "workstation_id": s.spacetime_workstation}
        try:
            stored = json.loads((self.dir / "spacetime.json").read_text())
        except (OSError, ValueError):
            stored = {}
        merged = {k: v or stored.get(k) for k, v in found.items()}
        if not all(merged.values()):
            return None
        # Which views to follow, if Sid's device-scoped ones have other names: incidents_view, lock_view, sessions_view in spacetime.json.
        return {**merged, **{k: stored[k] for k in ("incidents_view", "lock_view", "sessions_view") if isinstance(stored.get(k), str) and stored[k]}}

    def _connect_spacetime(self) -> None:
        """Only when configured. A problem here is reported, and local detection and warnings carry on without it."""
        settings = self.spacetime_settings()
        if settings is None or self.subscriber is not None or not isinstance(self.transport, LocalOnlyTransport):
            return
        from .spacetime_adapter import (
            SpacetimeConfig,
            SpacetimeSubscriber,
            SpacetimeTransport,
            load_or_create_token,
        )

        url = settings["url"].rstrip("/")
        views = {k: v for k, v in settings.items() if k in ("incidents_view", "lock_view", "sessions_view") and v}
        config = SpacetimeConfig(url=url, database=settings["database"], workstation_id=settings["workstation_id"], device_id=self.identity.device_id, **views)
        try:
            config.token = load_or_create_token(url, self.dir)
        except Exception as exc:
            self.spacetime_error = f"could not get an identity: {type(exc).__name__}: {exc}"[:200]
            log.warning("SpacetimeDB: %s", self.spacetime_error)
        self.transport = SpacetimeTransport(config)
        problems = self.transport.check()
        if problems:
            self.spacetime_error = "the database's reducers no longer match this adapter: " + "; ".join(problems)[:200]
            log.warning("SpacetimeDB: %s", self.spacetime_error)
        self.subscriber = SpacetimeSubscriber(config, on_incident=self.apply_incident_update, on_lock_request=self.handle_lock_request, on_session=self.apply_remote_session)
        self.subscriber.start()

    async def start_monitoring(self) -> dict:
        """The owner's opt-in to background monitoring. The USB and app watchers run from here until stop_monitoring."""
        self._loop = asyncio.get_running_loop()
        self._prepare()
        if not self.monitor.running:
            if type(self.monitor.usb_watcher).__name__ in ("IOKitUsbWatcher", "PollingUsbWatcher"):
                from .device_lock import DeviceBusy, usb_monitor_lock

                if self._usb_lock is None:
                    self._usb_lock = usb_monitor_lock(self.rt.settings.data_dir)
                try:
                    self._usb_lock.acquire()
                except DeviceBusy as exc:
                    raise SecurityError(str(exc)) from exc
            await asyncio.to_thread(self.monitor.start)
        self.enabled = True
        self._publish_state("monitoring_started")
        self.report_baseline()
        return self.monitor.snapshot()

    @property
    def database_arms(self) -> bool:
        """With SpacetimeDB configured, arming and disarming are the owner's, on the dashboard. This Mac follows."""
        return self.subscriber is not None

    def report_baseline(self) -> None:
        """Tells SpacetimeDB what is connected now, so the owner can arm with it. At most once every few seconds, and the last change of a burst always
        goes out afterwards: from a real run, a hub that came back just after a report left the database with an old capture, so arming was refused."""
        if not self.database_arms or self.monitor is None or self.publisher is None:
            return
        now = time.monotonic()
        wait = BASELINE_MIN_GAP_S - (now - self._last_baseline)
        if wait > 0:
            loop = self._loop
            if not self._baseline_pending and loop is not None and loop.is_running():
                self._baseline_pending = True
                loop.call_later(wait + 0.05, self._report_pending_baseline)
            return
        self._last_baseline = now
        from .spacetime_adapter import BaselineReport

        front = self.monitor.frontmost if isinstance(self.monitor.frontmost, dict) else {}
        self.publisher.publish(BaselineReport(observed_at=int(time.time() * 1000), usb=self.monitor.fingerprints(), active_app_id=(front or {}).get("bundle_id")))

    def _report_pending_baseline(self) -> None:
        self._baseline_pending = False
        self.report_baseline()

    def apply_remote_session(self, remote) -> None:
        """SpacetimeDB's word on whether this workstation is armed. The voice says what really changed, and only then."""
        if self._loop is not None and self._loop.is_running() and asyncio.get_running_loop() is not self._loop:
            self._loop.call_soon_threadsafe(self.apply_remote_session, remote)
            return
        if remote.active():
            if self.session is not None and self.session.session_id == remote.session_id:
                return
            baseline = remote.baseline()
            if baseline is None:
                self.spacetime_error = "the armed session's baseline is not in this Mac's format, so every device counts as new"
            self.session = ArmedSession(remote.session_id, remote.armed_at_ms or int(time.time() * 1000), remote.expires_at_ms, baseline or {}, ResponsePolicy(auto_lock=remote.auto_lock_enabled, approved_apps=remote.approved_apps()),
                                        "spacetimedb", None, remote.owner_policy_hash or None)
            self.pending_arm, self.incident = None, None
            self.evidence.clear()
            self._last_warned.clear()
            self._publish_state("armed", origin="spacetimedb", simulated=remote.simulated)
            self.modes.set_armed(True, remote.session_id)
            self._presence_quiet = False
            self.rt._say("security_armed_camera" if self._camera_on() else "security_armed")
        elif self.session is not None and self.session.session_id == remote.session_id:
            self.session, self.incident = None, None
            self.lock.owner_returned()
            self.rt.tool_gate.unfreeze()
            self._camera_off()
            self.modes.set_armed(False)
            self._publish_state("disarmed", origin="spacetimedb")
            self.rt._say("security_disarmed")

    async def stop_monitoring(self) -> None:
        if self.session is not None:
            raise SecurityError("disarm first: monitoring stays on while armed")
        if self.monitor is not None and self.monitor.running:
            await asyncio.to_thread(self.monitor.stop)
        if self._usb_lock is not None:
            self._usb_lock.release()
        self._publish_state("monitoring_stopped")

    async def close(self) -> None:
        if self.camera is not None:
            await asyncio.to_thread(self.camera.close)
        if self.monitor is not None and self.monitor.running:
            await asyncio.to_thread(self.monitor.stop)
        if self._usb_lock is not None:
            self._usb_lock.release()
        if self.subscriber is not None:
            await self.subscriber.stop()
        if self.publisher is not None:
            await asyncio.to_thread(self.publisher.close)

    # ------------------------------------------------------------------ arming
    def request_arm_by_voice(self):
        """Speech cannot arm by itself. With SpacetimeDB, the owner arms on its dashboard and this Mac follows; without it, the owner sets a disarm code here."""
        rt = self.rt
        if self.session is not None:
            return rt._result(True, "already armed", rt._say("security_already_armed"))
        self.pending_arm = {"at": time.time(), "origin": "voice"}
        self._publish_state("arm_requested")
        if self.database_arms:
            self._last_baseline = 0.0
            self.report_baseline()
            return rt._result(True, "arming is the owner's, on the SpacetimeDB dashboard", rt._say("security_arm_on_dashboard"))
        return rt._result(True, "arming needs the owner's confirmation on the dashboard", rt._say("security_confirm_arm"))

    async def arm(self, owner_code: str, policy: ResponsePolicy | None = None, origin: str = "dashboard") -> ArmedSession:
        if self.session is not None and not self.session.expired():
            raise SecurityError("already armed")
        if self.database_arms:
            raise SecurityError("SpacetimeDB owns arming: arm from its dashboard, and this Mac follows")
        if not owner_code or len(owner_code) < MIN_CODE_LENGTH:
            raise SecurityError(f"choose an owner code of at least {MIN_CODE_LENGTH} characters: disarming will need it")
        await self.start_monitoring()
        monitor = self.monitor
        problems = [name for name in ("usb", "apps") if monitor.errors.get(name)]
        salt = secrets.token_bytes(16)
        code_hash = await asyncio.to_thread(_hash_code, owner_code, salt)
        self._code_salt, self._code_hash, self._failures, self._locked_out_until = salt, code_hash, 0, 0.0
        front = (monitor.frontmost or {}).get("name") if isinstance(monitor.frontmost, dict) else None
        self.session = new_local_session(monitor.fingerprints(), policy or ResponsePolicy(), frontmost_app=front)
        self.pending_arm, self.incident = None, None
        self.evidence.clear()
        self._last_warned.clear()
        self._publish_state("armed", origin=origin)
        self.modes.set_armed(True, self.session.session_id)
        self._presence_quiet = False
        rt = self.rt
        camera = self._camera_on()
        if problems:
            rt._say("security_armed_degraded", problem=" and ".join(problems))
        else:
            rt._say("security_armed_camera" if camera else "security_armed")
        return self.session

    def request_disarm_by_voice(self):
        """Anyone in the room can say a word, so a spoken "disarm" changes nothing."""
        rt = self.rt
        self._publish_state("disarm_refused_by_voice")
        if self.session is None:
            return rt._result(False, "not armed", rt._say("security_not_armed"))
        return rt._result(False, "disarming needs the owner's code on the dashboard; a voice is not enough", rt._say("security_disarm_needs_owner"))

    async def disarm(self, owner_code: str, origin: str = "dashboard") -> None:
        if self.session is None:
            raise SecurityError("not armed")
        if self.session.source == "spacetimedb":
            raise SecurityError("SpacetimeDB armed this session: disarm it from its dashboard")
        await self._check_code(owner_code)
        self.session, self._code_hash, self._code_salt = None, None, None
        self.lock.owner_returned()
        self.incident = None
        self.rt.tool_gate.unfreeze()
        self._camera_off()
        self.modes.set_armed(False)
        self._publish_state("disarmed", origin=origin)
        self.rt._say("security_disarmed")

    async def _check_code(self, owner_code: str) -> None:
        now = time.monotonic()
        if now < self._locked_out_until:
            raise SecurityError("too many wrong codes: wait a minute and try again")
        if self._code_hash is None or not owner_code:
            raise SecurityError("wrong owner code")
        if not hmac.compare_digest(await asyncio.to_thread(_hash_code, owner_code, self._code_salt), self._code_hash):
            self._failures += 1
            if self._failures >= CODE_ATTEMPTS:
                self._failures, self._locked_out_until = 0, now + CODE_LOCKOUT_S
            self._publish_state("wrong_owner_code")
            raise SecurityError("wrong owner code")
        self._failures = 0

    # ------------------------------------------------------------------ the response
    def _from_watcher_thread(self, observation: Observation) -> None:
        """Called on a watcher's thread. The warning is queued from the event loop, right away."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.respond, observation)
        else:
            self.respond(observation)

    def respond(self, observation: Observation) -> None:
        event = observation.event
        if event is None:
            if observation.kind in (SecurityKind.USB_DEVICE_ADDED, SecurityKind.USB_DEVICE_REMOVED):
                self.report_baseline()
            return
        self.evidence.append(event)
        self.rt.bus.publish(EventType.SECURITY_OBSERVED, EventSource.SECURITY, {"event_id": event.event_id, "seq": event.seq, "kind": event.kind.value, "needs_attention": observation.needs_attention, "label": observation.label, "reason": observation.reason, "metadata": event.metadata})
        if not observation.needs_attention:
            return
        self._open_local_incident(observation)
        self._warn(observation)
        if event.kind == SecurityKind.USB_DEVICE_ADDED:
            self.modes.on_usb(event, unknown=observation.reason == "unknown_usb")
        self._local_lock(observation)

    def _open_local_incident(self, observation: Observation) -> None:
        """Only what this Mac saw, for the voice and the investigator. SpacetimeDB's incident, when there is one, is the real record."""
        event = observation.event
        if self.incident is None:
            self.incident = {"incident_id": f"local-{uuid.uuid4().hex[:12]}", "source": "local", "authoritative": False, "state": "SUSPECTED_INTRUSION", "opened_at": event.observed_at, "reason": observation.reason, "evidence_ids": []}
        self.incident["evidence_ids"].append(event.event_id)
        if self.session is not None and self.session.policy.freeze_protected_tools:
            self.rt.tool_gate.freeze("an incident is open")
        self.rt.bus.publish(EventType.SECURITY_INCIDENT, EventSource.SECURITY, dict(self.incident))

    def _warn(self, observation: Observation) -> None:
        reason = observation.reason or "attention"
        now = time.monotonic()
        record = {"event_id": observation.event.event_id, "reason": reason, "label": observation.label, "at": time.time(), "queued_ms": round((now - observation.observed_mono) * 1000, 1), "spoken": False, "audio": None}
        self.warnings.append(record)
        if now - self._last_warned.get(reason, -1e9) < WARNING_REPEAT_S:
            record["skipped"] = "the same warning was just spoken"
            return
        self._last_warned[reason] = now
        category = "security_warning_usb" if reason == "unknown_usb" else "security_warning_app"
        if reason == "unknown_usb":
            category, context = self._usb_wording()
            record["camera_context"] = context
        trace = {"kind": "security", "id": observation.event.event_id, "t0": observation.observed_mono}
        # force: "stop talking" silences conversation, not a new alarm. Never a model, never the network.
        self.rt._say(category, force=True, trace=trace)
        record["spoken"] = True

    def speech_finished(self, payload: dict) -> None:
        """What really happened to a warning: did audio play, and how long after the OS reported the event."""
        trace = payload.get("trace") or {}
        if trace.get("kind") != "security":
            return
        for record in self.warnings:
            if record["event_id"] == trace.get("id"):
                record["audio"] = {k: payload.get(k) for k in ("outcome", "source", "source_label", "to_audio_ms", "output_latency_ms", "detail")}
                # Where the clock started: the settling camera frame (frame_at_ms) and, for arrivals, the first frame with the person.
                record["timing"] = {k: trace.get(k) for k in ("frame_at_ms", "started_at_ms", "trigger") if trace.get(k) is not None}
                record["played"] = payload.get("outcome") == "finished" and payload.get("source") not in ("text_only", "none", None)
                return

    def _local_lock(self, observation: Observation) -> None:
        """With no SpacetimeDB connected, the owner's own opt-in decides. When it is connected, lock requests come from it instead."""
        session = self.session
        if session is None or not session.policy.auto_lock or self.spacetime_connected:
            return
        if observation.reason == "watchlist_app" and not session.policy.auto_lock_on_watchlist_app:
            return
        request = LockRequestV1(
            session_id=session.session_id, action_id=f"local-{observation.event.event_id}", expiry=int(time.time() * 1000) + LOCAL_LOCK_TTL_MS,
            reason=f"{observation.reason} while armed", owner_policy_hash=session.owner_policy_hash, incident_id=None if self.incident is None else self.incident["incident_id"],
        )
        self._run_lock(request)

    @property
    def spacetime_connected(self) -> bool:
        return bool(getattr(self.transport, "connected", False)) or bool(self.subscriber is not None and self.subscriber.connected)

    def _run_lock(self, request: LockRequestV1, owner_initiated: bool = False) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.create_task(asyncio.to_thread(self.lock.request, request, self.session, owner_initiated=owner_initiated))
        else:
            self.lock.request(request, self.session, owner_initiated=owner_initiated)

    async def owner_lock(self, owner_code: str) -> LockResult:
        """The owner asks for a lock from the dashboard. Whether it is real still depends on how the backend was started."""
        session = self.session
        if session is None:
            raise SecurityError("not armed")
        await self._check_code(owner_code)
        request = LockRequestV1(session_id=session.session_id, action_id=f"owner-{uuid.uuid4().hex[:12]}", expiry=int(time.time() * 1000) + LOCAL_LOCK_TTL_MS, reason="the owner asked", owner_policy_hash=session.owner_policy_hash)
        return await asyncio.to_thread(self.lock.request, request, session, owner_initiated=True)

    async def handle_lock_request(self, request: LockRequestV1) -> LockResult:
        """From SpacetimeDB. The Mac still checks everything itself, and reports each state back to the module's report_lock_result."""
        self._server_actions.add(request.action_id)
        return await asyncio.to_thread(self.lock.request, request, self.session)

    def _tool_decision(self, decision, outcome, context: dict) -> None:
        """Every decision by the protected tool gate goes on the record, allowed or denied, and so does whether its callback ran."""
        run_id = str(context.get("run_id") or context.get("mission_id") or "no-run")
        metadata = {"run_id": run_id[:128], "tool_name": decision.tool[:80], "decision": "allowed" if decision.allowed else "denied", "reason": (decision.reason or "")[:240] or None, "callback_ran": outcome.callback_ran}
        self.rt.bus.publish_threadsafe(EventType.SECURITY_TOOL_DECISION, EventSource.SECURITY, {**metadata, "rule": decision.rule, "trace_source": outcome.trace_source, "relay_rejected": outcome.relay_saw_rejection})
        if self.publisher is not None:
            event = self.publisher.build(SecuritySource.MAC_GATE, SecurityKind.PROTECTED_TOOL_DECISION, metadata)
            self.publisher.publish(event)
            if self.session is not None and "run_id" not in context:  # the investigator's own calls are not evidence about anything
                self.evidence.append(event)

    def _lock_status(self, result: LockResult) -> None:
        self.locks.append({"at": time.time(), **result.to_dict()})
        if self.publisher is not None and result.action_id in self._server_actions:
            from .spacetime_adapter import LockResult as LockResultMessage

            self.publisher.publish(LockResultMessage(action_id=result.action_id, status=result.status.value, detail=(result.detail or "")[:240]))
        elif self.publisher is not None:
            try:
                metadata = {"action_id": result.action_id, "session_id": result.session_id, "incident_id": result.incident_id, "status": result.status.value, "mechanism": result.mechanism, "detail": (result.detail or "")[:240] or None, "dry_run": result.dry_run}
                self.publisher.publish(self.publisher.build(SecuritySource.MAC_AGENT, SecurityKind.LOCK_STATUS, metadata))
            except Exception:
                log.exception("recording a lock status failed")
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._lock_said, result.status, result.detail)
        else:
            self._lock_said(result.status, result.detail)

    def _lock_said(self, status: LockStatus, detail: str | None) -> None:
        self.rt.bus.publish(EventType.SECURITY_LOCK, EventSource.SECURITY, {"status": status.value, "detail": detail})
        category = {
            LockStatus.LOCK_REQUESTED: "security_lock_requested",
            LockStatus.LOCK_CONFIRMED: "security_lock_confirmed",
            LockStatus.LOCK_FAILED: "security_lock_failed",
            LockStatus.LOCK_DRY_RUN: "security_lock_dry_run",
        }.get(status)
        if category is not None:
            self.rt._say(category)

    def heartbeat(self) -> None:
        """Every few seconds while connected: each monitor's real state, so the dashboard can tell a working Mac from a silent one."""
        self._camera_heartbeat()
        if not self.database_arms or self.monitor is None or self.publisher is None or not self.monitor.running:
            return
        now = time.monotonic()
        if now - self._last_beat < HEARTBEAT_S:
            return
        self._last_beat = now
        if self.session is None and now - self._last_baseline >= BASELINE_REFRESH_S:
            self.report_baseline()  # keeps the capture the owner arms against fresh
        from .spacetime_adapter import HealthBeat

        for name in ("usb", "apps"):
            watcher = self.monitor.usb_watcher if name == "usb" else self.monitor.app_watcher
            problem = self.monitor.errors.get(name)
            self.publisher.publish(HealthBeat(component=name, status="degraded" if problem else "online", detail=(problem or watcher.method)[:200]), record=False)

    def _camera_heartbeat(self) -> None:
        """The desk camera's own health, from its own device, while it runs and publishes to the module (component "camera",
        online, degraded, offline or unavailable). While disarmed the camera is off and says nothing, which the module shows as offline."""
        cam = self.camera
        publisher, transport = getattr(cam, "publisher", None), getattr(cam, "transport", None)
        if publisher is None or transport is None or isinstance(transport, LocalOnlyTransport) or not getattr(cam, "running", False):
            return
        now = time.monotonic()
        if now - self._last_camera_beat < CAMERA_HEARTBEAT_S:
            return
        self._last_camera_beat = now
        state = getattr(getattr(getattr(cam, "vision", None), "health", None), "state", "starting")
        status = {"live": "online", "obstructed": "degraded", "offline": "offline", "unavailable": "unavailable"}.get(state, "degraded")
        from .spacetime_adapter import HealthBeat

        try:
            publisher.publish(HealthBeat(component="camera", status=status, detail=f"desk camera {state}"), record=False)
        except Exception:  # health reporting never interferes with the warnings
            log.debug("camera heartbeat failed", exc_info=True)

    def tick(self) -> None:
        """An armed session that has run out ends, and says so. The owner coming back is noticed, so a lock they just unlocked does not lock again as they sit down."""
        self.heartbeat()
        if self.session is None:
            return
        self.modes.tick()
        if self._presence_pending is not None and time.monotonic() >= self._presence_pending[0]:
            self._presence_warning("no matching face in time")
        if self.session.expired():
            self.session, self.incident = None, None
            self.rt.tool_gate.unfreeze()
            self._camera_off()
            self.modes.set_armed(False)
            self._publish_state("session_expired")
            self.rt._say("security_expired")
            return
        now = time.monotonic()
        if not self.locks or now - self._last_lock_check < 1.0:
            return
        self._last_lock_check = now
        try:
            locked = self.lock.is_locked()
        except Exception:
            return
        if locked is False and self._was_locked:
            self.lock.owner_returned()
            self._publish_state("owner_unlocked")
        self._was_locked = locked is True

    # ------------------------------------------------------------------ the desk camera
    def camera_conflict(self) -> str | None:
        """Another camera consumer in this process on the same camera: the LOCKED IN focus camera."""
        vision = getattr(self.rt, "vision", None)
        cam = getattr(vision, "camera", None) if vision is not None else None
        if cam is not None and str(getattr(cam, "source", "")) == str(self.rt.settings.airlock_vision_source) and getattr(vision, "_running", False):
            return f"camera {cam.source} is in use by the LOCKED IN focus camera; stop it first"
        return None

    def _ensure_camera(self) -> None:
        if self.camera is None:
            from .camera import CameraWatch

            settings, mac = self.rt.settings, self.spacetime_settings()

            def transport_factory():  # runs inside CameraWatch.start, on a worker thread: files and network stay off the event loop
                return CameraWatch.spacetime_transport(settings, mac, settings.data_dir / "airlock_vision")

            self.camera = CameraWatch(self.rt.settings, on_event=self._from_camera_thread, session=lambda: self.session, transport_factory=transport_factory)
            self.camera.status_provider = lambda: {"mode": self.mode.value, "armed": self.session is not None}

    def _camera_on(self, reason: str = "armed") -> bool:
        """Start the desk camera for an armed session (if the owner turned it on) or for a demo. True if it runs or is starting."""
        if self._loop is None or (reason == "armed" and not getattr(self.rt.settings, "airlock_vision", False) and "demo" not in self._camera_reasons):
            return False
        if self.camera_conflict():
            return False
        self._ensure_camera()
        if reason == "armed":
            self.physical.clear()
            self._presence_pending = None
        already = bool(self._camera_reasons) and self.camera.running
        if reason == "armed" and already:
            self.camera.reobserve()  # armed while the camera was already watching: settle presence again from new frames
        self._camera_reasons.add(reason)
        if not already:
            task = self._loop.create_task(self._camera_call("start"), name="airlock-camera-start")
            self._camera_tasks.add(task)
            task.add_done_callback(self._camera_tasks.discard)
        return True

    def _camera_off(self, reason: str = "armed") -> None:
        self._camera_reasons.discard(reason)
        if reason == "armed":
            self._presence_pending = None
        if self._camera_reasons or self.camera is None or self._loop is None:
            return
        task = self._loop.create_task(self._camera_call("stop"), name="airlock-camera-stop")
        self._camera_tasks.add(task)
        task.add_done_callback(self._camera_tasks.discard)

    async def _camera_call(self, what: str) -> None:
        try:
            await asyncio.to_thread(getattr(self.camera, what))
            if what == "start":
                self.modes.camera_started(True)
        except Exception as exc:  # the Mac's own monitoring is not affected
            self._publish_state("camera_failed", detail=f"{type(exc).__name__}: {exc}"[:200])
            if what == "start":
                self.modes.camera_started(False)
            if what == "start" and self.session is not None:
                self._camera_down_said = time.monotonic()
                self.rt._say("security_camera_degraded")

    def _from_camera_thread(self, event: SecurityEventV1) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.on_camera_event, event)
        else:
            self.on_camera_event(event)

    def on_camera_event(self, event: SecurityEventV1) -> None:
        """What the desk camera saw. It only changes which true sentence LOCKI says, never whether a USB or app warning is given."""
        now = time.monotonic()
        kind = event.kind.value
        self.physical.append((now, event))
        self.rt.bus.publish(EventType.SECURITY_OBSERVED, EventSource.SECURITY, {"event_id": event.event_id, "seq": event.seq, "kind": kind, "source": event.source.value, "device_id": event.device_id,
                                                                                "needs_attention": False, "label": event.metadata.get("zone"), "reason": None, "metadata": event.metadata,
                                                                                "confidence": event.confidence, "observed_at": event.observed_at})
        if self.session is None:
            return
        self.evidence.append(event)
        self._last_camera_event = event
        before = self.mode
        self.modes.on_camera(event)  # may change the mode; _mode_changed reacts
        state = self.modes.state
        if kind in CAMERA_DOWN:
            if now - self._camera_down_said >= 60.0:
                self._camera_down_said = now
                self.rt._say("security_camera_degraded")
        elif kind in ("person_left_zone", "zone_clear"):
            self._presence_pending = None
            if kind == "zone_clear":
                self._presence_quiet = False  # everyone left the view: the next arrival is a new situation
        elif kind == "person_in_zone" and self.mode == SecurityMode.WATCHFUL == before:
            self._start_presence_wait(event)  # someone was only in view; now they are at the desk
        elif kind in ("unrecognized_face", "face_uncertain", "no_face_visible") and self._presence_pending is not None and state.at_desk:
            if kind == "unrecognized_face" or self.modes._visit_owner_at is None:
                self._presence_warning(kind)  # the face said it is not the owner: no need to wait any longer
        elif kind in INTERACTION_ALERTS and self.mode in (SecurityMode.DEFENSIVE, SecurityMode.CRITICAL) and before == self.mode and self.modes._non_owner_present():
            self._physical_alert(event)  # more evidence for the open incident; spoken again only after WARNING_REPEAT_S

    @staticmethod
    def _frame_trace(event: SecurityEventV1) -> dict:
        """Speech timing starts at the camera frame that settled the observation (on this Mac's monotonic clock), not when the line was queued,
        so to_audio_ms is frame to audio start. started_at_ms is the observation's first frame: arrival to audio is that plus the settle time."""
        now_m, now_w = time.monotonic(), time.time()
        t0 = now_m - max(0.0, now_w - event.observed_at / 1000)
        return {"kind": "security", "id": event.event_id, "t0": t0, "frame_at_ms": event.observed_at, "started_at_ms": event.metadata.get("started_at"), "trigger": event.kind.value}

    def _owner_enrolled(self) -> bool:
        faces = getattr(getattr(self.camera, "vision", None), "faces", None) if self.camera is not None else None
        return getattr(faces, "template", None) is not None

    def _event_by_id(self, event_id: str | None) -> SecurityEventV1 | None:
        if event_id is None:
            return None
        return next((e for _t, e in reversed(self.physical) if e.event_id == event_id), None)

    def _start_presence_wait(self, event: SecurityEventV1) -> None:
        if self._presence_said_visit == self.modes.state.visit or self._presence_pending is not None or self._presence_quiet:
            return
        if not self._owner_enrolled():
            self._presence_pending = (time.monotonic(), event)  # nobody is enrolled, so no owner can be about to match: warn now
            self._presence_warning("nobody enrolled")
            return
        self._presence_pending = (time.monotonic() + PRESENCE_FACE_WAIT_S, event)  # give the owner's face about a second to match

    def _mode_changed(self, prev: SecurityMode, new: SecurityMode, reason: str, evidence: list[str], state: VisualState) -> None:
        """Every change is published at once (the dashboard and the voice read it); the voice reacts here, deterministically."""
        self.rt.bus.publish(EventType.SECURITY_MODE, EventSource.SECURITY, {**state.to_dict(), "from": prev.value, "to": new.value, "reason": reason})
        if self.session is None:
            self._presence_pending = None
            return
        event = getattr(self, "_last_camera_event", None)
        if new == SecurityMode.WATCHFUL:
            if not state.at_desk or event is None:
                return  # someone only in view, across the room: attentive, but nothing is said
            if reason == "unrecognized_face":
                self._presence_pending = (time.monotonic(), event)
                self._presence_warning(reason)
            else:
                self._start_presence_wait(event)
            return
        self._presence_pending = None
        if new == SecurityMode.DEFENSIVE:
            cited = self._event_by_id(self.modes.interaction_event_id()) or event
            if cited is not None:
                self._physical_alert(cited)
        elif new == SecurityMode.OWNER_RETURNED:
            self.rt._say("security_owner_seen_incident" if self.incident is not None else "security_owner_seen")

    @property
    def mode(self) -> SecurityMode:
        return self.modes.state.current_security_mode

    def visual_state(self) -> dict:
        return self.modes.state.to_dict()

    def context_block(self) -> str:
        """For the conversation model: what the camera and the Mac actually observed, in a few lines, current at this turn."""
        if not self.enabled:
            return ""
        s = self.modes.state
        if not s.armed_status and not self.physical:
            return ""
        lines = [f"\nSECURITY (observed; answer questions about the desk only from these lines, never guess who someone is, never call anyone a hacker, "
                 f"intruder or criminal, and never say a screen was locked or anyone notified unless a line here says so): mode {s.current_security_mode.value}, "
                 f"{'armed' if s.armed_status else 'disarmed'}, desk camera {s.camera_health}."]
        if s.person_present:
            who = {"owner": "matches the enrolled owner", "unrecognized": "not recognized as the owner", "uncertain": "could not be compared"}.get(s.face_identity_state or "", "unknown")
            touching = [n for n, on in (("keyboard", s.keyboard_interaction), ("laptop", s.laptop_interaction), ("protected device", s.protected_device_interaction), ("USB ports", s.usb_zone_interaction)) if on]
            lines.append(f"Now: someone {'at the desk' if s.at_desk else 'in view'}; face {who}" + (f" (agreement {s.identity_confidence:.2f})" if s.identity_confidence else "")
                         + (f"; touching the {', '.join(touching)}" if touching else "") + ".")
        else:
            lines.append("Now: nobody seen at the desk.")
        recent = [e for _t, e in list(self.physical)[-8:]]
        if recent:
            lines.append("Recent camera events: " + "; ".join(f"{_ago_s(e.observed_at)} {e.kind.value.replace('_', ' ')}" + (f" ({e.metadata['zone']})" if e.metadata.get("zone") else "") for e in recent) + ".")
        if self._episodes():
            lines.append("Visits (from tracked camera events): " + self.describe_desk("who"))
            touched = self.describe_desk("touched")
            if touched.startswith("Yes."):
                lines.append("Touches: " + touched[5:].strip())
        usb = [e for e in list(self.evidence)[-20:] if e.kind == SecurityKind.USB_DEVICE_ADDED and self._flagged(e)]
        if usb:
            lines.append(f"Unknown USB devices this session: {len(usb)}, the last {_ago_s(usb[-1].observed_at)}.")
        if s.current_security_mode in (SecurityMode.DEFENSIVE, SecurityMode.CRITICAL):
            lines.append("Reply in one short, firm sentence: ask them to step away from the computer. Share nothing about the owner.")
        return "\n".join(lines)

    def _latest(self, kinds: set[str], now: float) -> SecurityEventV1 | None:
        for t, e in reversed(self.physical):
            if now - t > PHYSICAL_WINDOW_S:
                return None
            if e.kind.value in kinds:
                return e
        return None

    def _owner_seen(self, now: float) -> bool:
        face = self._latest(FACE_KINDS, now)
        return face is not None and face.kind.value == "owner_face_match"

    def _presence_warning(self, why: str) -> None:
        pending, self._presence_pending = self._presence_pending, None
        if pending is None or self.session is None or self.mode not in (SecurityMode.WATCHFUL,) or not self.modes.state.at_desk:
            return
        if self._presence_said_visit == self.modes.state.visit or self._presence_quiet:
            return
        now = time.monotonic()
        if now - self._last_warned.get("presence", -1e9) < WARNING_REPEAT_S:
            return
        self._last_warned["presence"] = now
        self._presence_said_visit = self.modes.state.visit
        self._presence_quiet = True
        event = pending[1]
        trace = self._frame_trace(event)
        self.warnings.append({"event_id": event.event_id, "reason": "person_at_desk", "label": why, "at": time.time(), "queued_ms": 0.0, "spoken": True, "audio": None})
        self.rt._say("security_warning_presence", force=True, trace=trace)

    def _physical_alert(self, event: SecurityEventV1) -> None:
        """Someone not matched to the owner is using the keyboard, the laptop or a protected device while armed. Observed, not proof of anything."""
        if self.incident is None:
            self.incident = {"incident_id": f"local-{uuid.uuid4().hex[:12]}", "source": "local", "authoritative": False, "state": "SUSPECTED_INTRUSION", "opened_at": event.observed_at, "reason": event.kind.value, "evidence_ids": []}
        self.incident["evidence_ids"].append(event.event_id)
        self.rt.bus.publish(EventType.SECURITY_INCIDENT, EventSource.SECURITY, dict(self.incident))
        now = time.monotonic()
        record = {"event_id": event.event_id, "reason": event.kind.value, "label": event.metadata.get("zone"), "at": time.time(), "queued_ms": round(time.time() * 1000 - event.observed_at, 1), "spoken": False, "audio": None}
        self.warnings.append(record)
        if now - self._last_warned.get("physical", -1e9) < WARNING_REPEAT_S:
            record["skipped"] = "the same warning was just spoken"
            return
        self._last_warned["physical"] = now
        self._presence_pending = None
        self.rt._say("security_warning_interaction", force=True, trace=self._frame_trace(event))
        record["spoken"] = True

    # ------------------------------------------------------------------ the desk, in words, from the record
    IDENTITY_WORDS = {"owner_face_match": "who matched you", "unrecognized_face": "who was not recognized as you",
                      "face_uncertain": "whose face could not be checked", "no_face_visible": "whose face was not visible"}
    TOUCH_WORDS = {"keyboard_interaction": "typed on the keyboard", "laptop_interaction": "touched the laptop",
                   "protected_device_interaction": "touched a protected device", "usb_zone_interaction": "reached into the USB ports"}

    def _episodes(self) -> list[dict]:
        """Visits to the desk this session, rebuilt from camera events. A visit runs from arriving at the desk to leaving it.
        Each face state and each touch keeps its own time, so a touch is described by the face seen before it on that visit."""
        out: list[dict] = []
        open_: dict | None = None
        for _t, e in self.physical:
            kind = e.kind.value
            starts = kind in ("person_in_zone", "person_in_view", "person_approaching") or kind in self.IDENTITY_WORDS or kind in self.TOUCH_WORDS
            if open_ is None and starts:
                open_ = {"start": e.observed_at, "end": None, "faces": [], "touches": [], "evidence": []}
                out.append(open_)
            if open_ is None:
                continue
            open_["evidence"].append(e.event_id)
            if kind in self.IDENTITY_WORDS:
                open_["faces"].append((e.observed_at, kind))
            elif kind in self.TOUCH_WORDS:
                face = next((k for at, k in reversed(open_["faces"]) if k in ("owner_face_match", "unrecognized_face")), None) \
                    or next((k for at, k in reversed(open_["faces"])), None)
                open_["touches"].append((kind, e.metadata.get("dwell_ms"), e.observed_at, face))
            elif kind in ("person_left_zone", "zone_clear"):
                open_["end"] = e.observed_at
                open_ = None
        return out

    def _who(self, faces: list) -> str:
        """Every distinct face state on a visit, anyone not matched first: a later owner match never hides a visitor."""
        kinds = [k for _at, k in faces]
        order = ("unrecognized_face", "face_uncertain", "no_face_visible", "owner_face_match")
        seen = [k for k in order if k in kinds]
        if not seen:
            return self.IDENTITY_WORDS["no_face_visible"]
        words = [self.IDENTITY_WORDS[k] for k in seen]
        return words[0] if len(words) == 1 else ", then ".join([words[0], " and ".join(words[1:])]) if len(words) > 2 else f"{words[0]}, and someone {words[1]}"

    def describe_desk(self, ask: str) -> str:
        """Answers "is someone at my desk", "who was near my workstation" and "did somebody touch my laptop" from tracked
        observations only. Never a name, a description of someone, or a guess at intent. When the camera cannot see, it says so."""
        s = self.modes.state
        cam = self.camera
        running = bool(cam is not None and getattr(cam, "running", False))
        health = getattr(getattr(getattr(cam, "vision", None), "health", None), "state", None) if cam is not None else None
        if not running and not self.physical:
            return "The desk camera isn't on, so I haven't seen the desk. It watches while the workstation is armed with the camera turned on, or during a demo."
        blind = not running or (health is not None and health != "live")
        episodes = self._episodes()
        if ask == "now":
            if blind:
                why = "off" if not running else health
                last = f" Someone was last seen there {_ago_s(episodes[-1]['end'] or episodes[-1]['start'])}." if episodes else ""
                return f"I can't see the desk right now: the camera is {why}.{last}"
            if s.person_present:
                faces = {"owner": "matches you", "unrecognized": "is not recognized as you", "uncertain": "could not be checked"}.get(s.face_identity_state or "", "has not been checked yet")
                touch = [w for k, w in self.TOUCH_WORDS.items() if getattr(s, k)]
                return f"Yes. Someone is {'at your desk' if s.at_desk else 'near your desk'} right now, and their face {faces}." + (f" They just {touch[0]}." if touch else "")
            if episodes:
                return f"No one is at your desk right now. Someone was last there {_ago_s(episodes[-1]['end'] or episodes[-1]['start'])}."
            return "No one is at your desk right now, and no one has been seen there this session."
        if ask == "touched":
            touches = [t for ep in episodes for t in ep["touches"]]
            if not touches:
                return "I didn't see anyone touch the keyboard, the laptop or the USB ports" + (", but the camera can't see right now." if blind else ".")
            parts = []
            for kind, dwell, at, face in touches[-3:]:
                who = self.IDENTITY_WORDS.get(face or "", "whose face wasn't checked")
                parts.append(f"{_ago_s(at)}, someone {who} {self.TOUCH_WORDS[kind]}" + (f" for {round(dwell / 1000, 1)} seconds" if dwell else ""))
            return "Yes. " + "; ".join(parts) + ". That's what the camera saw, not proof of anything."
        if not episodes:
            return "No one has been seen at the desk this session" + (", but the camera can't see right now." if blind else ".")
        parts = []
        for ep in episodes[-3:]:
            dur = None if ep["end"] is None else max(1, round((ep["end"] - ep["start"]) / 1000))
            parts.append(f"{_ago_s(ep['start'])}, someone {self._who(ep['faces'])}" + (f", for {dur} seconds" if dur else ", still there"))
        n = len(episodes)
        return f"{n} visit{'s' if n != 1 else ''} to the desk this session: " + "; ".join(parts) + "."

    def desk_question(self, text: str):
        rt = self.rt
        t = (text or "").lower()
        ask = "touched" if any(w in t for w in ("touch", " use", "used", "type", "mess", "plug")) else "who" if any(w in t for w in ("who was", "who came", "who went", "been", "earlier", "while i was")) else "now"
        return rt._result(True, f"desk: {ask}, from the camera record", rt._say("security_report", report=self.describe_desk(ask)))

    def _usb_wording(self) -> tuple[str, list[str]]:
        """Which true sentence fits a new USB device, from what the camera saw in the last PHYSICAL_WINDOW_S. A face match never removes the warning."""
        now = time.monotonic()
        presence = self._latest(PRESENCE_KINDS, now)
        face = self._latest(FACE_KINDS, now)
        reach = self._latest({"usb_zone_interaction", "hand_near_usb_zone"}, now)
        at_desk = (presence is not None and presence.kind.value == "person_in_zone") or reach is not None
        context = [e.event_id for e in (presence, face, reach) if e is not None]
        if at_desk and face is not None and face.kind.value == "unrecognized_face":
            return "security_warning_usb_unrecognized", context
        if at_desk and (face is None or face.kind.value != "owner_face_match"):
            return "security_warning_usb_person", context
        return "security_warning_usb", context

    # ------------------------------------------------------------------ from SpacetimeDB
    def apply_incident_update(self, update: IncidentUpdateV1) -> None:
        """SpacetimeDB's incident is the real one. LOCKI warns at once for one it has not already warned about: for example the camera seeing someone as a device
        was connected, which this Mac cannot see by itself. A fixture is shown and never spoken or acted on."""
        if self._loop is not None and self._loop.is_running() and asyncio.get_running_loop() is not self._loop:
            self._loop.call_soon_threadsafe(self.apply_incident_update, update)
            return
        self._last_incident_update = update
        self.rt.bus.publish(EventType.SECURITY_INCIDENT, EventSource.SECURITY, {**update.model_dump(mode="json"), "source": "fixture" if update.is_fixture else "spacetimedb", "authoritative": not update.is_fixture})
        if update.is_fixture or self.session is None or (update.session_id and update.session_id != self.session.session_id):
            return
        self.modes.on_incident(update.incident_id, correlated=bool(update.physical_event_id and update.digital_event_id),
                               resolved=update.state == IncidentState.RESOLVED, evidence=[e for e in (update.digital_event_id, update.physical_event_id) if e])
        self.incident = {"incident_id": update.incident_id, "source": "spacetimedb", "authoritative": True, "state": update.state.value, "severity": update.severity.value, "opened_at": update.updated_at, "reason": update.rule_id, "summary": update.summary, "evidence_ids": list(update.evidence_ids)}
        if update.incident_id in self._voiced_incidents or update.state not in (IncidentState.SUSPECTED_INTRUSION, IncidentState.LOCK_REQUESTED):
            return
        received_ms = int(time.time() * 1000)
        if update.updated_at is not None and received_ms - update.updated_at > INCIDENT_VOICE_MAX_AGE_MS:
            self._voiced_incidents.add(update.incident_id)  # old news: never spoken, now or on a later reconnect
            return
        self._voiced_incidents.add(update.incident_id)
        warned = {w["event_id"] for w in self.warnings if w.get("spoken")}
        trace = {"kind": "security", "id": update.incident_id, "t0": time.monotonic()}
        # Each boundary, for measuring: the server's commit time, and when this Mac received the row (both UTC ms). Audio start arrives in speech_finished.
        timing = {"server_updated_at_ms": update.updated_at, "received_at_ms": received_ms, "severity": update.severity.value, "evidence_ids": list(update.evidence_ids)}
        if update.physical_event_id and update.digital_event_id:
            self.warnings.append({"event_id": update.incident_id, "reason": "correlated", "label": update.rule_id, "at": time.time(), "queued_ms": 0.0, "spoken": True, "audio": None, **timing})
            self.rt._say("security_warning_correlated", force=True, trace=trace)
        elif not warned & set(update.evidence_ids):
            self.warnings.append({"event_id": update.incident_id, "reason": "incident", "label": update.rule_id, "at": time.time(), "queued_ms": 0.0, "spoken": True, "audio": None, **timing})
            self.rt._say("security_warning_incident", force=True, trace=trace)

    # ------------------------------------------------------------------ what happened, from the record alone
    def facts(self) -> dict:
        """The record, in plain terms, with no model involved. Used when the investigator cannot run, and by its tools."""
        attention = [e for e in self.evidence if e.kind in (SecurityKind.USB_DEVICE_ADDED, SecurityKind.APP_LAUNCHED) and self._flagged(e)]
        lock = self.locks[-1] if self.locks else None
        return {"armed": self.session is not None, "session_id": None if self.session is None else self.session.session_id, "incident": self.incident, "flagged": [e.event_id for e in attention], "last_lock_status": None if lock is None else lock["status"], "dry_run": None if lock is None else lock["dry_run"]}

    @staticmethod
    def _flagged(event: SecurityEventV1) -> bool:
        m = event.metadata
        return m.get("allowlist_status") == "unknown" or bool(m.get("watchlist_match"))

    def describe_record(self) -> str:
        """One or two true sentences for the voice, built from recorded events."""
        attention = [e for e in self.evidence if e.kind in (SecurityKind.USB_DEVICE_ADDED, SecurityKind.APP_LAUNCHED) and self._flagged(e)]
        desk = self.describe_desk("who") if self._episodes() else ""
        if not attention:
            return ("Nothing unusual from the USB or app monitors since you armed. " + desk).strip() if desk else "Nothing unusual has been recorded since you armed."
        latest = attention[-1]
        when = max(0, round(time.time() - latest.observed_at / 1000))
        if latest.kind == SecurityKind.USB_DEVICE_ADDED:
            m = latest.metadata
            what = f"An unknown {m.get('category', 'USB')} device was connected"
        else:
            what = "A watched app opened"  # a device or an app chooses its own name, so names stay on the dashboard and are not read out
        text = f"{what} {when} seconds ago. That alone does not prove anything is wrong."
        lock = self.locks[-1] if self.locks else None
        if lock is not None:
            text += {"LOCK_CONFIRMED": " The screen lock was confirmed.", "LOCK_EXECUTED": " A screen lock ran, but it isn't confirmed.", "LOCK_DRY_RUN": " The lock was only a dry run, so the screen was not locked.", "LOCK_FAILED": " The screen lock failed.", "LOCK_REFUSED": " No screen lock ran."}.get(lock["status"], "")
        return (text + " " + desk).strip() if desk else text

    async def what_happened(self, origin: str = "voice"):
        """Says it is checking, then the investigator looks. If no model can run, the record itself is the answer."""
        rt = self.rt
        if self.session is None and self.incident is None and not self.investigations:
            return rt._result(True, "not armed", rt._say("security_not_armed"))
        if self._investigating is not None and not self._investigating.done():
            return rt._result(True, "already investigating", rt._say("security_checking"))
        if self.incident is None:
            return rt._result(True, "from the record, no incident", rt._say("security_report", report=self.describe_record()))
        model = self._agent_model()
        if model is None or not model.available():
            self.investigations.append((await self.agent().investigate(self._incident_for_agent(), model)).to_dict())
            return rt._result(True, "from the record: no investigation model", rt._say("security_report", report=self.describe_record() + " My investigation model isn't available, so that comes straight from the record."))
        reply = rt._say("security_checking")
        self._investigating = asyncio.create_task(self._investigate(model), name="airlock-investigation")
        return rt._result(True, f"investigating with {model.identity}", reply)

    def agent(self):
        from ..intelligence.airlock_agent import AirlockAgent
        from .tools import AirlockTools

        if self._agent is None:
            self._tools = AirlockTools(self, self.rt.tool_gate)
            self._agent = AirlockAgent(self._tools, on_step=self._agent_step)
        return self._agent

    def _agent_model(self):
        """Nemotron if it is ready, else the conversation model, labelled. Nothing here ever downloads a model."""
        from ..intelligence.airlock_agent import FallbackAgentModel, OllamaAgentModel
        from ..intelligence.ollama_llm import OllamaLLM

        settings = self.rt.settings
        if self._nemotron is None:
            self._nemotron = OllamaAgentModel(OllamaLLM(settings.airlock_model, base=settings.airlock_ollama_url, allow_lan=True))
        if self._nemotron.available():
            return self._nemotron
        companion_model = self.rt.companion.llm
        if settings.airlock_fallback_model and companion_model is not None and self.rt.companion.available:
            return FallbackAgentModel(companion_model)
        return self._nemotron

    def _incident_for_agent(self) -> dict:
        incident = self.incident or {}
        ids = list(incident.get("evidence_ids") or [])
        kinds = {e.event_id: e.kind.value for e in self.evidence}
        return {"incident_id": incident.get("incident_id"), "source": incident.get("source"), "reason": incident.get("reason"), "state": incident.get("state"), "opened_seconds_ago": None if not incident.get("opened_at") else round(time.time() - incident["opened_at"] / 1000), "evidence_ids": ids, "evidence": [{"event_id": i, "kind": kinds.get(i, "unknown")} for i in ids]}

    async def _investigate(self, model) -> None:
        relay = await self._relay_for_run()
        try:
            run = await self.agent().investigate(self._incident_for_agent(), model, relay=relay)
        except Exception as exc:
            log.exception("the investigation failed")
            self.rt.bus.publish(EventType.SECURITY_INVESTIGATION, EventSource.SECURITY, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]})
            self.rt._say("security_report", report=self.describe_record())
            return
        finally:
            await self._relay_done(relay)
        result = run.to_dict()
        self.investigations.append(result)
        self.rt.bus.publish(EventType.SECURITY_INVESTIGATION, EventSource.SECURITY, result)
        self._speak_report(run)

    def _speak_report(self, run) -> None:
        """The model's spoken line, if it survived the report checks and the voice's own guard. Otherwise the record."""
        from ..intelligence.dialogue import check_reply

        rt = self.rt
        text = (run.report or {}).get("spoken_summary")
        if text and check_reply(text, facts=rt.facts(), style=rt.style(), progress_claim_ok=False, extra_numbers=_numbers_in_run(run)).ok:
            rt.speaker.say(text, priority=PRIORITY_REPLY, category="generated")
            rt.last_reply = {"text": text, "category": "generated", "at": time.time(), "audible": not rt.speaker.muted, "priority": "P2"}
            return
        rt._say("security_report", report=self.describe_record())

    async def _relay_for_run(self):
        from ..missions.relay import RelayRecorder

        if not self.rt.settings.relay_enabled:
            return None
        mission_relay = self.rt.missions.relay
        if mission_relay is not None and mission_relay.active:
            return mission_relay  # Relay allows one active configuration in a process
        relay = RelayRecorder(guardrail=self.rt.tool_gate.guardrail)
        await relay.start(self.dir / "relay" / uuid.uuid4().hex[:12], "airlock-investigation", scope_name="airlock-investigation")
        return relay

    async def _relay_done(self, relay) -> None:
        if relay is not None and relay is not self.rt.missions.relay:
            await relay.stop()

    def _agent_step(self, step: AgentStepV1) -> None:
        if self.publisher is not None:
            self.publisher.publish(step)
        self.rt.bus.publish(EventType.SECURITY_AGENT_STEP, EventSource.SECURITY, step.model_dump(mode="json"))

    # ------------------------------------------------------------------ state
    def _publish_state(self, what: str, **extra) -> None:
        self.rt.bus.publish(EventType.SECURITY_STATE, EventSource.SECURITY, {"what": what, "armed": self.session is not None, "session_source": None if self.session is None else self.session.source, **extra})

    def snapshot(self) -> dict:
        session = self.session
        return {
            "enabled": self.enabled,
            "armed": session is not None,
            "state": "ARMED" if session is not None and self.incident is None else "SUSPECTED_INTRUSION" if session is not None else "DISARMED",
            "state_source": "local" if session is None or session.source == "local" else "spacetimedb",
            "session": None if session is None else {"session_id": session.session_id, "armed_at_ms": session.armed_at_ms, "expires_at_ms": session.expires_at_ms, "baseline_devices": len(session.baseline), "frontmost_app": session.frontmost_app, "policy": session.policy.model_dump(), "policy_hash": session.policy.digest(), "source": session.source},
            "pending_arm": self.pending_arm,
            "monitor": None if self.monitor is None else self.monitor.snapshot(),
            "publisher": None if self.publisher is None else self.publisher.snapshot(),
            "incident": self.incident,
            "warnings": list(self.warnings)[-10:],
            "locks": list(self.locks)[-10:],
            "lock_mode": self.lock.mode,
            "lock_mechanism": self.lock.mechanism,
            "last_incident_update": None if self._last_incident_update is None else self._last_incident_update.model_dump(mode="json"),
            "investigation": None if not self.investigations else self.investigations[-1],
            "investigating": self._investigating is not None and not self._investigating.done(),
            "review_requests": list(self.review_requests)[-10:],
            "gate": self.rt.tool_gate.snapshot(),
            "spacetimedb": None if self.subscriber is None else {**self.subscriber.snapshot(), "transport_connected": bool(getattr(self.transport, "connected", False)), "authorized": getattr(self.transport, "authorized", None),
                                                                 "contract_problems": getattr(self.transport, "contract", None), "last_refusal": getattr(self.transport, "last_refusal", None), "error": self.spacetime_error},
            "camera": None if self.camera is None else self.camera.snapshot(),
            "demo": None if self.demo is None else self.demo.snapshot(),
            "visual_state": self.modes.state.to_dict(),
            "mode_transitions": self.modes.transitions[-10:],
            "camera_events": [{"kind": e.kind.value, "event_id": e.event_id, "observed_at": e.observed_at, "confidence": e.confidence, "zone": e.metadata.get("zone")} for _t, e in list(self.physical)[-12:]],
            "arming": "SpacetimeDB dashboard" if self.database_arms else "this Mac, with the owner code",
            "limits": "Detects and responds. It does not stop a device or a program that has already acted. A locked screen does not stop background processes.",
        }


def _ago_s(observed_at_ms: int) -> str:
    seconds = max(0, round(time.time() - observed_at_ms / 1000))
    return f"{seconds} s ago" if seconds < 90 else f"{round(seconds / 60)} min ago"
