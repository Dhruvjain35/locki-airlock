"""Camera-to-voice latency of the running backend's current session, from its own records (GET /api/security/latency).

  .venv/bin/python scripts/airlock_latency_report.py

For each presence warning: arrival (the first frame with the person) to person_in_zone, the settling frame to audio
start, plus the speaker's own reported output latency (an estimate of when it is audible), and arrival to audio.
Arrivals are person_in_zone events after the view was clear; a missed arrival got no warning; a warning with no
arrival behind it is a false alarm. Nothing here is simulated: an empty report means nothing happened yet.
"""

from __future__ import annotations

import json
import sys
import urllib.request


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


def main() -> int:
    data = json.load(urllib.request.urlopen("http://127.0.0.1:8077/api/security/latency", timeout=5))
    events = data["camera_events"]
    arrivals, clear = [], True
    for e in events:
        if e["kind"] == "zone_clear":
            clear = True
        elif e["kind"] == "person_in_zone" and clear:
            arrivals.append(e)
            clear = False
    presence = [w for w in data["warnings"] if w.get("reason") == "person_at_desk"]
    by_event = {w["event_id"]: w for w in presence}
    rows, settle, audio, audible, arrival_audio = [], [], [], [], []
    for a in arrivals:
        w = by_event.get(a["event_id"])
        m = a["metadata"]
        presence_ms = (m.get("dwell_ms") or 0) + (m.get("capture_to_event_ms") or 0)
        settle.append(presence_ms)
        if w is None or not (w.get("audio") or {}).get("to_audio_ms"):
            rows.append(f"  arrival {a['event_id'][:8]}: person_in_zone {presence_ms:.0f} ms after arrival; NO WARNING AUDIO")
            continue
        au = w["audio"]
        a_ms, dev = float(au["to_audio_ms"]), au.get("output_latency_ms")
        audio.append(a_ms)
        if dev is not None:
            audible.append(a_ms + float(dev))
        timing = w.get("timing") or {}
        arr = a_ms + (timing["frame_at_ms"] - timing["started_at_ms"]) if timing.get("frame_at_ms") and timing.get("started_at_ms") else None
        if arr is not None:
            arrival_audio.append(arr)
        rows.append(f"  arrival {a['event_id'][:8]}: person_in_zone {presence_ms:.0f} ms after arrival; frame to audio start {a_ms:.0f} ms"
                    + (f"; +{dev:.0f} ms device = audible about {a_ms + dev:.0f} ms" if dev is not None else "")
                    + (f"; arrival to audio {arr:.0f} ms" if arr is not None else "") + f"; source {au.get('source')}")
    false_alarms = [w for w in presence if w["event_id"] not in {a["event_id"] for a in arrivals}]
    print(f"armed: {data['armed']}; arrivals {len(arrivals)}, presence warnings {len(presence)}, missed {sum(1 for r in rows if 'NO WARNING' in r)}, "
          f"warnings without an arrival {len(false_alarms)}")
    print("\n".join(rows) or "  nothing yet")
    for name, v in (("arrival to person_in_zone", settle), ("frame to audio start", audio), ("frame to audible (est.)", audible), ("arrival to audio start", arrival_audio)):
        print(f"{name:28} p50 {pct(v, 0.5)}  p95 {pct(v, 0.95)}  max {max(v) if v else None}  n {len(v)}")
    print("mode changes: " + ", ".join(f"{t['from']}->{t['to']} ({t['reason']})" for t in data["transitions"][-12:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
