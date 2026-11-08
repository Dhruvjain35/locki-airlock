"""Turns NeMo Relay's own ATOF events into AgentStepV1 messages for SpacetimeDB and the dashboard. Only what Relay itself recorded becomes a step here, and each
one says trace_source="nemo_relay". LOCKI's own application log never goes through this."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .contracts import AgentStepStatus, AgentStepV1


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _when(event: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event.get("timestamp")))
    except ValueError:
        return None


def steps_from_relay(events: list[dict], *, run_id: str, model_id: str | None = None, incident_id: str | None = None) -> list[AgentStepV1]:
    """A step for each tool Relay saw finish and each call its guardrail rejected, in the order they happened. Model calls are spans in Relay's trace, not steps."""
    starts = {e["uuid"]: e for e in events if e.get("category") == "tool" and e.get("scope_category") == "start" and e.get("uuid")}
    found: list[tuple[str, dict]] = []
    for event in events:
        if event.get("category") == "tool" and event.get("scope_category") == "end":
            found.append(("tool", event))
        elif event.get("kind") == "mark" and (event.get("data") or {}).get("rejected"):
            found.append(("rejected", event))
    steps: list[AgentStepV1] = []
    for number, (what, event) in enumerate(found, start=1):
        name = str(event.get("name") or "unknown_tool")
        tool = name if name.replace("_", "").isalnum() and name[0].isalpha() and name.islower() else "unknown_tool"
        trace = str(event.get("propagation_root_uuid") or "") or None
        if what == "rejected":
            steps.append(AgentStepV1(run_id=run_id, step=number, tool_name=tool, status=AgentStepStatus.DENIED, model_id=model_id, incident_id=incident_id, trace_id=trace, trace_source="nemo_relay", detail=str((event.get("data") or {}).get("rejection_reason") or "")[:240] or None))
            continue
        start = starts.get(event.get("uuid"))
        began, ended = (None if start is None else _when(start)), _when(event)
        duration = None if began is None or ended is None else round((ended - began).total_seconds() * 1000, 2)
        ok = (event.get("metadata") or {}).get("otel.status_code") == "OK"
        steps.append(AgentStepV1(run_id=run_id, step=number, tool_name=tool, status=AgentStepStatus.SUCCEEDED if ok else AgentStepStatus.FAILED, duration_ms=duration, model_id=model_id, incident_id=incident_id, trace_id=trace, trace_source="nemo_relay"))
    return steps
