"""A mission's own record: one JSON line per real event, kept in the mission's folder under data/.
It is LOCKED IN's log, not a NeMo Relay trace. Secrets and the home folder are redacted."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .workspace import redact

MAX_FIELD = 2000


def _clean(value):
    if isinstance(value, str):
        return redact(value)[:MAX_FIELD]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value][:60]
    return value if isinstance(value, (int, float, bool)) or value is None else redact(str(value))[:MAX_FIELD]


class MissionTrace:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def record(self, record_kind: str, /, **data) -> None:
        line = {"at": round(time.time(), 3), "kind": record_kind, **_clean(data)}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            self.count += 1
        except OSError:
            pass  # a full disk must not stop the mission
