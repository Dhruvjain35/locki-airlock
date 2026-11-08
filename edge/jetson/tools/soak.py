"""EDGE-01 soak: run the real runtime for N seconds and sample metrics every 10 s.

    python3 tools/soak.py --env-file ~/.config/locki-edge/airlock.env --seconds 600 --out <dir>

Writes <dir>/samples.jsonl and <dir>/SUMMARY.md. Works on the Mac (runtime=mac_dev) and on the
Jetson (runtime=jetson). The summary states which runtime produced it; a Mac soak is NOT a Jetson pass.
"""
import argparse
import json
import os
import platform
import resource
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.config import load_config  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402


def rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024.0 * 1024.0) if platform.system() == "Darwin" else peak / 1024.0, 1)


def current_rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (IOError, OSError):
        pass
    try:
        import subprocess
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], timeout=2)
        return round(int(out.strip()) / 1024.0, 1)
    except Exception:
        return None


def rng(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n/a"
    return "%.1f / %.1f / %.1f" % (min(vals), sum(vals) / len(vals), max(vals))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--env-file")
    p.add_argument("--seconds", type=float, default=600)
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    cfg = load_config(a.env_file)
    rt = EdgeRuntime(cfg).start(with_api=True)
    loop = threading.Thread(target=rt.run, name="frame-loop")
    loop.start()
    samples = []
    t0 = time.monotonic()
    cpu0 = os.times()
    try:
        with open(os.path.join(a.out, "samples.jsonl"), "w") as f:
            while time.monotonic() - t0 < a.seconds and loop.is_alive():
                time.sleep(a.interval)
                m = rt.metrics_payload()
                t = os.times()
                elapsed = time.monotonic() - t0
                m["t_s"] = round(elapsed, 1)
                m["proc_cpu_pct"] = round(100.0 * ((t.user + t.system) - (cpu0.user + cpu0.system)) / elapsed, 1)
                m["rss_mb"] = current_rss_mb()
                samples.append(m)
                f.write(json.dumps(m, sort_keys=True) + "\n")
                f.flush()
    finally:
        rt.request_stop()
        loop.join(20)
    cam = samples[-1]["camera_counters"] if samples else {}
    fps = [s["camera_fps"] for s in samples]
    ages = [s["frame_age_s"] * 1000.0 for s in samples if s["frame_age_s"] is not None]
    rss = [s["rss_mb"] for s in samples]
    qd = [s["queue_depth"] for s in samples if s["queue_depth"] is not None]
    last = samples[-1] if samples else {}
    lines = [
        "# Soak summary (%s)" % cfg.runtime,
        "",
        "| Field | Value |", "|---|---|",
        "| Runtime | `%s` on %s (%s) |" % (cfg.runtime, rt.facts.get("model"), rt.facts.get("os")),
        "| Jetson-verified | %s |" % ("YES" if rt.facts.get("jetson_verified") else "NO (not a Jetson pass)"),
        "| Host | %s |" % socket.gethostname(),
        "| Camera | %s via %s |" % (cfg.camera_source, rt.capture.backend),
        "| Duration | %.0f s, %d samples every %.0f s |" % (time.monotonic() - t0, len(samples), a.interval),
        "| Capture FPS min/mean/max | %s |" % rng(fps),
        "| Frame age at sample, ms min/mean/max | %s |" % rng(ages),
        "| Frames captured / processed | %s / %s |" % (cam.get("frames"), last.get("frames_processed")),
        "| Stale frames dropped (by design) | %s |" % cam.get("dropped_stale"),
        "| Read failures / disconnects / reconnects | %s / %s / %s |" % (
            cam.get("read_failures"), cam.get("disconnects"), cam.get("reconnects")),
        "| Inference ms p50/p95 (last window) | %s / %s |" % (
            last.get("inference_ms", {}).get("p50"), last.get("inference_ms", {}).get("p95")),
        "| Process CPU %% (avg since start) | %s |" % (last.get("proc_cpu_pct")),
        "| RSS MB min/mean/max (leak check) | %s |" % rng(rss),
        "| Outbox queue depth max | %s |" % (max(qd) if qd else "publishing off"),
        "| Events | %s |" % json.dumps(last.get("events", {})),
    ]
    with open(os.path.join(a.out, "SUMMARY.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
