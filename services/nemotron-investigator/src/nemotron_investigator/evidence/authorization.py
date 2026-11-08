"""Bind a stored live report to the exact approved incident snapshot it used."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def incident_scope_digest(incident: BaseModel) -> str:
    """Any grant, evidence, severity or incident-state change invalidates the report.

    This is a comparison value, not an authorization credential. Callers must also
    authenticate the live principal and read the current caller-scoped view.
    """
    canonical = json.dumps(incident.model_dump(mode="json"), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
