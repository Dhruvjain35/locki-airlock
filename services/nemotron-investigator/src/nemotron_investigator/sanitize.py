"""Handling of untrusted text that reaches the model through tool results.

Security does NOT rely on this module: tool permissions, argument validation
and the action allowlist are enforced in code regardless of what the model
reads. This module (a) bounds and cleans untrusted strings, (b) neutralizes
chat-template control delimiters so device-controlled text cannot imitate a
turn boundary, and (c) flags instruction-like content so the assessment can
report it as an observation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_INJECTION_PATTERNS = [
    r"ignore (all|any|the)?\s*(previous|prior|above)",
    r"disregard (all|any|the)?\s*(previous|prior|above|instructions)",
    r"\bsystem prompt\b",
    r"\byou are now\b",
    r"\bnew instructions?\b",
    r"\b(call|invoke|use|run) (the )?(tool|function)\b",
    r"\b(disarm|unlock|unarm|turn off|disable)\b.{0,40}\b(session|alarm|airlock|lock|security|monitoring)\b",
    r"\b(mark|classify|report)\b.{0,30}\b(as )?(safe|benign|low risk|resolved|false positive)\b",
    r"\b(owner|admin|security) (policy|team|says|approved)\b.{0,40}\b(approved|allowed|safe|ignore|low)\b",
    r"\bdeveloper mode\b|\bmaintenance mode\b|\badmin mode\b",
    r"[<‹]/?\s*(system|assistant|tool|instructions?|user)\s*[>›]",
    # chat-template control tokens (raw or after neutralization by clean_text)
    r"[<‹]\|[^|]{0,32}\|?[>›]?|\|[>›]",
    r"\b(im_start|im_end|start_of_turn|end_of_turn|tool_call|tool_response|eot_id|begin_of_text)\b",
    r"\[/?INST\]|[<‹]{2}/?SYS[>›]{2}",
    r"\b(assistant|system)\s*:",
    r"\b(rm -rf|curl |wget |bash -c|powershell)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)

_SAFE_ID = re.compile(r"^[A-Za-z0-9:_\-\.@/+=]{1,96}$")


def clean_text(value: object, max_len: int = 120) -> str:
    """Normalize, strip control/format characters, neutralize angle brackets, bound length."""
    s = unicodedata.normalize("NFKC", str(value))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" or ch in " ")
    # Chat templates delimit turns with <...> tokens; untrusted text must not be able
    # to spell one. Lookalike angle quotes keep the text readable as evidence.
    s = s.replace("<", "‹").replace(">", "›")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def looks_like_injection(value: str) -> bool:
    return bool(_INJECTION_RE.search(value))


def safe_id(raw: object) -> tuple[str, bool]:
    """Display form of an externally chosen identifier.

    Returns (id, ok). Ids that fit the strict charset pass through unchanged
    (so citations match the backend). Anything else becomes an opaque,
    deterministic placeholder so it cannot carry instructions to the model.
    """
    s = str(raw)
    if _SAFE_ID.fullmatch(s):
        return s, True
    return "unsafe-id:" + hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12], False


def primitive(value: object, max_len: int = 64):
    """Return a primitive JSON-safe value or None (drops nested structures)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return clean_text(value, max_len)
    return None
