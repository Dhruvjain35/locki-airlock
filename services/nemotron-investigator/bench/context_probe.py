"""Measure what the shared Nemotron runner actually retains from long prompts.

Background: /api/ps reports context_length 4096 for the loaded runner (Ollama's
VRAM-based default; we never send num_ctx), yet real investigation prompts reach
about 5,000 tokens. This probe checks retention directly instead of assuming it.

Method (all real model calls, nothing simulated):
  * Build a multi-message chat (system, user/assistant turns, final user turn) of
    a target size from neutral filler text.
  * Plant a fresh random verification code at five positions:
      S  - start of the system message (survives message-level truncation that
           keeps system messages; lost under token-level eviction of early tokens)
      U1 - start of the first user message (lost under either truncation style)
      M1 - about one third of the way in
      M2 - about two thirds of the way in
      E  - the final user message (should always be recalled)
  * Ask the model to list the codes; score exact matches per position.
  * Record prompt_eval_count (tokens the runner actually evaluated) and /api/ps
    context_length and size_vram after every call.

The request sends no num_ctx and no keep_alive, so it never reconfigures or
reloads the shared runner. Codes are random per trial, and each prompt starts
with its own code, so no prefix-cache reuse can answer for the model.

  python bench/context_probe.py [--sizes 3000,4600,6900,10000] [--trials 2]
Output: bench/results/context_probe_<UTC>.json (synthetic data only).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OLLAMA = "http://127.0.0.1:11434"
MODEL = "nemotron-3.5-bolt:e4b"
LABELS = ("S", "U1", "M1", "M2", "E")

_W1 = ["AMBER", "BISON", "CEDAR", "DELTA", "EMBER", "FJORD", "GECKO", "HAZEL", "IGLOO", "JUNCO",
       "KAYAK", "LOTUS", "MAPLE", "NOVA", "OPAL", "PIXEL", "QUILL", "RAVEN", "SIERRA", "TULIP",
       "UMBRA", "VIOLA", "WALRUS", "XENON", "YUCCA", "ZEPHYR"]
_PLACES = ["corridor", "loading bay", "server room", "atrium", "stairwell", "parking deck", "lab annex",
           "mail room", "west wing", "roof access", "break room", "lobby"]
_SUBJ = ["humidity sensor", "badge reader", "ceiling light", "air handler", "fire panel", "water meter",
         "door closer", "thermostat", "smoke detector", "elevator controller"]
_VERB = ["logged routine readings", "passed its weekly self-test", "was inspected by facilities",
         "reported nominal status", "was cleaned and recalibrated", "showed no faults",
         "completed a firmware check", "was photographed for the asset register"]
_TAIL = ["and no follow-up was required.", "and the inspector signed the sheet.",
         "with all values inside the normal band.", "before the afternoon shift began.",
         "and the ticket was closed the same day.", "while the area stayed open to staff."]


def code(rng: random.Random) -> str:
    return f"{rng.choice(_W1)}-{rng.randint(1000, 9999)}-{rng.choice(_W1)}"


def filler(rng: random.Random, n_chars: int) -> str:
    out, size = [], 0
    while size < n_chars:
        s = (f"Maintenance note {rng.randint(100, 999)}: the {rng.choice(_SUBJ)} in the "
             f"{rng.choice(_PLACES)} {rng.choice(_VERB)} {rng.choice(_TAIL)}")
        out.append(s)
        size += len(s) + 1
    return " ".join(out)


def build(rng: random.Random, target_tokens: int, chars_per_token: float) -> tuple[list[dict], dict]:
    codes = {lab: code(rng) for lab in LABELS}
    budget = max(400, int(target_tokens * chars_per_token))
    # Split filler across four user turns so the chat has real message structure.
    part = budget // 4
    msgs = [
        {"role": "system", "content": (f"Verification code S is {codes['S']}. "
                                       "You are a careful assistant that answers from the conversation only. "
                                       + filler(rng, 200))},
        {"role": "user", "content": f"Verification code U1 is {codes['U1']}. Part one of the log: " + filler(rng, part)},
        {"role": "assistant", "content": "Noted part one."},
        {"role": "user", "content": "Part two of the log: " + filler(rng, part // 2)
         + f" Verification code M1 is {codes['M1']}. " + filler(rng, part // 2)},
        {"role": "assistant", "content": "Noted part two."},
        {"role": "user", "content": "Part three of the log: " + filler(rng, part // 2)
         + f" Verification code M2 is {codes['M2']}. " + filler(rng, part // 2)},
        {"role": "assistant", "content": "Noted part three."},
        {"role": "user", "content": "Part four of the log: " + filler(rng, part)
         + f"\n\nVerification code E is {codes['E']}.\n\n"
         "Now list every verification code from this whole conversation, one per line, exactly as "
         "`LABEL=CODE` for the labels S, U1, M1, M2, E in that order. If you cannot see a code, write "
         "`LABEL=UNKNOWN`. Output only those five lines."},
    ]
    return msgs, codes


def ps() -> dict:
    try:
        d = httpx.get(f"{OLLAMA}/api/ps", timeout=5).json()
        m = next((m for m in d.get("models", []) if m.get("name", "").startswith(MODEL)), None)
        return {} if m is None else {"context_length": m.get("context_length"),
                                     "size_vram_gb": round(m.get("size_vram", 0) / 1e9, 2)}
    except Exception as e:  # noqa: BLE001 - recorded, not hidden
        return {"error": type(e).__name__}


def runtime() -> dict:
    """Which runtime the result applies to (agent.VERIFIED_CONTEXT_RUNTIMES is keyed on these)."""
    out: dict = {}
    try:
        out["ollama_version"] = httpx.get(f"{OLLAMA}/api/version", timeout=5).json().get("version")
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=5).json().get("models", [])
        m = next((m for m in tags if m.get("name") == MODEL), {})
        out["model_digest"] = m.get("digest")
        out["runner"] = (m.get("details") or {}).get("runner")
        out["family"] = (m.get("details") or {}).get("family")
    except Exception as e:  # noqa: BLE001
        out["error"] = type(e).__name__
    return out


def ask(msgs: list[dict]) -> dict:
    t = time.perf_counter()
    r = httpx.post(f"{OLLAMA}/api/chat", json={"model": MODEL, "messages": msgs, "stream": False, "think": False,
                                               "options": {"temperature": 0, "num_predict": 120}},
                   timeout=180)
    r.raise_for_status()
    d = r.json()
    return {"wall_ms": round((time.perf_counter() - t) * 1000, 1),
            "prompt_eval_count": d.get("prompt_eval_count"), "eval_count": d.get("eval_count"),
            "load_ms": round((d.get("load_duration") or 0) / 1e6, 1),
            "prompt_eval_ms": round((d.get("prompt_eval_duration") or 0) / 1e6, 1),
            "answer": (d.get("message") or {}).get("content", "")}


def score(answer: str, codes: dict) -> dict:
    got = {}
    for lab in LABELS:
        m = re.search(rf"(?m)^\W*{lab}\s*=\s*([A-Z]+-\d{{4}}-[A-Z]+|UNKNOWN)", answer)
        got[lab] = m.group(1) if m else None
    return {lab: {"expected": codes[lab], "got": got[lab], "ok": got[lab] == codes[lab]} for lab in LABELS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="3000,4600,6900,10000")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else int(time.time())
    rng = random.Random(seed)
    out = {"started_utc": datetime.now(timezone.utc).isoformat(), "seed": seed, "model": MODEL,
           "runtime": runtime(), "ps_before": ps(), "request_options": {"temperature": 0, "num_predict": 120, "num_ctx": "not sent",
                                                  "keep_alive": "not sent"}, "trials": []}
    # Calibrate characters per token on this tokenizer with a real call.
    cal_msgs, cal_codes = build(rng, 1500, 4.0)
    cal = ask(cal_msgs)
    chars = sum(len(m["content"]) for m in cal_msgs)
    cpt = chars / cal["prompt_eval_count"] if cal.get("prompt_eval_count") else 4.0
    out["calibration"] = {"chars": chars, "prompt_eval_count": cal["prompt_eval_count"],
                          "chars_per_token": round(cpt, 3), "recall": score(cal["answer"], cal_codes)}
    print(f"calibration: {chars} chars -> {cal['prompt_eval_count']} tokens ({cpt:.2f} chars/token)")
    for size in [int(s) for s in a.sizes.split(",") if s.strip()]:
        for trial in range(a.trials):
            msgs, codes = build(rng, size, cpt)
            res = ask(msgs)
            sc = score(res["answer"], codes)
            row = {"target_tokens": size, "trial": trial + 1, **{k: v for k, v in res.items() if k != "answer"},
                   "answer": res["answer"][:400], "recall": sc, "ps_after": ps()}
            out["trials"].append(row)
            marks = " ".join(f"{lab}:{'ok' if sc[lab]['ok'] else 'MISS(' + str(sc[lab]['got']) + ')'}" for lab in LABELS)
            print(f"target {size:>6} trial {trial + 1}: prompt_eval_count={res['prompt_eval_count']:>6} "
                  f"wall={res['wall_ms']:>8.1f} ms  {marks}  ps={row['ps_after']}")
    out["ps_after"] = ps()
    out["all_recalled"] = all(t["recall"][lab]["ok"] for t in out["trials"] for lab in LABELS)
    out["max_prompt_eval_count"] = max((t["prompt_eval_count"] or 0 for t in out["trials"]), default=0)
    dest = ROOT / "bench" / "results" / f"context_probe_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"-> {dest}")


if __name__ == "__main__":
    main()
