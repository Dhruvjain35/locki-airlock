"""Record a live conversation trial with a real person, from the running backend's own event stream. Read-only: it never sends a
command, never plays audio and never touches the microphone; it only listens to ws://127.0.0.1:8077/ws while you talk to LOCKI.

    cd backend && .venv/bin/python scripts/conversation_trial.py --minutes 10 --label "20 turns, quiet room"

Ctrl-C ends early. The report (P50 / P95 / count per measure, plus every event's time) is written to data/airlock_runs/ and printed.
It holds no audio and no transcript text, only intents, reasons and timings.

What each number means (all from the backend's real clocks):
  stt_ms                   end of the turn's audio -> transcript (speech-to-text time)
  reply_to_audio_ms        end of the owner's speech -> LOCKI's first audio sample leaves the process (a turn's trace)
  audible_reply_ms         reply_to_audio_ms + the output device's measured latency (Bluetooth buffering included)
  security_to_audio_ms     the observation that triggered a warning -> its first audio (cross-clock for a Jetson row)
  barge_in_to_stop_ms      barge-in detected -> the interrupted line reports finished
Counts: live-microphone turns and recorded turns (kept apart), barge-ins, interrupted lines, echo rejections, fragments ignored while LOCKI talked, unfinished turns joined, lines kept
silent (by reason), security mode changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 1)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": round(statistics.median(values), 1) if values else None, "p95": pct(values, 0.95), "max": max(values) if values else None}


class Trial:
    def __init__(self):
        self.t0 = time.time()
        self.timeline: list[dict] = []
        self.stt: list[float] = []
        self.reply: list[float] = []
        self.audible: list[float] = []
        self.security: list[float] = []
        self.barge_stop: list[float] = []
        self.counts: Counter = Counter()
        self.silenced: Counter = Counter()
        self._barge_at: float | None = None

    def event(self, ev: dict) -> None:
        kind, p, at = ev.get("event_type"), ev.get("payload") or {}, time.time()
        keep = {"t": round(at - self.t0, 3), "event": kind}
        if kind == "voice.speech_started":
            self.counts["speech_onsets"] += 1
        elif kind == "voice.transcribed" and p.get("source") == "recorded":
            self.counts["recorded_turns"] += 1  # a recorded WAV, never counted as a live-microphone turn
            keep["source"] = "recorded"
        elif kind == "voice.transcribed" and p.get("source") == "voice":
            self.counts["turns"] += 1
            if p.get("latency_ms") is not None:
                self.stt.append(float(p["latency_ms"]))
                keep["stt_ms"] = p["latency_ms"]
        elif kind == "voice.command_rejected":
            reason = str(p.get("reason") or "")
            key = "echo_rejected" if "echo" in reason else "fragment_ignored_while_talking" if "talking" in reason else "rejected_other"
            self.counts[key] += 1
            keep["reason"] = key
        elif kind == "voice.barge_in":
            self.counts["barge_ins"] += 1
            self._barge_at = at
            keep.update({k: p.get(k) for k in ("over_echo_db", "speech_prob")})
        elif kind == "voice.silenced":
            self.silenced[str(p.get("reason"))] += 1
            keep.update({"category": p.get("category"), "reason": p.get("reason")})
        elif kind == "security.mode":
            self.counts["mode_changes"] += 1
            keep.update({"from": p.get("from"), "to": p.get("to"), "reason": p.get("reason")})
        elif kind == "voice.tts_finished":
            trace, outcome = p.get("trace") or {}, p.get("outcome")
            keep.update({"category": p.get("category"), "outcome": outcome, "to_audio_ms": p.get("to_audio_ms"), "output_latency_ms": p.get("output_latency_ms"),
                         "trace": trace.get("kind")})
            if outcome and "interrupt" in str(outcome):
                self.counts["lines_interrupted"] += 1
                if self._barge_at is not None:
                    self.barge_stop.append(round((at - self._barge_at) * 1000, 1))
                    self._barge_at = None
            to_audio, out = p.get("to_audio_ms"), p.get("output_latency_ms")
            if to_audio is not None:
                if trace.get("kind") == "turn":
                    self.reply.append(float(to_audio))
                    if out is not None:
                        self.audible.append(float(to_audio) + float(out))
                elif trace.get("kind") == "security":
                    self.security.append(float(to_audio))
        else:
            return
        self.timeline.append(keep)

    def report(self, label: str) -> dict:
        return {
            "label": label, "started_at": self.t0, "duration_s": round(time.time() - self.t0, 1), "source": "live backend event stream (read-only)",
            "stt_ms": summary(self.stt), "reply_to_audio_ms": summary(self.reply), "audible_reply_ms": summary(self.audible),
            "security_to_audio_ms": summary(self.security), "barge_in_to_stop_ms": summary(self.barge_stop),
            "counts": dict(self.counts), "silenced": dict(self.silenced), "timeline": self.timeline,
        }


async def run(url: str, minutes: float, label: str) -> dict:
    import websockets

    trial = Trial()
    deadline = time.monotonic() + minutes * 60
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            print(f"recording from {url} for up to {minutes:g} min. Talk to LOCKI now; Ctrl-C ends.")
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "event":
                    trial.event(msg.get("data") or {})
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return trial.report(label)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://127.0.0.1:8077/ws")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--label", default="live conversation trial")
    args = ap.parse_args()
    try:
        report = asyncio.run(run(args.url, args.minutes, args.label))
    except KeyboardInterrupt:
        return
    out = ROOT / "data" / "airlock_runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-conversation-trial.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "timeline"}, indent=2))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
