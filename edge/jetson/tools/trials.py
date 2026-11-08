"""Consented human trials for the desk-zone detector (PRD: 20-40 cases).

Record one trial (run while the service is running on the same machine):
    python3 tools/trials.py trial --label at_desk        # person sits/reaches at the keyboard  (positive)
    python3 tools/trials.py trial --label walk_by        # person walks past, does not touch desk (negative)
    python3 tools/trials.py trial --label empty          # nobody in view                          (negative)

Score them all:
    python3 tools/trials.py report --out ../../docs/hardware/trials-report.md

A trial is DETECTED if a person_in_zone event has observed_at inside [start, end + grace].
Latency is trial start (operator presses Enter as the volunteer starts moving) -> event,
so it includes walking time. It is not pure model latency.
"""
import argparse
import datetime
import json
import os
import sys
import time

POSITIVE = ("at_desk",)
NEGATIVE = ("walk_by", "empty")
# The runtime (python3 -m locki_edge.runtime) audits to AIRLOCK_STATE_DIR/events.jsonl; LOCKI_STATE_DIR is the
# legacy service's directory. Run this as the same user as the runtime/service.
STATE = os.path.expanduser(os.environ.get("AIRLOCK_STATE_DIR") or os.environ.get("LOCKI_STATE_DIR")
                           or "~/.local/state/locki-edge")


def event_ms(e):
    """Epoch ms of an audit record: runtime events carry ISO 'observed_at' (+ observed_at_ms when local-only)."""
    v = e.get("observed_at_ms", e.get("observed_at"))
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.endswith("Z"):
        try:
            dt = datetime.datetime.strptime(v[:-1], "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            try:
                dt = datetime.datetime.strptime(v[:-1], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
        return int(round(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000))
    return None


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def cmd_trial(a):
    if a.label not in POSITIVE + NEGATIVE:
        sys.exit("label must be one of %s" % (POSITIVE + NEGATIVE,))
    trials_path = os.path.join(a.state_dir, "trials.jsonl")
    n = len(load_jsonl(trials_path)) + 1
    try:
        input("Trial %d [%s, %ds]. Press Enter the moment the volunteer starts... " % (n, a.label, a.seconds))
    except EOFError:
        pass
    start = int(time.time() * 1000)
    for left in range(a.seconds, 0, -1):
        sys.stdout.write("\r  recording... %2ds " % left)
        sys.stdout.flush()
        time.sleep(1)
    end = int(time.time() * 1000)
    rec = {"trial": n, "label": a.label, "start_ms": start, "end_ms": end, "note": a.note or ""}
    with open(trials_path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("\r  saved trial %d (%s)          " % (n, a.label))


def score(trials, events, grace_ms):
    # Detection is what the camera saw: a local-only person_in_zone (workstation disarmed) still counts.
    persons = [(event_ms(e), e) for e in events if e.get("kind") == "person_in_zone"]
    persons = [(ms, e) for ms, e in persons if ms is not None]
    rows = []
    for t in trials:
        hits = [(ms, e) for ms, e in persons if t["start_ms"] <= ms <= t["end_ms"] + grace_ms]
        first_ms, first = min(hits, key=lambda h: h[0]) if hits else (None, None)
        md = (first or {}).get("metadata") or {}
        rows.append({
            "trial": t["trial"], "label": t["label"], "detected": bool(hits),
            "latency_ms": (first_ms - t["start_ms"]) if first else None,
            "detector": md.get("detector") or md.get("provider"),
            "confidence": (first or {}).get("confidence"),
            "sent": None if first is None else ("local:%s" % first.get("reason") if first.get("local_only") else "sent"),
        })
    return rows


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round((len(s) - 1) * p / 100.0)))]


def summarize(rows):
    pos = [r for r in rows if r["label"] in POSITIVE]
    neg = [r for r in rows if r["label"] in NEGATIVE]
    tp = sum(r["detected"] for r in pos)
    fp = sum(r["detected"] for r in neg)
    lat = [r["latency_ms"] for r in pos if r["latency_ms"] is not None]
    return {
        "n": len(rows), "positives": len(pos), "negatives": len(neg), "tp": tp, "fn": len(pos) - tp,
        "fp": fp, "tn": len(neg) - fp,
        "recall": round(tp / float(len(pos)), 3) if pos else None,
        "false_alert_rate": round(fp / float(len(neg)), 3) if neg else None,
        "false_alerts_per_20_benign": round(20.0 * fp / len(neg), 2) if neg else None,
        "latency_p50_ms": pct(lat, 50), "latency_p95_ms": pct(lat, 95),
    }


def cmd_report(a):
    trials = load_jsonl(os.path.join(a.state_dir, "trials.jsonl"))
    events = load_jsonl(os.path.join(a.state_dir, "events.jsonl"))
    if not trials:
        sys.exit("no trials recorded yet")
    rows = score(trials, events, a.grace_ms)
    s = summarize(rows)
    lines = ["# Desk-zone detector trials", "",
             "Generated %s from %d consented trials. Detector(s): %s." % (
                 time.strftime("%Y-%m-%d %H:%M"), s["n"],
                 ", ".join(sorted(set(r["detector"] for r in rows if r["detector"]))) or "n/a"), "",
             "| Metric | Result | PRD target |", "|---|---|---|",
             "| Positives detected (recall) | %s/%s (%s) | >= 90%% |" % (s["tp"], s["positives"], s["recall"]),
             "| False alerts on benign trials | %s/%s (%s per 20) | <= 1 per 20 |" % (
                 s["fp"], s["negatives"], s["false_alerts_per_20_benign"]),
             "| Trial start -> event p50 / p95 | %s / %s ms | (includes walking time) |" % (
                 s["latency_p50_ms"], s["latency_p95_ms"]),
             "", "Small sample: treat these as a hackathon measurement, not a benchmark.", "",
             "| # | Label | Detected | Latency ms | Detector | Confidence | DB |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (r["trial"], r["label"], "yes" if r["detected"] else "no",
                                                            r["latency_ms"], r["detector"], r["confidence"],
                                                            r["sent"] or "-"))
    text = "\n".join(lines) + "\n"
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    print(text)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", default=STATE)
    sub = p.add_subparsers(dest="cmd")
    t = sub.add_parser("trial")
    t.add_argument("--label", required=True)
    t.add_argument("--seconds", type=int, default=8)
    t.add_argument("--note")
    r = sub.add_parser("report")
    r.add_argument("--grace-ms", type=int, default=1500)
    r.add_argument("--out")
    a = p.parse_args(argv)
    if a.cmd == "trial":
        cmd_trial(a)
    elif a.cmd == "report":
        cmd_report(a)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
