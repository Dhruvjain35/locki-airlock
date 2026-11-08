"""Metrics + the local service API: GET /health, GET /metrics, POST /local-test-alert.

Binds to 127.0.0.1 by default. Binding anywhere else REQUIRES a bearer token.
"""
import glob
import hmac
import json
import socket
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:  # pragma: no cover
    raise


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


class Metrics(object):
    def __init__(self, window=300):
        self.lock = threading.Lock()
        self.window = window
        self.frame_times = []
        self.inference_ms = []
        self.capture_to_event_ms = []
        self.frames = 0
        self.read_failures = 0
        self.events = {}
        self.started = time.time()

    def _push(self, arr, v):
        arr.append(v)
        if len(arr) > self.window:
            del arr[0]

    def frame(self, t, inference_ms):
        with self.lock:
            self.frames += 1
            self._push(self.frame_times, t)
            self._push(self.inference_ms, inference_ms)

    def read_failed(self):
        with self.lock:
            self.read_failures += 1

    def event(self, kind, capture_to_event_ms=None):
        with self.lock:
            self.events[kind] = self.events.get(kind, 0) + 1
            if capture_to_event_ms is not None:
                self._push(self.capture_to_event_ms, capture_to_event_ms)

    def fps(self):
        with self.lock:
            ft = self.frame_times[-60:]
        if len(ft) < 2 or ft[-1] == ft[0]:
            return None
        return round((len(ft) - 1) / (ft[-1] - ft[0]), 2)

    def snapshot(self):
        fps = self.fps()
        with self.lock:
            return {
                "uptime_s": round(time.time() - self.started, 1),
                "frames": self.frames,
                "read_failures": self.read_failures,
                "fps": fps,
                "inference_ms": {"p50": percentile(self.inference_ms, 50), "p95": percentile(self.inference_ms, 95),
                                 "n": len(self.inference_ms)},
                "capture_to_event_ms": {"p50": percentile(self.capture_to_event_ms, 50),
                                        "p95": percentile(self.capture_to_event_ms, 95),
                                        "n": len(self.capture_to_event_ms)},
                "events": dict(self.events),
            }


class SystemStats(object):
    """CPU/RAM/GPU/temp from /proc and /sys on Linux/Jetson. Returns None fields elsewhere."""

    GPU_LOAD_GLOBS = ("/sys/devices/gpu.0/load", "/sys/devices/platform/gpu.0/load",
                      "/sys/devices/platform/*.gpu/load", "/sys/devices/platform/*/*.gpu/load",
                      "/sys/devices/*.gpu/load")

    def __init__(self):
        self.prev = None
        self.gpu_path = None
        for pattern in self.GPU_LOAD_GLOBS:
            hits = glob.glob(pattern)
            if hits:
                self.gpu_path = hits[0]
                break

    def _cpu(self):
        try:
            with open("/proc/stat") as f:
                vals = [int(v) for v in f.readline().split()[1:]]
        except (IOError, OSError):
            return None
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        prev, self.prev = self.prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / float(total - prev[1])), 1)

    @staticmethod
    def _mem():
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])
            return round(100.0 * (1 - info["MemAvailable"] / float(info["MemTotal"])), 1)
        except (IOError, OSError, KeyError, ValueError):
            return None

    def _gpu(self):
        if not self.gpu_path:
            return None
        try:
            with open(self.gpu_path) as f:
                return round(int(f.read().strip()) / 10.0, 1)  # Jetson reports per-mille
        except (IOError, OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _temp():
        temps = []
        for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(p) as f:
                    t = int(f.read().strip()) / 1000.0
                if 0 < t < 150:
                    temps.append(t)
            # Orin: some thermal zones return no data on read (TypeError inside the decoder); skip them.
            except (IOError, OSError, ValueError, TypeError):
                pass
        return round(max(temps), 1) if temps else None

    def snapshot(self):
        return {"cpu_pct": self._cpu(), "mem_pct": self._mem(), "gpu_pct": self._gpu(), "temp_c_max": self._temp()}


def is_loopback(host):
    try:
        return socket.gethostbyname(host).startswith("127.")
    except socket.error:
        return False


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(state):
    """state: object with .token, .health(), .metrics_payload(), .local_test_alert()."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "locki-edge/1"

        def log_message(self, fmt, *args):  # keep journald quiet
            pass

        def _send(self, code, payload):
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not state.token:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode("utf-8"), ("Bearer " + state.token).encode("utf-8"))

        def do_GET(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/health":
                return self._send(200, state.health())
            if self.path == "/metrics":
                return self._send(200, state.metrics_payload())
            if self.path == "/receipts" and hasattr(state, "receipts_payload"):
                return self._send(200, state.receipts_payload())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/local-test-alert":
                return self._send(200, state.local_test_alert())
            return self._send(404, {"error": "not found"})

    return Handler


def start_api(state, host="127.0.0.1", port=8765):
    if not is_loopback(host) and not state.token:
        raise SystemExit("refusing to bind %s without AIRLOCK_API_TOKEN" % host)
    server = _Server((host, port), make_handler(state))
    t = threading.Thread(target=server.serve_forever, name="api")
    t.daemon = True
    t.start()
    return server
