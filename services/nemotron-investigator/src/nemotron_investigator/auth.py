"""API-key authentication for the investigation API.

Keys are random 256-bit tokens shown ONCE at creation. Only their SHA-256
digests are stored (in .secrets/api_keys.json, mode 0600). Each key maps to a
requester identity, the workstations it may investigate, and scopes.

The key file fails closed:
  * every entry is validated strictly. A malformed entry is skipped and logged;
    it is never completed with defaults, and it never breaks the other keys;
  * a key file that group or other users can write is refused (no key works);
  * a digest that appears in any revoked entry stays revoked, and a digest that
    appears in more than one active entry is ambiguous and is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger("nemotron_investigator.auth")

DEFAULT_SCOPES = ("investigations:create", "investigations:read", "health:read")
# health:admin also shows the evidence backend's membership details on /health.
# It is never granted by default.
KNOWN_SCOPES = frozenset(DEFAULT_SCOPES + ("health:admin",))

MAX_PRESENTED_KEY_CHARS = 256
MAX_KEY_FILE_BYTES = 1 << 20
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_ID_CHARS = 128


@dataclass(frozen=True)
class ApiPrincipal:
    requester_id: str
    allowed_workstations: tuple[str, ...]
    scopes: tuple[str, ...]
    # SHA-256 of the presented key, so a queued run can check that its key is still valid.
    key_digest: str = field(default="", repr=False)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _is_label(v: object) -> bool:
    return (isinstance(v, str) and 0 < len(v) <= _MAX_ID_CHARS and v.strip() == v and v.isprintable())


def validate_entry(e: object) -> tuple[dict | None, str | None]:
    """Return (normalized entry, None) for a valid active entry, else (None, reason)."""
    if not isinstance(e, dict):
        return None, "entry is not an object"
    if not isinstance(e.get("key_sha256"), str) or not _DIGEST.fullmatch(e["key_sha256"]):
        return None, "key_sha256 must be 64 lowercase hex characters"
    if not _is_label(e.get("requester_id")):
        return None, "requester_id must be a non-empty printable string (<=128 chars)"
    ws = e.get("allowed_workstations")
    if not isinstance(ws, list) or not all(_is_label(w) for w in ws):
        return None, "allowed_workstations must be a list of non-empty strings"
    scopes = e.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        return None, "scopes must be a list of strings (required)"
    unknown = sorted(set(scopes) - KNOWN_SCOPES)
    if unknown:
        return None, f"unknown scope(s) {unknown}"
    return {"key_sha256": e["key_sha256"], "requester_id": e["requester_id"],
            "allowed_workstations": tuple(dict.fromkeys(ws)), "scopes": tuple(dict.fromkeys(scopes))}, None


def _is_revoked(e: dict) -> bool:
    # Only an absent, null or false `revoked` means active; any other value revokes.
    rv = e.get("revoked", False)
    return not (rv is None or rv is False)


def parse_key_entries(data: object, *, source: str = "api key file") -> list[dict]:
    """Validate a parsed key file. Invalid entries are skipped and logged."""
    if not isinstance(data, dict):
        log.error("%s: top level is not an object; no API key is accepted", source)
        return []
    raw = data.get("keys", [])
    if not isinstance(raw, list):
        log.error("%s: 'keys' is not a list; no API key is accepted", source)
        return []
    revoked = {e["key_sha256"] for e in raw
               if isinstance(e, dict) and isinstance(e.get("key_sha256"), str) and _is_revoked(e)}
    valid: list[dict] = []
    for i, e in enumerate(raw):
        if isinstance(e, dict) and _is_revoked(e):
            continue
        entry, reason = validate_entry(e)
        if entry is None:
            log.warning("%s: entry %d skipped: %s", source, i, reason)
        elif entry["key_sha256"] in revoked:
            log.warning("%s: entry %d skipped: the same key is revoked in another entry", source, i)
        else:
            valid.append(entry)
    seen: dict[str, int] = {}
    for e in valid:
        seen[e["key_sha256"]] = seen.get(e["key_sha256"], 0) + 1
    dupes = {d for d, n in seen.items() if n > 1}
    if dupes:
        log.warning("%s: %d key(s) appear in more than one active entry; those keys are refused",
                    source, len(dupes))
    return [e for e in valid if e["key_sha256"] not in dupes]


class KeyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._sig: tuple | None = None
        self._entries: list[dict] = []

    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._entries, self._sig = [], None
            return
        except OSError as e:
            log.error("api key file %s: cannot stat (%s); no API key is accepted", self.path.name,
                      type(e).__name__)
            self._entries, self._sig = [], None
            return
        sig = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_uid)
        if sig == self._sig:
            return
        self._sig = sig
        self._entries = self._read()

    def _read(self) -> list[dict]:
        name = self.path.name
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())  # check the file we actually read
                if not stat.S_ISREG(st.st_mode):
                    log.error("api key file %s is not a regular file; no API key is accepted", name)
                    return []
                if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    log.error("api key file %s is writable by group or others (mode %o); refusing it, "
                              "no API key is accepted until it is chmod 600", name, stat.S_IMODE(st.st_mode))
                    return []
                raw = f.read(MAX_KEY_FILE_BYTES + 1)
        except OSError as e:
            log.error("api key file %s: cannot read (%s); no API key is accepted", name, type(e).__name__)
            return []
        if len(raw) > MAX_KEY_FILE_BYTES:
            log.error("api key file %s is larger than %d bytes; no API key is accepted", name, MAX_KEY_FILE_BYTES)
            return []
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            log.error("api key file %s is not valid JSON; no API key is accepted", name)
            return []
        return parse_key_entries(data, source=f"api key file {name}")

    def _match(self, digest: str) -> ApiPrincipal | None:
        match = None
        for e in self._entries:  # constant-time compare against every entry
            if hmac.compare_digest(e["key_sha256"], digest):
                match = e
        if match is None:
            return None
        return ApiPrincipal(match["requester_id"], match["allowed_workstations"], match["scopes"],
                            key_digest=match["key_sha256"])

    def authenticate(self, presented: str | None) -> ApiPrincipal | None:
        if not isinstance(presented, str) or not presented or len(presented) > MAX_PRESENTED_KEY_CHARS:
            return None
        try:
            digest = _digest(presented)
        except UnicodeError:
            return None
        self._load()
        return self._match(digest)

    def resolve_digest(self, digest: str) -> ApiPrincipal | None:
        """The current principal for a key digest, or None if the key is gone, revoked or invalid."""
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            return None
        self._load()
        return self._match(digest)


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def add_key(path: str | Path, requester_id: str, workstations: list[str],
            scopes: tuple[str, ...] = DEFAULT_SCOPES) -> str:
    path = Path(path)
    # A bare string would split into characters ("ws:team-*" -> [..., "*"]): require real sequences.
    if not isinstance(workstations, (list, tuple)) or not isinstance(scopes, (list, tuple)):
        raise ValueError("refusing to create API key: workstations and scopes must be lists")
    new = {"key_sha256": "0" * 64, "requester_id": requester_id,
           "allowed_workstations": list(workstations), "scopes": list(scopes)}
    _, reason = validate_entry(new)
    if reason:
        raise ValueError(f"refusing to create API key: {reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        if os.stat(path).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(f"{path.name} is writable by group or others; inspect it and chmod 600 first")
        data = json.loads(path.read_text() or "{}")
        if not isinstance(data, dict) or not isinstance(data.get("keys", []), list):
            raise ValueError(f"{path.name} is not a valid API key file")
        data.setdefault("keys", [])
    else:
        data = {"keys": []}
    key = "ni_" + secrets.token_urlsafe(32)
    data["keys"].append({**new, "key_sha256": _digest(key), "created_at_ms": int(time.time() * 1000)})
    _write_private(path, json.dumps(data, indent=2))
    return key


def create_key(requester_id: str, workstations: list[str], *, scopes: list[str] | None = None,
               out: str | None = None) -> int:
    """Create a key. With `out`, the key is written (key only, mode 0600, never overwriting a file)
    and not printed; otherwise it is printed once."""
    chosen = tuple(scopes) if scopes else DEFAULT_SCOPES
    unknown = sorted(set(chosen) - KNOWN_SCOPES)
    if unknown:
        raise SystemExit(f"unknown scope(s) {unknown}; known: {sorted(KNOWN_SCOPES)}")
    target = Path(out) if out else None
    if target is not None and target.exists():
        raise SystemExit(f"{target} already exists; refusing to overwrite a key file")
    key = add_key(Settings.from_env().api_keys_file, requester_id, workstations, scopes=chosen)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        print(f"API key for {requester_id} written to {target} (mode 0600, not printed); scopes {list(chosen)}")
    else:
        print(f"API key for {requester_id} (shown once; store it securely):\n{key}")
    return 0
