"""End-to-end check of the real HTTP API with the real local Nemotron.

Starts `python -m nemotron_investigator serve` on 127.0.0.1 (temporary key
file under var/), drives it over real HTTP, records every response, stops it.
Output: bench/results/api_live_check.json

  python bench/api_live_check.py [--port 8787]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from nemotron_investigator.auth import add_key  # noqa: E402

A_ID = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "A_unknown_usb_armed.json")
                  .read_text())["tables"]["incidents"][0]["id"]


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(port: int) -> dict:
    if _port_in_use(port):
        # Never test someone else's server by accident (e.g. the running service on 8787).
        raise SystemExit(f"port {port} is already in use; pass --port with a free port")
    keys_file = ROOT / "var" / "api_live_check_keys.json"
    keys_file.unlink(missing_ok=True)
    dhruv = add_key(keys_file, "dhruv-m4-backend", ["ws:fixture-a"])
    other = add_key(keys_file, "other-requester", ["ws:fixture-b"])
    env = {**os.environ, "NI_API_KEYS_FILE": str(keys_file), "NI_BIND_PORT": str(port),
           "NI_STORE_DIR": str(ROOT / "var" / "api_live_check_store"), "NI_EVIDENCE_BACKEND": "fixture"}
    log = open(ROOT / "var" / "api_live_check_server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "nemotron_investigator", "serve"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    H = {"Authorization": f"Bearer {dhruv}"}
    out: dict = {"base": base, "steps": []}

    def rec(name, r, t0):
        try:
            body = r.json()
        except ValueError:
            body = r.text[:300]
        out["steps"].append({"step": name, "status_code": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                             "x_evidence_mode": r.headers.get("X-Evidence-Mode"), "body": body})
        return body

    try:
        for _ in range(100):
            try:
                httpx.get(f"{base}/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        with httpx.Client(base_url=base, timeout=10) as c:
            t = time.perf_counter(); rec("health_no_auth", c.get("/health"), t)
            t = time.perf_counter(); rec("health", c.get("/health", headers=H), t)
            t = time.perf_counter(); rec("create_bad_incident_id", c.post("/investigations", headers=H,
                                                                       json={"incident_id": "incident:nope"}), t)
            t = time.perf_counter()
            created = rec("create", c.post("/investigations", headers=H, json={
                "incident_id": A_ID, "requester": {"identity": "dhruv-m4-backend"},
                "context": {"note": "Owner asked by voice: what happened at my desk?"}}), t)
            inv = created["investigation_id"]
            t_poll = time.perf_counter()
            polls = 0
            while True:
                polls += 1
                r = c.get(f"/investigations/{inv}", headers=H)
                if r.json()["status"] not in ("queued", "running"):
                    break
                time.sleep(0.5)
            body = rec("poll_until_done", r, t_poll)
            out["polls"] = polls
            t = time.perf_counter(); rec("voice_view", c.get(f"/investigations/{inv}?view=voice", headers=H), t)
            t = time.perf_counter(); rec("other_requester_cannot_read",
                                         c.get(f"/investigations/{inv}", headers={"Authorization": f"Bearer {other}"}), t)
            t = time.perf_counter()
            o = rec("other_requester_wrong_workstation", c.post("/investigations",
                                                                headers={"Authorization": f"Bearer {other}"},
                                                                json={"incident_id": A_ID}), t)
            for _ in range(40):
                r = c.get(f"/investigations/{o['investigation_id']}", headers={"Authorization": f"Bearer {other}"})
                if r.json()["status"] not in ("queued", "running"):
                    break
                time.sleep(0.25)
            out["other_requester_result_status"] = r.json()["status"]
            res = body.get("result") or {}
            voice = out["steps"][-3]["body"] if len(out["steps"]) >= 3 else {}
            out["summary"] = {"status": body.get("status"), "tools": [x["tool"] for x in res.get("tool_execution_trace", [])],
                              "total_latency_ms": res.get("total_latency_ms"),
                              "official_severity": (res.get("official_severity") or {}).get("level"),
                              "nemotron_risk": (res.get("risk_assessment") or {}).get("level"),
                              "effective_severity": (res.get("effective_severity") or {}).get("level"),
                              "evidence_mode": res.get("evidence_mode"), "evidence_badge": res.get("evidence_badge"),
                              "x_evidence_mode_header": out["steps"][-3].get("x_evidence_mode") if len(out["steps"]) >= 3 else None,
                              "evidence_live": (res.get("evidence_provenance") or {}).get("live"),
                              "voice_spoken_summary": voice.get("spoken_summary") if isinstance(voice, dict) else None}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    res = main(ap.parse_args().port)
    dest = ROOT / "bench" / "results" / "api_live_check.json"
    dest.write_text(json.dumps(res, indent=2, default=str))
    for s in res["steps"]:
        b = s["body"]
        brief = b.get("status") or b.get("detail") or b.get("service") if isinstance(b, dict) else str(b)[:80]
        print(f"{s['step']:<36} HTTP {s['status_code']}  {s['ms']:>8} ms  {brief}")
    print("summary:", res.get("summary"), "| other requester's investigation:", res.get("other_requester_result_status"))
    print(f"-> {dest}")
