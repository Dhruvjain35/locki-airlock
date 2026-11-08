"""LOCKI AIRLOCK readiness: what is actually working right now, checked against the real services. Never a guess.

Run with the system Python (stdlib only):  /usr/bin/python3 -I readiness.py [--json]
READY only when every required check passes. Each failure says what to do. The investigator key is read here and sent only
to the loopback tunnel; it is never printed.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BACKEND = "http://127.0.0.1:8077"
FRONTEND = "http://127.0.0.1:5174"
INVESTIGATOR = "http://127.0.0.1:18787"
OLLAMA = "http://127.0.0.1:11435"
MODEL = os.environ.get("LOCKI_NEMOTRON_MODEL", "nemotron-3.5-bolt:e4b")
KEY_FILE = os.path.expanduser(os.environ.get("LOCKI_INVESTIGATOR_KEY_FILE", "~/.locki/investigator.key"))
JETSON = os.environ.get("LOCKI_JETSON_DEVICE", "jetson-01")
DATABASE = os.environ.get("LOCKI_SPACETIME_DATABASE", "locki-w91bw")
FRESH_MS = 30_000


def http(url, method="GET", body=None, headers=None, timeout=4.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={**({"content-type": "application/json"} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(1_000_000)
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read(100_000)
    except Exception as e:  # refused, timeout, DNS
        return None, str(e).encode()


def item(name, ok, detail, fix=None, required=True, group="M4"):
    return {"name": name, "ok": bool(ok), "required": required, "detail": detail, "fix": None if ok else fix, "group": group}


def check_backend_and_mac():
    out = []
    code, raw = http(f"{BACKEND}/api/health")
    if code != 200:
        return [item("M4 backend", False, f"not answering on 127.0.0.1:8077 ({code or raw.decode(errors='replace')[:80]})",
                     "Press Retry. If it stays down, see the runbook: Backend did not start.")], None
    out.append(item("M4 backend", True, "answering on 127.0.0.1:8077"))
    code, raw = http(f"{BACKEND}/api/security")
    sec = json.loads(raw) if code == 200 else {}
    st = sec.get("spacetimedb") or {}
    out.append(item("SpacetimeDB (Mac publisher)", st.get("connected") and st.get("authorized"),
                    f"{st.get('database', '?')} · {'connected' if st.get('connected') else 'not connected'} · {'authorized' if st.get('authorized') else 'not authorized'}",
                    "Check the Internet connection (maincloud needs it), then Retry."))
    mon = sec.get("monitor") or {}
    usb, apps = mon.get("usb") or {}, mon.get("apps") or {}
    out.append(item("USB and app watchers", mon.get("running") and not usb.get("error") and not apps.get("error"),
                    f"USB {usb.get('method', '?')} · {usb.get('connected', '?')} devices · apps: visible launches only",
                    f"Watcher error: {usb.get('error') or apps.get('error') or 'not running'}. Retry; if it persists restart LOCKI while disarmed."))
    out.append(item("Screen lock is dry run", sec.get("lock_mode") == "dry_run", f"lock mode {sec.get('lock_mode')}",
                    "The backend is not in dry_run. Stop and relaunch with the launcher (it never enables real locks)."))
    sess = sec.get("session") or {}
    out.append(item("Protection", True, f"ARMED · session {sess.get('session_id')}" if sec.get("armed") else "disarmed · press START PROTECTION in the dashboard",
                    required=False))
    return out, sec


def check_frontend():
    code, raw = http(f"{FRONTEND}/")
    ok = code == 200 and b"LOCKI AIRLOCK" in raw
    return [item("Dashboard", ok, "serving on 127.0.0.1:5174" if ok else f"not serving ({code})", "Press Retry; the launcher starts it if missing.")]


def check_jetson():
    """jetson-01's own heartbeat in SpacetimeDB, read with the owner's SpacetimeDB CLI (read-only)."""
    cli = os.path.expanduser("~/.local/bin/spacetime")
    try:
        r = subprocess.run([cli, "sql", "-s", "maincloud", DATABASE, "SELECT device_id, component, status, last_seen_ms FROM my_device_health"],
                           capture_output=True, text=True, timeout=8)
    except Exception as e:
        return [item("Jetson camera (jetson-01)", False, f"heartbeat could not be read ({type(e).__name__})", "Check the Internet connection, then Retry.", group="Jetson")]
    row = next((ln for ln in r.stdout.splitlines() if f'"{JETSON}"' in ln and '"camera"' in ln), None)
    if r.returncode != 0 or row is None:
        return [item("Jetson camera (jetson-01)", False, "no heartbeat row visible", "Check the owner SpacetimeDB login on this Mac (spacetime login), then Retry.", group="Jetson")]
    cells = [c.strip().strip('"') for c in row.split("|")]
    status, seen = cells[2], int(re.sub(r"\D", "", cells[3]) or 0)
    age = time.time() * 1000 - seen
    ok = status == "online" and age <= FRESH_MS
    return [item("Jetson camera (jetson-01)", ok, f"{status} · last heartbeat {age / 1000:.0f} s ago",
                 "Jetson offline: check it is powered on and on the network (its publisher starts at boot). Detection of people is unavailable until it reconnects.",
                 group="Jetson")]


def check_investigator():
    try:
        key = open(KEY_FILE).read().strip()
    except OSError:
        return [item("Nemotron investigator (M5)", False, "investigator key file missing", "Put Mustafa's key in ~/.locki/investigator.key (chmod 600).", group="M5")]
    code, raw = http(f"{INVESTIGATOR}/health", headers={"Authorization": f"Bearer {key}"})
    del key
    if code != 200:
        return [item("Nemotron investigator (M5)", False, f"no authenticated answer through the tunnel ({code or raw.decode(errors='replace')[:60]})",
                     "Explain unavailable. The tunnel restores itself within ~15 s; check the M5 is awake and online, then Retry.", group="M5")]
    h = json.loads(raw)
    ev, ol = h.get("evidence_backend") or {}, h.get("ollama") or {}
    ok = h.get("service") == "ok" and not h.get("degraded") and ev.get("live") and ev.get("reachable") and ol.get("model_installed")
    return [item("Nemotron investigator (M5)", ok,
                 f"service {h.get('service')} · live evidence {'reachable' if ev.get('reachable') else 'unreachable'} · model {ol.get('model', '?')} {'installed' if ol.get('model_installed') else 'missing'}"
                 + (f" · degraded: {', '.join(map(str, h['degraded']))}" if h.get("degraded") else ""),
                 "Explain unavailable: the investigator reports a problem. Ask Mustafa to check the M5 service.", group="M5")]


def check_model_answers():
    t0 = time.monotonic()
    code, raw = http(f"{OLLAMA}/api/generate", "POST", {"model": MODEL, "prompt": "Reply with the single word OK.", "stream": False,
                                                         "options": {"num_predict": 2, "temperature": 0}}, timeout=25)
    ms = (time.monotonic() - t0) * 1000
    try:
        text = json.loads(raw).get("response", "") if code == 200 else ""
    except ValueError:
        text = ""
    ok = code == 200 and isinstance(text, str)
    return [item("Nemotron model answers", ok, f"{MODEL} answered in {ms / 1000:.1f} s" if ok else f"no answer ({code or raw.decode(errors='replace')[:60]})",
                 "Explain unavailable: the M5 model did not answer. Check the M5 is awake; Retry.", group="M5")]


def check_audio():
    out = []
    code, raw = http(f"{BACKEND}/api/voice/devices")
    name = None
    if code == 200:
        d = json.loads(raw)
        name = next((o["name"] for o in d.get("outputs", []) if o.get("default")), None)
    jbl = bool(name and "jbl" in name.lower())
    out.append(item("Speaker", jbl, f"LOCKI speaks on: {name or 'unknown'}",
                    "JBL not connected: speech will play on the Mac. Turn the JBL Go 3 on (it reconnects and becomes the output automatically).",
                    required=False, group="Audio"))
    try:
        vs = subprocess.run(["/usr/bin/osascript", "-e", "get volume settings"], capture_output=True, text=True, timeout=4).stdout
        muted = "output muted:true" in vs
        vol = int(re.search(r"output volume:(\d+)", vs).group(1))
        out.append(item("Mac output volume", not muted and vol >= 25, f"volume {vol}{' · MUTED' if muted else ''}",
                        "Unmute and turn the volume up (F12 / volume keys), or nobody will hear LOCKI's warnings.", group="Audio"))
    except Exception:
        out.append(item("Mac output volume", False, "could not read the volume", "Check the volume manually.", required=False, group="Audio"))
    return out


def run():
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(f) for f in (check_backend_and_mac, check_frontend, check_jetson, check_investigator, check_model_answers, check_audio)]
        results = [f.result() for f in futs]
    items = results[0][0] + [x for r in results[1:] for x in r]
    required_bad = [i for i in items if i["required"] and not i["ok"]]
    detection_ok = all(i["ok"] for i in items if i["group"] in ("M4", "Jetson") and i["required"])
    explain_ok = all(i["ok"] for i in items if i["group"] == "M5")
    state = "READY" if not required_bad else "DETECTION ONLY" if detection_ok and not explain_ok and all(i["ok"] for i in items if i["group"] == "Audio" and i["required"]) else "NOT READY"
    # Waiting helps only for things that come up on their own (services starting, tunnel reconnecting, heartbeats); a muted Mac or a
    # wrong lock mode needs a person, so the launcher reports those at once instead of waiting.
    waitable = [i for i in required_bad if i["name"] not in ("Mac output volume", "Screen lock is dry run")]
    return {"state": state, "ready": not required_bad, "worth_waiting": bool(waitable), "detection_ready": detection_ok, "explain_ready": explain_ok,
            "checked_in_ms": round((time.monotonic() - t0) * 1000), "at": time.strftime("%H:%M:%S"), "items": items}


def summary(r):
    lines = [f"{r['state']}  (checked in {r['checked_in_ms'] / 1000:.1f} s at {r['at']})"]
    for i in r["items"]:
        mark = "OK " if i["ok"] else ("!! " if i["required"] else "-- ")
        lines.append(f"{mark}{i['name']}: {i['detail']}")
        if i["fix"]:
            lines.append(f"     → {i['fix']}")
    return "\n".join(lines)


if __name__ == "__main__":
    res = run()
    print(json.dumps(res) if "--json" in sys.argv else summary(res))
    sys.exit(0 if res["ready"] else 1)
