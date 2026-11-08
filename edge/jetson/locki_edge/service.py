"""LOCKI AIRLOCK edge service: camera -> detector -> desk-zone events -> SpacetimeDB.

Run:  python3 -m locki_edge.service --help
Every flag also reads an env var (LOCKI_*) so systemd can configure it.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import __version__
from .alert import LocalAlarm
from .api import Metrics, SystemStats, start_api
from .publish import JsonlSink, Outbox, SequenceStore, SpacetimeSink, load_token, make_event, now_ms
from .vision import Camera, Zone, ZoneTracker, make_detector

HEARTBEAT_S = 3.0


def env(name, default=None):
    return os.environ.get("LOCKI_" + name, default)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="LOCKI AIRLOCK Jetson edge service")
    p.add_argument("--camera", default=env("CAMERA", "0"),
                   help="index, /dev/videoN, /dev/v4l/by-id/... (survives replug), or a video file")
    p.add_argument("--source", default=env("SOURCE", "jetson"), choices=["jetson", "mac_fallback_camera"])
    p.add_argument("--device-label", default=env("DEVICE_LABEL", "jetson-desk-1"))
    p.add_argument("--zone", default=env("ZONE", "0.25,0.35,0.75,1.0"), help="x1,y1,x2,y2 normalized desk zone")
    p.add_argument("--detector", default=env("DETECTOR", "auto"), choices=["auto", "detectnet", "motion"])
    p.add_argument("--threshold", type=float, default=float(env("THRESHOLD", "0.5")))
    p.add_argument("--min-overlap", type=float, default=float(env("MIN_OVERLAP", "0.3")))
    p.add_argument("--enter-frames", type=int, default=int(env("ENTER_FRAMES", "3")))
    p.add_argument("--clear-seconds", type=float, default=float(env("CLEAR_SECONDS", "2.0")))
    p.add_argument("--width", type=int, default=int(env("WIDTH", "640")))
    p.add_argument("--height", type=int, default=int(env("HEIGHT", "480")))
    p.add_argument("--state-dir", default=env("STATE_DIR", os.path.expanduser("~/.locki-edge")))
    p.add_argument("--stdb-url", default=env("STDB_URL"), help="e.g. http://192.168.1.20:3000")
    p.add_argument("--stdb-db", default=env("STDB_DB"))
    p.add_argument("--stdb-token-file", default=env("STDB_TOKEN_FILE"))
    p.add_argument("--stdb-reducer", default=env("STDB_REDUCER", "ingest_device_event"))
    p.add_argument("--api-host", default=env("API_HOST", "127.0.0.1"))
    p.add_argument("--api-port", type=int, default=int(env("API_PORT", "8765")))
    p.add_argument("--alert-wav", default=env("ALERT_WAV", os.path.join(os.path.dirname(__file__), "..", "assets",
                                                                        "alert.wav")))
    p.add_argument("--alert-device", default=env("ALERT_DEVICE"))
    p.add_argument("--local-alarm-on-person", action="store_true", default=env("LOCAL_ALARM") == "1",
                   help="play the cached warning on person_in_zone (local, ignores arm state)")
    p.add_argument("--snapshot", help="save one frame with the zone drawn to this .jpg and exit (calibration)")
    p.add_argument("--run-seconds", type=float, default=0, help="stop after N seconds (tests/soak)")
    p.add_argument("--exit-on-camera-eof", action="store_true", help="tests: stop when a video file ends")
    return p.parse_args(argv)


def parse_timedatectl_status(text):
    """systemd 237 (Ubuntu 18.04 / JetPack 4.6) has no `timedatectl show`; parse `status` instead."""
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("System clock synchronized", "NTP synchronized"):
            return val.strip() == "yes"
    return None


def ntp_synced():
    try:
        out = subprocess.check_output(["timedatectl", "status"], stderr=subprocess.DEVNULL, timeout=2)
        return parse_timedatectl_status(out.decode("utf-8", "replace"))
    except Exception:
        return None


def mono_to_wall(t_mono):
    return time.time() - (time.monotonic() - t_mono)


def snapshot(args):
    import cv2
    cam = Camera(args.camera, args.width, args.height)
    frame = None
    deadline = time.time() + 5
    while frame is None and time.time() < deadline:
        frame, _ = cam.read()
        if frame is None:
            time.sleep(0.1)
    cam.release()
    if frame is None:
        print("no frame from camera %s" % args.camera)
        return 2
    zone = Zone.parse(args.zone)
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (int(zone.x1 * w), int(zone.y1 * h)), (int(zone.x2 * w), int(zone.y2 * h)), (0, 0, 255), 3)
    cv2.imwrite(args.snapshot, frame)
    print("saved %s (%dx%d) zone=%s" % (args.snapshot, w, h, zone.as_list()))
    return 0


class EdgeService(object):
    def __init__(self, args):
        self.args = args
        os.makedirs(args.state_dir, exist_ok=True)
        self.zone = Zone.parse(args.zone)
        self.seq = SequenceStore(os.path.join(args.state_dir, "seq"))
        self.metrics = Metrics()
        self.sys = SystemStats()
        self.alarm = LocalAlarm(os.path.abspath(args.alert_wav), device=args.alert_device)
        self.token = os.environ.get("LOCKI_EDGE_API_TOKEN") or None
        self.audit = JsonlSink(os.path.join(args.state_dir, "events.jsonl"))
        self.stdb = None
        self.outbox = None
        if args.stdb_url and args.stdb_db:
            self.stdb = SpacetimeSink(args.stdb_url, args.stdb_db, load_token(args.stdb_token_file),
                                      reducer=args.stdb_reducer)
            self.outbox = Outbox(self.stdb)
        self.detector = make_detector(args.detector, self.zone, threshold=args.threshold)
        self.camera = Camera(args.camera, args.width, args.height)
        self.tracker = ZoneTracker(args.enter_frames, args.clear_seconds)
        self.camera_state = None  # None (unknown) -> "online"/"offline"
        self.running = True
        self.lock = threading.RLock()
        self.last_heartbeat = -HEARTBEAT_S
        self.ntp = ntp_synced()
        self.last_best = None
        log("detector=%s camera=%s zone=%s spacetime=%s" % (
            self.detector.name, args.camera, self.zone.as_list(),
            "%s/%s" % (args.stdb_url, args.stdb_db) if self.stdb else "OFF (local audit log only)"))

    # -- API state ---------------------------------------------------------
    def health(self):
        cam_ok = self.camera.is_online()
        status = "ok" if cam_ok and (self.stdb is None or self.stdb.last_error is None) else "degraded"
        age = self.camera.frame_age()
        return {
            "status": status, "version": __version__, "source": self.args.source,
            "device_label": self.args.device_label, "detector": self.detector.name,
            "camera_ok": cam_ok, "last_frame_age_s": None if age is None else round(age, 2),
            "camera_error": self.camera.last_error, "zone_occupied": self.tracker.occupied,
            "spacetime": None if self.stdb is None else {"url": self.stdb.url, "last_error": self.stdb.last_error,
                                                          "queue_depth": self.outbox.depth()},
            "seq": self.seq.value, "ntp_synced": self.ntp,
        }

    def metrics_payload(self):
        m = self.metrics.snapshot()
        m["system"] = self.sys.snapshot()
        if self.outbox:
            m["publish"] = dict(self.outbox.stats, last_latency_ms=self.stdb.last_latency_ms,
                                queue_depth=self.outbox.depth())
        return m

    def local_test_alert(self):
        return self.alarm.play(force=True)

    # -- events --------------------------------------------------------------
    def emit(self, kind, confidence=None, metadata=None, captured_at=None):
        md = dict(metadata or {})
        md.setdefault("device_label", self.args.device_label)
        md.setdefault("detector", self.detector.name)
        if captured_at is not None:  # captured_at is time.monotonic()
            md["capture_to_event_ms"] = round((time.monotonic() - captured_at) * 1000.0, 1)
        observed = int(round(mono_to_wall(captured_at) * 1000)) if captured_at is not None else now_ms()
        event = make_event(self.seq, self.args.source, kind, confidence, md, observed_at=observed)
        self.audit.send(event)
        if self.outbox:
            self.outbox.put(event)
        self.metrics.event(kind, md.get("capture_to_event_ms"))
        if kind != "heartbeat":
            log("event %s seq=%d conf=%s %s" % (kind, event["seq"], event["confidence"],
                                               json.dumps({k: v for k, v in md.items() if k != "device_label"})))
        return event

    def heartbeat(self):
        sysm = self.sys.snapshot()
        m = self.metrics.snapshot()
        self.emit("heartbeat", metadata={
            "camera_ok": self.camera.is_online(), "zone_occupied": self.tracker.occupied, "fps": m["fps"],
            "inference_ms_p50": m["inference_ms"]["p50"], "cpu_pct": sysm["cpu_pct"], "mem_pct": sysm["mem_pct"],
            "gpu_pct": sysm["gpu_pct"], "temp_c_max": sysm["temp_c_max"], "ntp_synced": self.ntp,
            "queue_depth": self.outbox.depth() if self.outbox else None, "uptime_s": m["uptime_s"],
        })

    # -- main loop -----------------------------------------------------------
    # Frames are processed on the main thread. Offline detection and heartbeats run on a
    # monitor thread, because a V4L2 read on an unplugged camera can block for seconds.
    def monitor(self):
        last_ntp = time.monotonic()
        while self.running:
            now = time.monotonic()
            with self.lock:
                stale = now - (self.camera.last_frame_at or self.camera.started_at) >= self.camera.offline_after
                if self.camera_state != "offline" and stale:
                    self.camera_state = "offline"
                    was_occupied = self.tracker.occupied
                    self.tracker.reset()
                    self.emit("camera_offline", metadata={"reason": self.camera.last_error or "no_frames",
                                                          "zone_was_occupied": was_occupied})
            if now - last_ntp >= 30:
                last_ntp = now
                self.ntp = ntp_synced()
            if now - self.last_heartbeat >= HEARTBEAT_S:
                self.last_heartbeat = now
                with self.lock:  # seq order == queue order across threads
                    self.heartbeat()
            time.sleep(0.2)

    def step(self):
        frame, captured_at = self.camera.read()
        if frame is None:
            self.metrics.read_failed()
            if self.camera.is_file and self.args.exit_on_camera_eof and self.camera_state == "offline":
                self.running = False
            time.sleep(0.05)
            return
        with self.lock:
            if self.camera_state != "online":
                self.camera_state = "online"
                h, w = frame.shape[:2]
                self.emit("camera_online", metadata={"width": w, "height": h})
        t0 = time.monotonic()
        detections = self.detector.detect(frame)
        self.metrics.frame(captured_at, (time.monotonic() - t0) * 1000.0)
        best, best_overlap = None, 0.0
        for d in detections:
            ov = self.zone.overlap(d.bbox)
            if ov >= self.args.min_overlap and ov > best_overlap:
                best, best_overlap = d, ov
        with self.lock:
            if self.camera_state != "online":
                return
            if best is not None:
                self.last_best = (best, best_overlap)
            change = self.tracker.update(best is not None, captured_at)
            if change == "enter":
                d, ov = self.last_best
                self.emit("person_in_zone", d.confidence, {
                    "zone_overlap": round(ov, 3), "bbox": [round(v, 3) for v in d.bbox], "label": d.label,
                    "frames_confirmed": self.args.enter_frames, "zone": self.zone.as_list()},
                    captured_at=captured_at)
                if self.args.local_alarm_on_person:
                    self.alarm.play()
            elif change == "clear":
                occupied = self.tracker.last_seen - self.tracker.entered_at
                self.emit("zone_clear", metadata={"occupied_s": round(occupied, 2)}, captured_at=captured_at)

    def run(self):
        started = time.monotonic()
        mon = threading.Thread(target=self.monitor, name="monitor")
        mon.daemon = True
        mon.start()
        while self.running:
            self.step()
            if self.args.run_seconds and time.monotonic() - started >= self.args.run_seconds:
                self.running = False
        mon.join(timeout=1.0)
        self.camera.release()
        if self.outbox:
            self.outbox.stop()
        log("stopped")

    def stop(self, *_):
        self.running = False


def log(msg):
    sys.stdout.write("[locki-edge %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def main(argv=None):
    args = parse_args(argv)
    if args.snapshot:
        return snapshot(args)
    svc = EdgeService(args)
    server = start_api(svc, args.api_host, args.api_port)
    signal.signal(signal.SIGTERM, svc.stop)
    signal.signal(signal.SIGINT, svc.stop)
    try:
        svc.run()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
