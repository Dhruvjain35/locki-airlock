"""Demo mode: the real product, start to finish, with a fresh owner enrollment every time.

LIVE uses only real hardware and services: webcam frames, model inference, macOS USB events, SpacetimeDB,
speech and Nemotron. REHEARSAL is the same product plus clearly labelled fixture events for whatever is not
available. Fixtures are shown as FIXTURE, never spoken, and never counted as measurements.

Isolation:
- A demo has its own folder, data/airlock/demo/<id>/, holding only its owner template, which is empty at the start.
- While the demo runs, the camera compares faces only against that template, never silently against an older owner.
- Reset and End delete that folder and nothing else. They do not touch the production owner template, the device
  identities, or anything in SpacetimeDB: incidents stay in the database, and the demo never writes to it itself.
- Timings are kept in data/airlock_runs/demo-<id>.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..telemetry.events import EventSource, EventType

if TYPE_CHECKING:
    from .service import SecurityService

log = logging.getLogger(__name__)

MODES = ("live", "rehearsal")
STEPS = ("start", "enroll", "recognized", "zones", "arm", "monitor", "incident", "owner_return", "disarm")
MODEL_FILES = ("efficientdet_lite0.tflite", "face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx", "hand_landmarker.task")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


@dataclass
class DemoRun:
    id: str
    mode: str
    dir: Path
    started_at: float
    started_mono: float
    template: object | None = None
    enroll_started: float | None = None
    enrolled_mono: float | None = None
    first_owner_match_mono: float | None = None
    marks: dict[str, float] = field(default_factory=dict)  # step -> wall time
    timings: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    fixtures: list[dict] = field(default_factory=list)
    trials: list[dict] = field(default_factory=list)  # consenting volunteers: outcomes and timings only, never a face
    events: list[dict] = field(default_factory=list)  # the demo's own timeline (camera, USB, modes, incidents, speech)
    error: str | None = None


class DemoService:
    def __init__(self, security: SecurityService):
        self.security = security
        self.rt = security.rt
        self.root = self.rt.settings.data_dir / "airlock" / "demo"
        self.runs_dir = self.rt.settings.data_dir / "airlock_runs"
        self.run: DemoRun | None = None
        self.history: dict[str, list[float]] = defaultdict(list)  # across demos since the backend started
        self._listening = False
        self._readiness: dict = {}
        self._readiness_at = 0.0

    # ------------------------------------------------------------------ lifecycle
    async def start(self, mode: str = "live") -> dict:
        from .service import SecurityError

        if mode not in MODES:
            raise SecurityError(f"mode is live or rehearsal, not {mode!r}")
        if self.run is not None:
            raise SecurityError("a demo is already running: end it or reset it first")
        security = self.security
        conflict = security.camera_conflict()
        if conflict:
            raise SecurityError(conflict)
        if security._loop is None:
            security._loop = asyncio.get_running_loop()
        demo_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        folder = self.root / demo_id
        folder.mkdir(parents=True, exist_ok=False)
        os.chmod(self.root, 0o700)
        os.chmod(folder, 0o700)
        self.run = DemoRun(demo_id, mode, folder, time.time(), time.monotonic())
        self._listen()
        security._ensure_camera()
        security.camera.use_template(None, "demo")  # nobody is enrolled in a new demo
        security._camera_reasons.add("demo")
        if not security.camera.running:
            try:
                await asyncio.to_thread(security.camera.start)
            except Exception as exc:
                security._camera_reasons.discard("demo")
                self.run.error = f"camera: {type(exc).__name__}: {exc}"[:200]
                raise SecurityError(f"the camera did not start: {exc}") from exc
        self._mark("start")
        self._timeline("demo", f"demo started ({mode}); nobody enrolled")
        self.rt._say("security_demo_started")
        return self.snapshot()

    async def enroll(self) -> dict:
        """Start (or restart) enrolling the owner from the live camera. The owner chose to: this is the button they pressed."""
        from ..vision.airlock.enrollment import EnrollmentConfig, FaceEnrollment
        from ..vision.airlock.faces import SFaceEmbedder, YuNetFaceDetector
        from .service import SecurityError

        run = self._need_run()
        cam = self.security.camera
        if cam is None or not cam.running:
            raise SecurityError("the camera is not running")
        models = self.rt.settings.models_dir
        cfg = EnrollmentConfig(samples=12, per_pose={"center": 6, "left": 3, "right": 3}, min_gap_s=0.25)
        enroller = await asyncio.to_thread(lambda: FaceEnrollment(YuNetFaceDetector(models), SFaceEmbedder(models), cfg))
        cam.use_template(None, "demo")  # a re-enrollment starts from nobody, too
        run.template, run.enrolled_mono, run.first_owner_match_mono = None, None, None
        run.marks.pop("enroll", None)
        run.marks.pop("recognized", None)
        run.enroll_started = time.monotonic()
        cam.start_enrollment(enroller, on_enrolled=self._from_vision_thread)
        self._timeline("enrollment", "enrollment started")
        self.rt._say("security_demo_enroll_start")
        return self.snapshot()

    def _from_vision_thread(self, enroller) -> None:
        loop = self.security._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._enrolled, enroller)
        else:
            self._enrolled(enroller)

    def _enrolled(self, enroller) -> None:
        from ..vision.airlock.enrollment import save_template

        run = self.run
        if run is None:
            return
        template = enroller.template()
        save_template(run.dir / "owner_face.json", template)  # mode 0600, inside the demo's own 0700 folder
        run.template = template
        run.enrolled_mono = time.monotonic()
        if run.enroll_started is not None:
            self._time("enrollment_ms", (run.enrolled_mono - run.enroll_started) * 1000)
        self.security.camera.use_template(template, "demo")
        self._mark("enroll")
        self._timeline("enrollment", f"enrolled: {template.samples.shape[0]} samples {template.poses}; embeddings only, kept on this Mac")
        self.rt._say("security_demo_enrolled")

    async def reset(self) -> dict:
        mode = self.run.mode if self.run is not None else "live"
        await self.end(keep_camera=True)
        return await self.start(mode)

    async def end(self, keep_camera: bool = False) -> dict:
        run = self.run
        if run is None:
            return {"active": False}
        cam = self.security.camera
        if cam is not None:
            cam.stop_enrollment()
            cam.use_template(None, "production")  # back to the production owner template, if there is one
        self._save_timings(run)
        removed = self._remove_demo_folder(run.dir)
        self.run = None
        if not keep_camera:
            self.security._camera_off("demo")
        self.rt.bus.publish(EventType.SECURITY_STATE, EventSource.SECURITY, {"what": "demo_ended", "demo_id": run.id, "removed": removed})
        if not keep_camera:
            self.rt._say("security_demo_ended")
        return {"active": False, "ended": run.id, "removed": removed}

    def _remove_demo_folder(self, folder: Path) -> list[str]:
        """Only a folder directly inside data/airlock/demo/, never anything else."""
        folder = folder.resolve()
        root = self.root.resolve()
        if folder.parent != root or not folder.name or not folder.is_dir():
            log.error("refusing to remove %s: not a demo folder", folder)
            return []
        removed = sorted(p.name for p in folder.iterdir())
        tpl = folder / "owner_face.json"
        if tpl.exists():
            from ..vision.airlock.enrollment import delete_template

            delete_template(tpl)  # overwritten, then removed
        shutil.rmtree(folder, ignore_errors=True)
        return removed

    def _need_run(self) -> DemoRun:
        from .service import SecurityError

        if self.run is None:
            raise SecurityError("start a demo first")
        return self.run

    # ------------------------------------------------------------------ recognition trials with consenting volunteers
    TRIAL_TIMEOUT_S = 8.0

    def trial_start(self, label: str, participant: str) -> dict:
        """A consenting person walks into view after this. ``genuine``: the person enrolled in this demo; ``impostor``: anyone else.
        The first face track that appears after the start decides the trial: its first settled state, and how long that took."""
        from .service import SecurityError

        run = self._need_run()
        if label not in ("genuine", "impostor"):
            raise SecurityError("a trial is genuine (the enrolled person) or impostor (anyone else)")
        if run.template is None:
            raise SecurityError("enroll someone in this demo first: a trial compares against the demo's enrollment")
        participant = "".join(ch for ch in participant if ch.isalnum() or ch in "-_")[:16] or "p"
        if any(t["state"] == "running" for t in run.trials):
            raise SecurityError("a trial is already running")
        run.trials.append({"n": len(run.trials) + 1, "label": label, "participant": participant, "state": "running", "started_at": int(time.time() * 1000),
                           "started_mono": time.monotonic(), "outcome": None, "identity_ms": None, "presence_ms": None, "agreement": None, "uncertain_first": False})
        self._timeline("trial", f"trial {len(run.trials)} started: {label}, participant {participant}")
        return self.snapshot()

    def _trial_event(self, kind: str, meta: dict, confidence) -> None:
        run = self.run
        trial = next((t for t in (run.trials if run else []) if t["state"] == "running"), None)
        if trial is None:
            return
        started = meta.get("started_at")
        if started is None or started < trial["started_at"] - 250:
            return  # a face or person that was already there before the trial began
        if kind in ("person_in_zone", "person_in_view") and trial.get("arrival_at") is None:
            trial["arrival_at"] = started  # the first frame with this person in it
        if kind == "person_in_zone" and trial["presence_ms"] is None and meta.get("dwell_ms") is not None:
            trial["presence_ms"] = float(meta["dwell_ms"]) + float(meta.get("capture_to_event_ms") or 0)
        elif kind == "face_uncertain":
            trial["uncertain_first"] = True
        elif kind in ("owner_face_match", "unrecognized_face"):
            self._finish_trial(trial, "owner" if kind == "owner_face_match" else "unrecognized", meta, confidence)

    def _finish_trial(self, trial: dict, outcome: str, meta: dict | None, confidence) -> None:
        trial["state"], trial["outcome"], trial["agreement"] = "done", outcome, confidence
        if meta is not None and meta.get("dwell_ms") is not None:
            trial["identity_ms"] = float(meta["dwell_ms"]) + float(meta.get("capture_to_event_ms") or 0)  # first face frame to the settled event
        if meta is not None and trial.get("arrival_at") and meta.get("observed_at"):
            trial["arrival_to_identity_ms"] = float(meta["observed_at"]) - float(trial["arrival_at"]) + float(meta.get("capture_to_event_ms") or 0)
        correct = (trial["label"] == "genuine" and outcome == "owner") or (trial["label"] == "impostor" and outcome == "unrecognized")
        trial["correct"] = correct
        self._timeline("trial", f"trial {trial['n']} ({trial['label']}, {trial['participant']}): {outcome}" + (f" in {trial['identity_ms']:.0f} ms" if trial["identity_ms"] else ""))

    def _trial_tick(self) -> None:
        run = self.run
        for t in (run.trials if run else []):
            if t["state"] == "running" and time.monotonic() - t["started_mono"] > self.TRIAL_TIMEOUT_S:
                self._finish_trial(t, "uncertain" if t["uncertain_first"] else "missed", None, None)

    def trial_summary(self) -> dict:
        run = self.run
        done = [t for t in (run.trials if run else []) if t["state"] == "done"]
        genuine = [t for t in done if t["label"] == "genuine"]
        impostor = [t for t in done if t["label"] == "impostor"]

        def count(ts, outcome):
            return sum(1 for t in ts if t["outcome"] == outcome)

        lat = {o: [t["identity_ms"] for t in done if t["outcome"] == o and t["identity_ms"] is not None] for o in ("owner", "unrecognized")}
        arrival = [t["arrival_to_identity_ms"] for t in done if t.get("arrival_to_identity_ms") is not None]
        return {
            "trials": len(done),
            "participants": sorted({t["participant"] for t in done}),
            "genuine": {"n": len(genuine), "matched": count(genuine, "owner"), "false_unrecognized": count(genuine, "unrecognized"),
                        "uncertain": count(genuine, "uncertain"), "missed": count(genuine, "missed")},
            "impostor": {"n": len(impostor), "false_owner_match": count(impostor, "owner"), "unrecognized": count(impostor, "unrecognized"),
                         "uncertain": count(impostor, "uncertain"), "missed": count(impostor, "missed")},
            "identity_ms": {o: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "max": round(max(v), 1) if v else None, "n": len(v)} for o, v in lat.items()},
            "arrival_to_identity_ms": {"p50": _pct(arrival, 0.5), "p95": _pct(arrival, 0.95), "max": round(max(arrival), 1) if arrival else None, "n": len(arrival)},
            "note": "a handful of consenting people in this room: rates describe them, not a population",
        }

    # ------------------------------------------------------------------ rehearsal fixtures
    def rehearsal_usb(self) -> dict:
        """REHEARSAL only: a fixture USB arrival, labelled FIXTURE, shown in the timeline and fed to the mode engine,
        never spoken and never measured. In LIVE, plug in a real drive instead."""
        from .contracts import SecurityEventV1, SecurityKind, SecuritySource
        from .service import SecurityError

        run = self._need_run()
        if run.mode != "rehearsal":
            raise SecurityError("fixtures are for rehearsal demos only; in a live demo, plug in a real USB drive")
        event = SecurityEventV1(event_id=f"fixture-{uuid.uuid4().hex[:12]}", device_id="fixture-mac", source=SecuritySource.MAC_USB, kind=SecurityKind.USB_DEVICE_ADDED,
                                observed_at=int(time.time() * 1000), seq=0, metadata={"fingerprint": "fixture" + "0" * 12, "has_serial": False, "vendor_id": "0x0000",
                                                                                      "product_id": "0x0000", "category": "storage", "allowlist_status": "unknown",
                                                                                      "detection": "iokit_notification", "armed": self.security.session is not None}, is_fixture=True)
        run.fixtures.append({"kind": "usb_device_added", "event_id": event.event_id, "at": time.time()})
        self._timeline("fixture", "FIXTURE: a pretend unknown USB device (rehearsal only, not real hardware)")
        self.security.modes.on_usb(event, unknown=True)
        return self.snapshot()

    # ------------------------------------------------------------------ measurements, from real events
    def _listen(self) -> None:
        if not self._listening:
            self.rt.bus.add_listener(self._on_bus)
            self._listening = True

    def _time(self, name: str, ms: float) -> None:
        if self.run is not None:
            self.run.timings[name].append(round(ms, 1))
        self.history[name].append(round(ms, 1))

    def _mark(self, step: str) -> None:
        if self.run is not None and step not in self.run.marks:
            self.run.marks[step] = time.time()

    def _timeline(self, kind: str, text: str, **extra) -> None:
        if self.run is not None:
            self.run.events.append({"at": int(time.time() * 1000), "kind": kind, "text": text, **extra})
            del self.run.events[:-200]

    def _on_bus(self, ev) -> None:
        run = self.run
        if run is None:
            return
        p = ev.payload or {}
        t = ev.event_type
        if t == EventType.SECURITY_OBSERVED:
            kind = p.get("kind")
            meta = p.get("metadata") or {}
            self._trial_event(str(kind), {**meta, "observed_at": p.get("observed_at")}, p.get("confidence"))
            self._timeline("camera" if str(p.get("source", "")).endswith("vision") else "usb", str(kind), zone=meta.get("zone"), confidence=meta.get("confidence"))
            if kind == "owner_face_match" and run.enrolled_mono is not None and run.first_owner_match_mono is None:
                run.first_owner_match_mono = time.monotonic()
                self._time("owner_recognition_after_enrollment_ms", (run.first_owner_match_mono - run.enrolled_mono) * 1000)
                self._mark("recognized")
                if self.security.session is None:
                    self.rt._say("security_demo_recognized")
            if kind == "person_in_zone" and meta.get("dwell_ms") is not None:
                self._time("presence_ms", float(meta["dwell_ms"]) + float(meta.get("capture_to_event_ms") or 0))
        elif t == EventType.SECURITY_MODE:
            self._timeline("mode", f"{p.get('from')} -> {p.get('to')} ({p.get('reason')})", mode=p.get("to"))
            since, evidence = p.get("mode_since"), p.get("evidence_ids") or []
            observed = next((e["at"] for e in reversed(run.events) if e["kind"] in ("camera", "usb")), None)
            if since and observed:
                self._time("mode_switch_ms", max(0.0, since - observed))
            if p.get("to") == "OWNER_RETURNED" and "incident" in run.marks:
                self._mark("owner_return")
            if p.get("to") in ("WATCHFUL", "DEFENSIVE", "CRITICAL") and self.security.session is not None:
                self._mark("monitor")
            del evidence
        elif t == EventType.SECURITY_INCIDENT:
            self._mark("incident")
            self._timeline("incident", f"incident {p.get('incident_id')} ({p.get('source')}, {p.get('state')})", authoritative=p.get("authoritative"))
        elif t == EventType.SECURITY_STATE:
            what = p.get("what") or p.get("state") or p.get("event")
            if what == "armed":
                self._mark("arm")
                self._timeline("state", "armed")
            elif what in ("disarmed", "session_expired") and "arm" in run.marks:
                self._mark("disarm")
                self._timeline("state", str(what))
        elif t == EventType.VOICE_TTS_FINISHED:
            trace = p.get("trace") or {}
            if trace.get("kind") == "security" and p.get("to_audio_ms") is not None and p.get("outcome") == "finished":
                audio = float(p["to_audio_ms"])
                self._time("event_to_speech_ms", audio)  # from the settling camera frame (or the OS report) to audio start
                if p.get("output_latency_ms") is not None:
                    self._time("frame_to_audible_est_ms", audio + float(p["output_latency_ms"]))  # plus the device's own reported latency
                if trace.get("started_at_ms") and trace.get("frame_at_ms"):
                    self._time("arrival_to_speech_ms", audio + float(trace["frame_at_ms"]) - float(trace["started_at_ms"]))
                self._timeline("speech", f"{trace.get('trigger') or 'warning'}: audio {audio:.0f} ms after the frame"
                               + (f" (+{p['output_latency_ms']:.0f} ms device)" if p.get("output_latency_ms") is not None else ""))
        elif t == EventType.SECURITY_INVESTIGATION:
            ms = (p.get("measures") or {}).get("total_ms") or p.get("total_ms")
            if ms:
                self._time("investigation_ms", float(ms))
            self._timeline("investigation", f"investigation {p.get('run_id', '')}: {p.get('model', '')}")

    def _save_timings(self, run: DemoRun) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            out = {"demo_id": run.id, "mode": run.mode, "started_at": run.started_at, "ended_at": time.time(), "steps": run.marks,
                   "trials": [{k: v for k, v in t.items() if k != "started_mono"} for t in run.trials], "trial_summary": self.trial_summary(),
                   "timings": {k: list(v) for k, v in run.timings.items()}, "fixtures": run.fixtures, "timeline": run.events,
                   "note": "measured on real events; fixtures are never measured"}
            (self.runs_dir / f"demo-{run.id}.json").write_text(json.dumps(out, indent=1) + "\n")
        except Exception:
            log.exception("could not save demo timings")

    # ------------------------------------------------------------------ status
    def readiness(self) -> dict:
        """Each component's real state. Nothing is shown as working unless it reports that it is."""
        now = time.monotonic()
        if now - self._readiness_at < 1.0 and self._readiness:
            return self._readiness
        sec, rt, s = self.security, self.rt, self.rt.settings
        cam = sec.camera
        vision = getattr(cam, "vision", None) if cam is not None else None
        health = getattr(getattr(vision, "health", None), "state", None)
        out = {}
        running = bool(cam is not None and cam.running)
        out["camera"] = {"ok": running and health == "live",
                         "detail": "off" if not running else f"{health or 'starting'}" + (f", {vision.stats.fps:.0f} fps" if vision is not None and vision.stats.fps else "")}
        missing = [f for f in MODEL_FILES if not (s.models_dir / f).exists()]
        out["models"] = {"ok": not missing, "detail": "all present" if not missing else "missing " + ", ".join(missing)}
        mon = sec.monitor
        out["usb_monitor"] = {"ok": bool(mon is not None and mon.running and not mon.errors.get("usb")), "detail": "running" if mon is not None and mon.running else "not running"}
        out["spacetimedb"] = {"ok": bool(sec.spacetime_connected and getattr(sec.transport, "authorized", None) is not False),
                              "detail": "connected" if sec.spacetime_connected else (sec.spacetime_error or "not configured")}
        cam_transport = getattr(cam, "transport", None) if cam is not None else None
        out["camera_to_database"] = {"ok": cam_transport is not None and type(cam_transport).__name__ == "SpacetimeTransport",
                                     "detail": "publishing as the enrolled camera" if cam_transport is not None and type(cam_transport).__name__ == "SpacetimeTransport"
                                     else "local only: enroll the camera device (scripts/airlock.py camera-setup)"}
        out["speaker"] = {"ok": s.tts_engine != "none", "detail": "text only (QUIET)" if s.tts_engine == "none" else "speaker"}
        stt = rt.voice.snapshot().get("stt", {}) if getattr(rt, "voice", None) is not None else {}
        out["microphone"] = {"ok": getattr(rt, "voice", None) is not None and stt.get("state") in ("ready", "listening", "running"), "detail": stt.get("state") or "voice input not started"}
        try:
            model = sec._agent_model()
            out["investigator"] = {"ok": bool(model is not None and model.available()), "detail": "none" if model is None else f"{model.identity}" + ("" if model.available() else f" ({getattr(model, 'why_unavailable', '')})"),
                                   "nemotron": bool(model is not None and getattr(model, "qualifies_as_nemotron", False))}
        except Exception as exc:
            out["investigator"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:120], "nemotron": False}
        out["screen_lock"] = {"ok": True, "detail": f"{sec.lock.mode} (real locks need your explicit approval)"}
        from ..vision.airlock.geometry import ZoneMap

        zones = ZoneMap.load(s.data_dir / "airlock" / "zones.json")
        out["zones"] = {"ok": zones.calibrated, "detail": f"calibrated: {', '.join(zones.zones)}" if zones.calibrated else "default desk zone only: draw them with scripts/airlock_zones.py"}
        self._readiness, self._readiness_at = out, now
        return out

    def steps(self) -> dict:
        run = self.run
        marks = run.marks if run is not None else {}
        readiness = self.readiness()
        out = {}
        for step in STEPS:
            if step == "zones":
                out[step] = "done" if readiness["zones"]["ok"] else "default"
            else:
                out[step] = "done" if step in marks else "pending"
        return out

    def snapshot(self) -> dict:
        self._trial_tick()
        run = self.run
        cam = self.security.camera
        vision = getattr(cam, "vision", None) if cam is not None else None
        faces = getattr(vision, "faces", None) if vision is not None else None
        stats = {k: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "n": len(v)} for k, v in (run.timings.items() if run is not None else [])}
        if vision is not None and run is not None and cam is not None and cam.running:  # never numbers from a camera that has stopped
            fs = faces.snapshot() if faces is not None else {}
            for name, key in (("owner_identity_ms", "owner_face_match"), ("unrecognized_identity_ms", "unrecognized_face"), ("uncertain_identity_ms", "face_uncertain")):
                d = (fs.get("identity_ms") or {}).get(key) or {}
                if d.get("n"):
                    stats[name] = d
            vs = vision.snapshot()["stats"]
            stats["person_detect_ms"] = {"p50": vs["detect_ms_p50"], "p95": vs["detect_ms_p95"]}
            stats["capture_to_event_ms"] = {"p50": vs["capture_to_event_ms_p50"], "p95": vs["capture_to_event_ms_p95"]}
            stats["face_pass_ms"] = {"p50": fs.get("ms_p50"), "p95": fs.get("ms_p95")}
            stats["face_compare_ms"] = {"p50": fs.get("compare_ms_p50"), "p95": fs.get("compare_ms_p95")}
            if vision.live_since is not None and vision.first_person_mono is not None:
                stats["first_person_after_camera_ms"] = {"p50": round((vision.first_person_mono - vision.live_since) * 1000, 1), "n": 1}
            stats["fps"] = round(vision.stats.fps, 1)
        enrolling = vision is not None and vision.enroller is not None
        return {
            "active": run is not None,
            "id": None if run is None else run.id,
            "mode": None if run is None else run.mode,
            "label": None if run is None else ("LIVE: real hardware and services only" if run.mode == "live" else "REHEARSAL: fixture events are labelled FIXTURE and never spoken"),
            "started_at": None if run is None else run.started_at,
            "enrolling": enrolling,
            "enrollment": vision.enroll_progress if vision is not None else None,
            "enrolled": bool(run is not None and run.template is not None),
            "enrolled_samples": None if run is None or run.template is None else int(run.template.samples.shape[0]),
            "recognized": bool(run is not None and run.first_owner_match_mono is not None),
            "template": "this demo's own (data/airlock/demo/<id>/owner_face.json, embeddings only)" if run is not None and run.template is not None
                        else "none: nobody is enrolled in this demo" if run is not None else None,
            "thresholds": None if run is None or run.template is None else {"match": run.template.thresholds.match, "reject": run.template.thresholds.reject, "calibrated": run.template.thresholds.calibrated},
            "steps": self.steps(),
            "readiness": self.readiness(),
            "timings": stats,
            "history": {k: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "n": len(v)} for k, v in self.history.items()},
            "timeline": [] if run is None else run.events[-60:],
            "fixtures": [] if run is None else run.fixtures,
            "trials": [] if run is None else [{k: v for k, v in t.items() if k != "started_mono"} for t in run.trials[-30:]],
            "trial_summary": self.trial_summary(),
            "error": None if run is None else run.error,
            "visual_state": self.security.modes.state.to_dict(),
            "armed": self.security.session is not None,
            "arming": "database" if self.security.database_arms else "local",
            "camera_running": bool(cam is not None and cam.running),
            "preview": "/api/security/demo/stream.mjpg",
        }
