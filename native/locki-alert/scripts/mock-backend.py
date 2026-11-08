#!/usr/bin/env python3
"""Contract-shaped mock of GET /api/security/takeover + POST /api/security/takeover/report for testing
LOCKI Alert end to end WITHOUT the real backend. Loopback only, never port 8077.

    python3 -I scripts/mock-backend.py --port 18077 --token-file /tmp/x/alert.token --out /tmp/x/mock.json

Timeline (seconds after start); every live overlay stays on screen under 3 s:
  0.0  armed, session S1, no takeover
  1.0  live+fresh takeover usb:mock-1 (local CRITICAL verified)       -> helper shows, reports "shown"
  2.0  HTTP 503 (backend down)                                        -> amber "connection lost" line, record kept
  2.6  usb:mock-1 again, audio pending->played                        -> update in place, no second show
  3.2  armed=false                                                    -> panel closes (disarm)
  4.2  session S2, live takeover incident:mock-2, local_mode_verified=false -> shows "Server-confirmed" kicker
  5.0  incident:mock-2 with acknowledged_at_ms set (dismissed elsewhere) -> panel hides, ack persisted
  6.0  incident:mock-2 again with acknowledged_at_ms=null (backend "restart") -> must NOT re-show
  7.0  stale takeover usb:mock-3 (fresh=false)                         -> must NOT show
  8.0  HTTP 404 (no native support)                                    -> nothing shown
  9.0  token rotated: old token gets 401                               -> helper re-reads alert.token, recovers
 10.0  armed, no takeover;  11.0 mock exits
"""
import argparse, hmac, json, os, secrets, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=18077)
ap.add_argument("--token-file", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--duration", type=float, default=11.0)
args = ap.parse_args()
if args.port == 8077:
    sys.exit("refusing to run on the live backend port 8077")

T0 = time.time()
state = {"token": None, "gets": 0, "get_headers": None, "posts": [], "phases_seen": [], "acks": {}, "shown": {}}
lock = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def write_token():
    tok = secrets.token_hex(32)
    d = os.path.dirname(args.token_file)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = args.token_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(tok)
    os.chmod(tmp, 0o600)
    os.replace(tmp, args.token_file)
    state["token"] = tok


write_token()
DET1 = int((T0 + 1.0) * 1000)
DET2 = int((T0 + 4.2) * 1000)


def ev(kind, label, detail, at, source, eid=None, sev=None):
    e = {"kind": kind, "label": label, "detail": detail, "at_ms": at, "source": source, "event_id": eid}
    if sev:
        e["severity"] = sev
    return e


def takeover(tid, det, verified, fresh=True, audio="pending"):
    return {
        "id": tid, "live": True, "rehearsal": False,
        "trigger": "unknown_usb" if tid.startswith("usb:") else "correlated_incident",
        "detected_at_ms": det, "fresh": fresh,
        "local_mode": "CRITICAL" if verified else "WATCHFUL", "local_mode_verified": verified,
        "headline": "Unauthorized device activity detected",
        "sentence": "MOCK: an unknown USB device connected 4.0 s after the Jetson reported a person at this desk.",
        "sentence_source": "local_timing" if verified else "spacetimedb_summary",
        "evidence": [
            ev("person", "Person at desk", "MOCK · Jetson jetson-01 · 91% confidence", det - 4000, "remote_camera", "mock-person"),
            ev("usb", "Unknown USB device connected", "MOCK · Unknown USB mass storage · 0781:5583", det, "mac_usb_watcher", "mock-usb"),
            ev("incident", "Incident opened", "correlated_usb_physical · HIGH (server) · SUSPECTED_INTRUSION", det + 400, "spacetimedb", "incident:mock", "high"),
            ev("scope", "Workstation and session", "mock-mbp · session-mock", None, "local"),
            ev("sensors", "Sensor health", "MOCK · Camera live · USB watcher running · SpacetimeDB connected", None, "backend"),
        ],
        "response": {
            "audio": {"state": audio, "detail": "MOCK · Kokoro · audio %s" % ("queued" if audio == "pending" else "420 ms after the event")},
            "tools": {"state": "frozen", "detail": "MOCK · protected tools frozen for this session"},
            "lock": {"state": "dry_run", "detail": "Dry run: the screen was not locked"},
            "monitoring": {"state": "running", "detail": "MOCK · camera, USB watcher and SpacetimeDB running"},
            "nemotron": {"state": "available", "detail": "MOCK · investigator reachable on 127.0.0.1:18787"},
            "evidence": {"state": "recorded", "detail": "MOCK · recorded in SpacetimeDB (2 evidence ids)"},
        },
        "incident_id": "incident:mock" if verified else "incident:mock-2",
        "acknowledged_at_ms": None, "shown_at_ms": None, "shown_latency_ms": None,
    }


def decision():
    t = time.time() - T0
    base = {"now_ms": now_ms(), "workstation_id": "mock-mbp", "blocked": [], "native": None, "handoff": None}
    if t < 1.0:
        return "A_armed_idle", 200, dict(base, armed=True, session_id="session-S1", active=False, takeover=None, blocked=["no_critical_evidence"])
    if t < 2.0:
        return "B_live_takeover", 200, dict(base, armed=True, session_id="session-S1", active=True, takeover=takeover("usb:mock-1", DET1, True))
    if t < 2.6:
        return "C_backend_503", 503, {"detail": "mock outage"}
    if t < 3.2:
        return "D_same_id_updated", 200, dict(base, armed=True, session_id="session-S1", active=True, takeover=takeover("usb:mock-1", DET1, True, audio="played"))
    if t < 4.2:
        return "E_disarmed", 200, dict(base, armed=False, session_id=None, active=False, takeover=None, blocked=["not_armed"])
    if t < 5.0:
        return "F_unverified_takeover", 200, dict(base, armed=True, session_id="session-S2", active=True, takeover=takeover("incident:mock-2", DET2, False))
    if t < 6.0:
        r = takeover("incident:mock-2", DET2, False)
        r["acknowledged_at_ms"] = int((T0 + 5.0) * 1000)
        return "G_acknowledged_elsewhere", 200, dict(base, armed=True, session_id="session-S2", active=True, takeover=r)
    if t < 7.0:
        return "H_same_id_ack_lost", 200, dict(base, armed=True, session_id="session-S2", active=True, takeover=takeover("incident:mock-2", DET2, False))
    if t < 8.0:
        return "I_stale", 200, dict(base, armed=True, session_id="session-S2", active=False, takeover=takeover("usb:mock-3", now_ms() - 200000, True, fresh=False), blocked=["stale"])
    if t < 9.0:
        return "J_404", 404, {"detail": "Not Found"}
    return "K_armed_idle", 200, dict(base, armed=True, session_id="session-S2", active=False, takeover=None)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        if self.headers.get("Origin") is not None:
            return 403
        tok = self.headers.get("X-Locki-Alert-Token") or ""
        return 200 if hmac.compare_digest(tok, state["token"] or "") else 401

    def do_GET(self):
        if self.path != "/api/security/takeover":
            return self._send(404, {"detail": "Not Found"})
        with lock:
            if time.time() - T0 >= 9.0 and not state.get("rotated"):
                state["rotated"] = True
                write_token()
            phase, code, body = decision()
            auth = self._auth()
            state["gets"] += 1
            if state["get_headers"] is None:
                state["get_headers"] = {k: self.headers.get(k) is not None for k in ("X-Locki-Alert-Token", "X-Locki-Alert-Pid", "X-Locki-Alert-Version", "Origin")}
            key = (phase, auth if code != 404 and code != 503 else code)
            if not state["phases_seen"] or state["phases_seen"][-1]["phase"] != phase or state["phases_seen"][-1]["status"] != (code if auth == 200 else auth):
                state["phases_seen"].append({"phase": phase, "status": code if auth == 200 else auth, "t": round(time.time() - T0, 3)})
        if code in (404, 503):
            return self._send(code, body)
        if auth != 200:
            return self._send(auth, {"detail": "unauthorized" if auth == 401 else "origin refused"})
        tk = body.get("takeover")
        if tk and tk["id"] in state["shown"]:
            tk["shown_at_ms"], tk["shown_latency_ms"] = state["shown"][tk["id"]]
        return self._send(200, body)

    def do_POST(self):
        if self.path != "/api/security/takeover/report":
            return self._send(404, {"detail": "Not Found"})
        auth = self._auth()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {"_unparseable": raw.decode(errors="replace")}
        with lock:
            state["posts"].append({"t": round(time.time() - T0, 3), "auth": auth, "body": body})
            if auth == 200 and body.get("event") == "shown":
                state["shown"][body.get("takeover_id")] = (body.get("at_ms"), body.get("latency_ms"))
        if auth != 200:
            return self._send(auth, {"detail": "unauthorized"})
        return self._send(200, {"ok": True})


srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(args.duration)
srv.shutdown()
with open(args.out, "w") as f:
    json.dump({k: v for k, v in state.items() if k != "token"}, f, indent=1, default=str)
print("mock done: %d GETs, %d POSTs -> %s" % (state["gets"], len(state["posts"]), args.out))
