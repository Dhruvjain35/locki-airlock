"""What the Mac needs to know about the armed session. When SpacetimeDB is connected it decides arming and this is only a copy of its
answer; until then the owner arms locally, and it says so (source="local"). Either way the deterministic warning does not depend on a
model or a network."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_SESSION_S = 4 * 3600


class ResponsePolicy(BaseModel):
    """What the owner chose when arming. Automatic locking is off unless they turned it on."""

    warn_on_unknown_usb: bool = True
    warn_on_watchlist_app: bool = True
    auto_lock: bool = False
    auto_lock_on_watchlist_app: bool = False
    freeze_protected_tools: bool = False  # during an incident, our own managed agents may only read
    approved_devices: list[str] = Field(default_factory=list, description="Device fingerprints the owner approved")
    approved_apps: list[str] = Field(default_factory=list, description="Bundle identifiers the owner approved")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:32]


UsbStatus = Literal["baseline", "allowlisted", "unknown", "not_armed"]


@dataclass(frozen=True)
class ArmedSession:
    session_id: str
    armed_at_ms: int
    expires_at_ms: int
    baseline: dict[str, int]  # fingerprint -> how many were connected. Two identical devices with no serial share a fingerprint.
    policy: ResponsePolicy
    source: Literal["local", "spacetimedb"] = "local"
    frontmost_app: str | None = field(default=None)
    policy_hash: str | None = None  # set by SpacetimeDB for a session it armed

    @property
    def owner_policy_hash(self) -> str:
        return self.policy_hash or self.policy.digest()

    def expired(self, now_ms: int | None = None) -> bool:
        return (now_ms if now_ms is not None else int(time.time() * 1000)) >= self.expires_at_ms

    def classify_usb(self, fingerprint: str, connected_like_it: int = 1) -> UsbStatus:
        """``connected_like_it`` counts this device too. One more than the baseline held is new, even if it looks the same."""
        if connected_like_it <= self.baseline.get(fingerprint, 0):
            return "baseline"
        if fingerprint in self.policy.approved_devices:
            return "allowlisted"
        return "unknown"


def new_local_session(baseline: Iterable[str] | dict[str, int], policy: ResponsePolicy, *, frontmost_app: str | None = None, duration_s: int = DEFAULT_SESSION_S, now_ms: int | None = None) -> ArmedSession:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    counts = dict(baseline) if isinstance(baseline, dict) else dict(Counter(baseline))
    return ArmedSession(f"local-{uuid.uuid4().hex[:12]}", now, now + duration_s * 1000, counts, policy, "local", frontmost_app)
