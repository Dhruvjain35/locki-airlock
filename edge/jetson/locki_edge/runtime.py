"""LOCKI AIRLOCK edge runtime: real camera -> observer -> normalizer -> SecurityEventV1 -> outbox -> SpacetimeDB.

    cd edge/jetson && python3 -m locki_edge.runtime --env-file ~/.config/locki-edge/airlock.env

Threads: capture (owns the device), main (observe + normalize), monitor (watchdog, camera_unavailable,
health reports), outbox (delivery), api (127.0.0.1 /health /metrics /receipts).
SIGINT/SIGTERM -> clean shutdown: stop capture, final health report, flush outbox (undelivered events
stay on disk for the next start), close everything. Frames never leave this process.

Python 3.6 compatible on purpose.
"""
import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time

from . import __version__
from .alert import LocalAlarm
from .api import SystemStats, percentile, start_api
from .capture import STATE_DISCONNECTED, STATE_STALLED, STATE_STARTING, STATE_STREAMING, CaptureSource
from .config import ConfigError, load_config
from .contract import ContractError, make_event, utc_iso_ms
from .normalizer import EventNormalizer
from .observers import (AppleVisionPersonProvider, BaselineMotionProvider, DetectNetPersonProvider, FixtureProvider,
                        OpenCvDnnPersonProvider,
                        load_plugin)
from .outbox import DurableOutbox
from .session import SessionTracker
from .publish import SequenceStore
from .stdb import SpacetimePublisher, StdbClient

ALARM_KINDS = ("person_in_zone", "unrecognized_face")


def log(level, msg, **fields):
    rec = {"ts": utc_iso_ms(int(time.time() * 1000)), "level": level, "msg": msg}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def hardware_facts(runtime):
    facts = {"runtime": runtime, "os": platform.platform(), "machine": platform.machine(),
             "python": platform.python_version(), "jetson_verified": False}
    try:
        import cv2
        facts["opencv"] = cv2.__version__
    except Exception:
        facts["opencv"] = None
    if os.path.exists("/proc/device-tree/model"):
        with open("/proc/device-tree/model", "rb") as f:
            facts["model"] = f.read().rstrip(b"\x00").decode("utf-8", "replace")
    elif platform.system() == "Darwin":
        try:
            facts["model"] = subprocess.check_output(["sysctl", "-n", "hw.model"], timeout=2).decode().strip()
        except Exception:
            facts["model"] = None
    if os.path.exists("/etc/nv_tegra_release"):
        with open("/etc/nv_tegra_release") as f:
            facts["l4t"] = f.readline().strip()
        facts["jetson_verified"] = runtime == "jetson"
    return facts


def build_provider(cfg):
    if cfg.observer == "baseline":
        return BaselineMotionProvider(cfg.zones)
    if cfg.observer == "detectnet":
        return DetectNetPersonProvider(cfg.zones, threshold=cfg.detectnet_threshold, model=cfg.detectnet_model)
    if cfg.observer == "opencv_dnn":
        return OpenCvDnnPersonProvider(cfg.zones, cfg.dnn_prototxt, cfg.dnn_model, threshold=cfg.person_threshold)
    if cfg.observer == "apple_vision":
        return AppleVisionPersonProvider(cfg.zones, threshold=cfg.person_threshold)
    if cfg.observer == "dhruv_cv":
        return load_plugin(cfg.observer_plugin, search_path=cfg.observer_path, kind_map=cfg.kind_map,
                           zones=[z._asdict() for z in cfg.zones])
    # fixture: a person "sits" for frames 60-180 of every 300. Always simulated=true.
    return FixtureProvider(lambda i: [("person_in_zone", 0.9, cfg.zones[0].zone_id)] if 60 <= i % 300 < 180 else [])


class EdgeRuntime(object):
    def __init__(self, cfg, capture=None, provider=None, client=None):
        self.cfg = cfg
        self.started_mono = time.monotonic()
        if not os.path.isdir(cfg.state_dir):
            os.makedirs(cfg.state_dir, mode=0o700)
        self.seq = SequenceStore(os.path.join(cfg.state_dir, "seq"))
        # The reducer rejects seq <= the device's last_seq. If the state dir is ever wiped, a counter that
        # restarts at 1 would be rejected forever. Floor it at unix seconds: events are rate-limited far
        # below 1/s, so a seconds floor is always above any seq this device sent before (if the clock is sane).
        if time.time() > 1.7e9:
            with self.seq.lock:
                self.seq.value = max(self.seq.value, int(time.time()))
        self.exit_code = 0
        self.audit_path = os.path.join(cfg.state_dir, "events.jsonl")
        self.provider = provider or build_provider(cfg)
        self.normalizer = EventNormalizer(self.provider, [z.zone_id for z in cfg.zones], cfg.enter_frames,
                                          cfg.exit_s, cooldown_s=cfg.cooldown_s)
        self.norm_lock = threading.Lock()
        self.emit_lock = threading.Lock()
        self.capture = capture or CaptureSource(cfg.camera_source, cfg.runtime, cfg.width, cfg.height, cfg.fps,
                                                stall_after_s=min(2.0, cfg.unavailable_after_s))
        self.publisher = None
        self.outbox = None
        self.session = None
        self.local_only = {}
        if cfg.publish:
            client = client or StdbClient(cfg.server, cfg.database, cfg.token)
            self.publisher = SpacetimePublisher(client, cfg.contract)
            self.outbox = DurableOutbox(os.path.join(cfg.state_dir, "outbox.sqlite3"), self.publisher,
                                        cfg.contract, max_pending=cfg.max_pending, max_age_s=cfg.max_event_age_s,
                                        on_receipt=self._on_receipt)
            self.session = SessionTracker(client, cfg.workstation_id, cfg.session_poll_s,
                                          static_session_id=None if cfg.session_auto else cfg.session_id,
                                          on_change=self._on_session_change)
        self.alarm = LocalAlarm(cfg.alarm_wav, device=cfg.alarm_device) if cfg.local_alarm else None
        self.sys = SystemStats()
        self.facts = hardware_facts(cfg.runtime)
        self.token = cfg.api_token
        self.running = False
        self.shutdown_done = False
        self.shutdown_lock = threading.Lock()
        self.unavailable_sent = False
        self.last_camera_state = None
        self.last_health_at = -1e9
        self.health_results = {}
        self.infer_ms = []
        self.event_latency_ms = []
        self.frames_processed = 0
        self.events = {}
        self.server = None
        self.monitor_thread = None

    # -- events -------------------------------------------------------------------------------
    def emit(self, kind, confidence, observed_at_ms, captured_mono, metadata):
        md = dict(metadata)
        md["runtime"] = self.cfg.runtime
        md["capture_source"] = self.capture.capture_source
        if captured_mono is not None:
            md["capture_to_event_ms"] = round(max(0.0, time.monotonic() - captured_mono) * 1000.0, 1)
        simulated = bool(getattr(self.provider, "simulated", False)) or self.capture.capture_source == "fixture"
        if kind not in self.cfg.contract.kinds:
            return self._local_only(kind, confidence, observed_at_ms, md, simulated, "kind_not_in_contract")
        if self.session is not None:
            session_id, armed = self.session.current()
        else:
            session_id, armed = ("" if self.cfg.session_auto else self.cfg.session_id), True
        if self.outbox is not None and not armed:
            return self._local_only(kind, confidence, observed_at_ms, md, simulated, "not_armed")
        with self.emit_lock:  # seq order == enqueue order across threads
            try:
                event = make_event(self.cfg.contract, self.seq.next, self.cfg.workstation_id or "unassigned",
                                   session_id, self.cfg.device_id or "unenrolled-%s" % self.cfg.runtime,
                                   self.capture.capture_source, kind, observed_at_ms, confidence, md, simulated)
            except ContractError as err:
                log("error", "event_rejected_locally", kind=kind, error=str(err))
                return None
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(event._asdict(), sort_keys=True) + "\n")
            if self.outbox is not None:
                self.outbox.enqueue(event, captured_mono)
        self.events[kind] = self.events.get(kind, 0) + 1
        if "capture_to_event_ms" in md:
            self._push(self.event_latency_ms, md["capture_to_event_ms"])
        log("info", "event", kind=kind, seq=event.seq, event_id=event.event_id, observed_at=event.observed_at,
            confidence=event.confidence, simulated=event.simulated, zone=md.get("zone_id"),
            capture_to_event_ms=md.get("capture_to_event_ms"))
        if self.alarm is not None and kind in ALARM_KINDS:
            self.alarm.play()
        return event

    def _local_only(self, kind, confidence, observed_at_ms, md, simulated, reason):
        """Observed, kept in the local audit, NOT sent: the module would reject it (disarmed / kind)."""
        rec = {"local_only": True, "reason": reason, "kind": kind, "observed_at": utc_iso_ms(int(observed_at_ms)),
               "observed_at_ms": int(observed_at_ms), "confidence": confidence, "metadata": md,
               "device_id": self.cfg.device_id, "workstation_id": self.cfg.workstation_id, "simulated": simulated}
        with self.emit_lock:
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        self.events[kind] = self.events.get(kind, 0) + 1
        self.local_only[reason] = self.local_only.get(reason, 0) + 1
        log("info", "event_local_only", kind=kind, reason=reason, observed_at=rec["observed_at"],
            confidence=confidence, zone=md.get("zone_id"))
        return None

    def _on_session_change(self, session_id, armed, detail):
        log("info" if armed else "warn", "session_changed", session_id=session_id, armed=armed, detail=detail)

    def _on_receipt(self, r):
        log("info" if r.delivered else ("warn" if r.retry_eligible else "error"), "receipt", event_id=r.event_id,
            seq=r.seq, kind=r.kind, status=r.status, reason=r.reason, http=r.http_status,
            server=r.server_result[:120], latency_ms=r.latency_ms, attempt=r.attempt)

    @staticmethod
    def _push(arr, v, cap=500):
        arr.append(v)
        if len(arr) > cap:
            del arr[0]

    # -- health ----------------------------------------------------------------------------------
    def _report_health(self, component, state, detail):
        if self.publisher is None:
            return
        contract = self.cfg.contract
        if component not in contract.health_components:
            self.health_results[component] = {"status_sent": None, "result": "not_sent",
                                              "reason": "component not in contract.health_components",
                                              "http": None, "server": "", "edge_state": state,
                                              "at": utc_iso_ms(int(time.time() * 1000))}
            return
        status = contract.health_status(state)
        detail = ("state=%s %s" % (state, detail)) if status != state else detail
        try:
            result, reason, raw = self.publisher.report_health(self.cfg.workstation_id, self.cfg.device_id,
                                                               component, status, detail[:500])
        except ContractError as err:
            result, reason, raw = "rejected", "local_contract: %s" % err, None
        self.health_results[component] = {"status_sent": status, "edge_state": state, "result": result,
                                          "reason": reason,
                                          "http": None if raw is None else raw.http_status,
                                          "server": "" if raw is None else raw.body[:160],
                                          "at": utc_iso_ms(int(time.time() * 1000))}
        if result != "accepted":
            log("warn", "health_report_not_accepted", component=component, status=status, result=result,
                reason=reason, server=self.health_results[component]["server"])

    def _camera_detail(self):
        h = self.capture.health()
        return "fps=%s age_s=%s err=%s src=%s" % (h["fps"], h["frame_age_s"], h["last_error"], h["capture_source"])

    def monitor_tick(self):
        state = self.capture.watchdog()
        h = self.capture.health()
        changed = state != self.last_camera_state
        if changed:
            log("info" if state == STATE_STREAMING else "warn", "camera_state", state=state,
                prev=self.last_camera_state, error=h["last_error"])
        # Startup gets a longer grace (V4L2/AVFoundation open + first frame can take seconds).
        grace = self.cfg.unavailable_after_s if state != STATE_STARTING else max(5.0, self.cfg.unavailable_after_s)
        if state == STATE_STREAMING:
            self.unavailable_sent = False
        elif state in (STATE_DISCONNECTED, STATE_STALLED, STATE_STARTING) and h["state_for_s"] >= grace \
                and not self.unavailable_sent:
            self.unavailable_sent = True
            with self.norm_lock:
                occupied = self.normalizer.occupied_zones()
                self.normalizer.camera_lost()   # forget presence; never send a fake "left/clear"
            reason = {STATE_STALLED: "stalled", STATE_STARTING: "no_frames"}.get(state, "disconnected")
            self.emit("camera_unavailable", None, int(time.time() * 1000), None,
                      {"reason": reason, "zone_id": self.cfg.zones[0].zone_id, "observation": "camera"})
            if occupied:
                log("warn", "zone_state_unknown_after_camera_loss", zones=occupied)
        self.last_camera_state = state
        if state == STATE_STALLED and self.cfg.restart_on_stall_s > 0 and h["state_for_s"] >= self.cfg.restart_on_stall_s:
            # The reader thread is stuck inside a driver read (seen with V4L2 unplugs). Only a process
            # restart frees the device: exit non-zero so systemd (Restart=on-failure) brings us back.
            log("error", "capture_hung_restarting", stalled_s=h["state_for_s"])
            self.exit_code = 3
            self.running = False
        now = time.monotonic()
        if changed or now - self.last_health_at >= self.cfg.health_interval_s:
            self.last_health_at = now
            self._report_health("camera", state, self._camera_detail())
            self._report_health("edge", self.status(), "runtime=%s observer=%s link=%s" % (
                self.cfg.runtime, self.provider.name, self.outbox.link_state() if self.outbox else "disabled"))

    def _monitor(self):
        while self.running:
            try:
                self.monitor_tick()
            except Exception as err:
                log("error", "monitor_error", error="%s: %s" % (type(err).__name__, err))
            time.sleep(0.25)

    def status(self):
        cam = self.capture.health()
        if cam["state"] != STATE_STREAMING:
            return "degraded" if cam["state_for_s"] < self.cfg.unavailable_after_s else "camera_unavailable"
        if self.outbox is not None and self.outbox.link_state() not in ("online", "unknown"):
            return "degraded"
        if getattr(self.provider, "errors", 0):
            return "degraded"
        return "ok"

    def health(self):
        return {
            "status": self.status(), "version": __version__, "config": self.cfg.public(),
            "hardware": self.facts, "camera": self.capture.health(), "provider": self.provider.describe(),
            "normalizer": dict(self.normalizer.counters, occupied_zones=self.normalizer.occupied_zones()),
            "publisher": self.outbox.stats() if self.outbox else {"link": "disabled",
                                                                  "reason": self.cfg.publish_reason},
            "health_reports": self.health_results, "seq": self.seq.value,
            "session": self.session.describe() if self.session is not None else {"mode": "off"},
            "local_only": dict(self.local_only),
            "uptime_s": round(time.monotonic() - self.started_mono, 1),
        }

    def metrics_payload(self):
        cam = self.capture.health()
        pub = self.outbox.stats() if self.outbox else {}
        return {
            "camera_fps": cam["fps"], "frame_age_s": cam["frame_age_s"], "camera_state": cam["state"],
            "camera_counters": cam["counters"], "frames_processed": self.frames_processed,
            "inference_ms": {"p50": percentile(self.infer_ms, 50), "p95": percentile(self.infer_ms, 95)},
            "capture_to_event_ms": {"p50": percentile(self.event_latency_ms, 50),
                                    "p95": percentile(self.event_latency_ms, 95)},
            "capture_to_ack_ms": {"p50": pub.get("capture_to_ack_ms_p50"), "p95": pub.get("capture_to_ack_ms_p95")},
            "queue_depth": pub.get("pending"), "events": dict(self.events), "system": self.sys.snapshot(),
            "uptime_s": round(time.monotonic() - self.started_mono, 1),
        }

    def receipts_payload(self):
        return {"receipts": self.outbox.receipts(20) if self.outbox else []}

    def local_test_alert(self):
        if self.alarm is None:
            return {"played": False, "reason": "AIRLOCK_LOCAL_ALARM=off"}
        return self.alarm.play(force=True)

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self, with_api=True):
        self.running = True
        log("info", "starting", runtime=self.cfg.runtime, platform=self.cfg.platform,
            camera=self.cfg.camera_source, observer=self.provider.describe(), publish=self.cfg.publish_reason,
            identity=self.cfg.identity, device_id=self.cfg.device_id, workstation_id=self.cfg.workstation_id,
            hardware=self.facts)
        if self.session is not None:
            self.session.poll_once()   # know armed/disarmed before the first frame can produce an event
            self.session.start()
        self.capture.start()
        if self.outbox is not None:
            self.outbox.start()
        if with_api:
            self.server = start_api(self, self.cfg.api_host, self.cfg.api_port)
        self.monitor_thread = threading.Thread(target=self._monitor, name="monitor")
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        return self

    def step(self, timeout=0.5):
        frame = self.capture.wait_frame(timeout)
        if frame is None:
            return False
        t0 = time.monotonic()
        observations = self.provider.observe(frame.image, utc_iso_ms(frame.captured_utc_ms))
        self._push(self.infer_ms, round((time.monotonic() - t0) * 1000.0, 2))
        self.frames_processed += 1
        if self.capture.state != STATE_STREAMING:
            return True
        with self.norm_lock:
            pending = self.normalizer.process(observations, frame.captured_utc_ms, frame.captured_mono)
        for pe in pending:
            self.emit(pe.kind, pe.confidence, pe.observed_at_ms, pe.captured_mono, pe.metadata)
        return True

    def run(self, run_seconds=0):
        deadline = time.monotonic() + run_seconds if run_seconds else None
        while self.running:
            self.step()
            if deadline is not None and time.monotonic() >= deadline:
                break
        self.shutdown()

    def request_stop(self, *_):
        self.running = False

    def shutdown(self, flush_s=5.0):
        with self.shutdown_lock:
            if self.shutdown_done:
                return
            self.shutdown_done = True
        self.running = False
        self.capture.stop()
        if self.session is not None:
            self.session.stop()
        if self.monitor_thread is not None:
            self.monitor_thread.join(timeout=2.0)
        self._report_health("camera", "stopped", "clean shutdown")
        self._report_health("edge", "stopped", "clean shutdown")   # dropped unless the contract permits "edge"
        left = self.outbox.stop(flush_s) if self.outbox is not None else 0
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        try:
            self.provider.close()
        except Exception:
            pass
        summary = {"frames_processed": self.frames_processed, "events": self.events,
                   "undelivered_kept_on_disk": left}
        if self.outbox is not None:
            summary["publisher"] = {k: v for k, v in self.outbox.stats().items() if k != "last_receipt"}
            self.outbox.close()
        log("info", "stopped", **summary)


def main(argv=None):
    p = argparse.ArgumentParser(description="LOCKI AIRLOCK edge runtime")
    p.add_argument("--env-file", help="KEY=VALUE file (chmod 600); environment variables override it")
    p.add_argument("--run-seconds", type=float, default=0, help="stop after N seconds (soak/tests)")
    p.add_argument("--no-api", action="store_true")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.env_file)
    except (ConfigError, ValueError, OSError) as err:
        log("error", "config_refused", error=str(err))
        return 2
    try:
        rt = EdgeRuntime(cfg)
    except Exception as err:  # e.g. detectnet without jetson_inference, a plugin import error, an unreadable state dir
        # Exit 2 = configuration problem: the unit's RestartPreventExitStatus=2 stops a systemd crash loop.
        log("error", "startup_refused", observer=cfg.observer, error="%s: %s" % (type(err).__name__, str(err)[:300]))
        return 2
    signal.signal(signal.SIGTERM, rt.request_stop)
    signal.signal(signal.SIGINT, rt.request_stop)
    rt.start(with_api=not a.no_api)
    try:
        rt.run(a.run_seconds)
    finally:
        rt.shutdown()
    return rt.exit_code


if __name__ == "__main__":
    sys.exit(main())
