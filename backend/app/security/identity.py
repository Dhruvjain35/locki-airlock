"""This Mac's identity in security events: a random device id and a private salt for hashing device identifiers. Neither comes
from the hardware, and both stay in data/security/ with owner-only permissions."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    salt: bytes


def load_identity(directory: Path, prefix: str = "mac") -> DeviceIdentity:
    """``prefix`` names the kind of device: ``mac`` for the security monitor, ``cam`` for the AIRLOCK camera."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "identity.json"
    try:
        data = json.loads(path.read_text())
        return DeviceIdentity(str(data["device_id"]), bytes.fromhex(data["salt"]))
    except FileNotFoundError:
        pass
    data = {"device_id": f"{prefix}-{secrets.token_hex(6)}", "salt": secrets.token_hex(32)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return DeviceIdentity(data["device_id"], bytes.fromhex(data["salt"]))
