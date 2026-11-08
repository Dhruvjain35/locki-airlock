"""Audit a source snapshot of this package before it goes into the monorepo.

FAIL (exit 1) on any of:
  * a forbidden path: .git, .venv, .secrets, var, caches, *.pyc, .DS_Store, .env*, key/token/cert files,
    api_keys.json
  * a model-weight or large binary file (*.safetensors, *.gguf, *.bin, *.pt, *.onnx, ... or any file > 2 MB)
  * the exact value of a real secret from --secrets-dir: the investigator's SpacetimeDB token, any
    handed-off API key, any stored API-key digest (values are compared, never printed)
  * a high-confidence credential pattern: JWT, ni_ API key, private-key block, GitHub/AWS/Slack tokens
  * a live-looking incident id (incident:<32 hex> with 8+ distinct hex digits; synthetic test ids are
    low-entropy on purpose) or any string listed in --forbid-file (real ids seen in live runs)
WARN (reported, exit 0): generic "secret = '...'" assignments, e-mail addresses, Tailscale IPs.

Prints only file paths, line numbers and the class of each finding, never a secret value.

  python -I scripts/audit_snapshot.py <snapshot_dir> [--secrets-dir .secrets] [--forbid-file f] [--report AUDIT.md]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

FORBIDDEN_DIRS = {".git", ".venv", "venv", ".secrets", "var", "__pycache__", ".pytest_cache", ".mypy_cache",
                  ".ruff_cache", "node_modules", ".ipynb_checkpoints"}
FORBIDDEN_NAMES = {".DS_Store", "api_keys.json"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".key", ".pem", ".p12", ".pfx", ".token", ".crt", ".der", ".keystore"}
WEIGHT_SUFFIXES = {".safetensors", ".gguf", ".ggml", ".bin", ".pt", ".pth", ".onnx", ".ckpt", ".npz", ".npy",
                   ".tflite", ".mlmodel", ".h5", ".pkl", ".parquet", ".zip", ".tgz", ".gz", ".tar", ".dylib", ".so"}
MAX_FILE_BYTES = 2 * 1024 * 1024
PATTERNS_FAIL = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "investigator_api_key": re.compile(r"\bni_[A-Za-z0-9_-]{30,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}
PATTERNS_WARN = {
    "generic_secret_assignment": re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|bearer)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "tailscale_ip": re.compile(r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
}
EMAIL_OK = {"noreply@anthropic.com"}
LIVE_INCIDENT = re.compile(r"incident:([0-9a-f]{32})\b")


def secret_values(secrets_dir: Path | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if secrets_dir is None or not secrets_dir.is_dir():
        return out
    tok = secrets_dir / "stdb_investigator.token"
    if tok.is_file():
        try:
            d = json.loads(tok.read_text())
            if isinstance(d, dict) and isinstance(d.get("token"), str) and len(d["token"]) >= 16:
                out.setdefault("spacetimedb_token", []).append(d["token"])
        except ValueError:
            raw = tok.read_text().strip()
            if len(raw) >= 16:
                out.setdefault("spacetimedb_token", []).append(raw)
    for k in (secrets_dir / "handoff").glob("*.key") if (secrets_dir / "handoff").is_dir() else []:
        v = k.read_text().strip()
        if len(v) >= 16:
            out.setdefault("api_key", []).append(v)
    keys = secrets_dir / "api_keys.json"
    if keys.is_file():
        try:
            for e in json.loads(keys.read_text()).get("keys", []):
                if isinstance(e, dict) and isinstance(e.get("key_sha256"), str) and len(e["key_sha256"]) == 64:
                    out.setdefault("api_key_digest", []).append(e["key_sha256"])
        except (ValueError, AttributeError):
            pass
    return out


def forbidden_strings(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def audit(root: Path, secrets_dir: Path | None, forbid_file: Path | None) -> dict:
    fails: list[dict] = []
    warns: list[dict] = []
    files: list[tuple[str, int, str]] = []
    secrets = secret_values(secrets_dir)
    forbid = forbidden_strings(forbid_file)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        parts = set(p.relative_to(root).parts)
        if p.is_dir():
            if p.name in FORBIDDEN_DIRS:
                fails.append({"file": rel, "check": "forbidden_directory"})
            continue
        if p.is_symlink():
            fails.append({"file": rel, "check": "symlink"})
            continue
        if parts & FORBIDDEN_DIRS:
            continue  # already reported via its directory
        size = p.stat().st_size
        data = p.read_bytes()
        files.append((rel, size, hashlib.sha256(data).hexdigest()))
        name, suffix = p.name, p.suffix.lower()
        if name in FORBIDDEN_NAMES or suffix in FORBIDDEN_SUFFIXES or name.startswith(".env"):
            fails.append({"file": rel, "check": "forbidden_file"})
        if suffix in WEIGHT_SUFFIXES:
            fails.append({"file": rel, "check": "weights_or_binary_archive"})
        if size > MAX_FILE_BYTES:
            fails.append({"file": rel, "check": f"file_larger_than_{MAX_FILE_BYTES}_bytes"})
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            fails.append({"file": rel, "check": "binary_file"})
            continue
        for cls, values in secrets.items():
            if any(v in text for v in values):
                fails.append({"file": rel, "check": f"contains_real_{cls}"})
        for s in forbid:
            if s in text:
                fails.append({"file": rel, "check": "contains_forbidden_live_identifier"})
        for i, line in enumerate(text.splitlines(), 1):
            for cls, rx in PATTERNS_FAIL.items():
                if rx.search(line):
                    fails.append({"file": rel, "line": i, "check": f"credential_pattern:{cls}"})
            for m in LIVE_INCIDENT.finditer(line):
                if len(set(m.group(1))) >= 8:
                    fails.append({"file": rel, "line": i, "check": "live_looking_incident_id"})
            for cls, rx in PATTERNS_WARN.items():
                for m in rx.finditer(line):
                    if cls == "email" and m.group(0).lower() in EMAIL_OK:
                        continue
                    warns.append({"file": rel, "line": i, "check": cls})
    return {"files": files, "fails": fails, "warns": warns,
            "secret_classes_checked": sorted(secrets), "forbidden_strings_checked": len(forbid)}


def render(res: dict, root: Path, source_commit: str | None) -> str:
    total = sum(s for _, s, _ in res["files"])
    out = [f"# Snapshot audit: {'PASS' if not res['fails'] else 'FAIL'}", "",
           f"- snapshot: `{root}`",
           f"- source commit (standalone repo, history not exported): `{source_commit or 'unknown'}`",
           f"- files: {len(res['files'])}, total {total} bytes",
           f"- real secret values compared (never printed): {', '.join(res['secret_classes_checked']) or 'none found'}",
           f"- forbidden live identifiers compared: {res['forbidden_strings_checked']}",
           "- checks: forbidden paths, key/token/cert files, model weights and archives, files over 2 MB, binary files, "
           "exact secret values, credential patterns, live-looking incident ids", ""]
    out.append(f"## Failures ({len(res['fails'])})")
    out += [f"- `{f['file']}`{':' + str(f['line']) if 'line' in f else ''}: {f['check']}" for f in res["fails"]] or ["- none"]
    out += ["", f"## Warnings ({len(res['warns'])}) (reviewed by a human; not secrets by themselves)"]
    out += [f"- `{w['file']}:{w['line']}`: {w['check']}" for w in res["warns"]] or ["- none"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--secrets-dir", default=None)
    ap.add_argument("--forbid-file", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--source-commit", default=None)
    a = ap.parse_args()
    root = Path(a.snapshot).resolve()
    res = audit(root, Path(a.secrets_dir) if a.secrets_dir else None, Path(a.forbid_file) if a.forbid_file else None)
    report = render(res, root, a.source_commit)
    if a.report:
        Path(a.report).write_text(report)
    if a.manifest:
        Path(a.manifest).write_text("".join(f"{h}  {rel}\n" for rel, _, h in res["files"]))
    print(report)
    return 1 if res["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
