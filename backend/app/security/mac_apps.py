"""Application launches on macOS: NSWorkspace notifications through a small helper process, or a labelled fallback that lists
processes every couple of seconds. Neither is complete. NSWorkspace covers visible applications, and a process list can miss
anything short-lived. Seeing everything would take Apple's restricted Endpoint Security entitlement, which this does not use.

A launch is reported as an observation. A remote-access tool opening does not mean anyone has taken control.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import plistlib
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import psutil

log = logging.getLogger(__name__)

HELPER = Path(__file__).with_name("nsworkspace_helper.py")
CODESIGN = "/usr/bin/codesign"
SNAPSHOT_INTERVAL_S = 2.0
_APP_BUNDLE = re.compile(r"^(.*?\.app)(?:/|$)")


@dataclass(frozen=True)
class AppLaunch:
    name: str | None
    bundle_id: str | None
    executable_path: str | None
    bundle_path: str | None
    pid: int | None
    launched_at_ms: int | None
    observed_at_ms: int
    observed_mono: float
    detection: Literal["nsworkspace_notification", "process_snapshot"]


OnLaunch = Callable[[AppLaunch], None]
OnProblem = Callable[[str], None]


class AppWatcher(Protocol):
    method: str

    def start(self, on_launch: OnLaunch) -> dict | None:
        """Starts watching and returns the frontmost application if known."""
        ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class WatchEntry:
    pattern: str  # fnmatch on the bundle identifier, or on the app's name
    category: str


# An example for the owner to edit. Everything on it has plenty of honest uses: a match is something to look at, not an accusation.
EXAMPLE_WATCHLIST: tuple[WatchEntry, ...] = (
    WatchEntry("com.teamviewer.*", "remote_access"),
    WatchEntry("com.philandro.anydesk", "remote_access"),
    WatchEntry("com.carriez.rustdesk", "remote_access"),
    WatchEntry("com.realvnc.*", "remote_access"),
    WatchEntry("com.splashtop.*", "remote_access"),
    WatchEntry("com.logmein.*", "remote_access"),
    WatchEntry("com.google.chrome.remote_desktop*", "remote_access"),
    WatchEntry("com.apple.ScreenSharing", "remote_access"),
    # Harmless and already on every Mac, so a launch can be tested without installing anything.
    WatchEntry("com.apple.calculator", "demo_test_app"),
    WatchEntry("com.apple.Chess", "demo_test_app"),
)


@dataclass
class Watchlist:
    entries: tuple[WatchEntry, ...] = EXAMPLE_WATCHLIST
    approved: set[str] = field(default_factory=set)  # bundle identifiers the owner said are fine

    def match(self, bundle_id: str | None, name: str | None = None) -> WatchEntry | None:
        if bundle_id and bundle_id.lower() in {a.lower() for a in self.approved}:
            return None
        for entry in self.entries:
            if bundle_id and fnmatch.fnmatchcase(bundle_id.lower(), entry.pattern.lower()):
                return entry
            if name and fnmatch.fnmatchcase(name.lower(), entry.pattern.lower()):
                return entry
        return None


@dataclass(frozen=True)
class CodeSignature:
    """What the app's signature says, read without any special permission. It says a signature is present and whose it claims
    to be; it does not re-verify the whole bundle, and being signed does not make an app harmless."""

    status: Literal["signed", "adhoc", "unsigned", "unknown"]
    identifier: str | None = None
    team_id: str | None = None
    authority: str | None = None

    def to_metadata(self) -> dict:
        return {"status": self.status, "identifier": self.identifier, "team_id": self.team_id, "authority": self.authority}


def parse_codesign(output: str, returncode: int) -> CodeSignature:
    if "not signed at all" in output:
        return CodeSignature("unsigned")
    if returncode != 0:
        return CodeSignature("unknown")
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key not in fields:
            fields[key] = value.strip()
    team = fields.get("TeamIdentifier")
    return CodeSignature(
        status="adhoc" if fields.get("Signature") == "adhoc" else "signed",
        identifier=(fields.get("Identifier") or None),
        team_id=None if team in (None, "", "not set") else team,
        authority=fields.get("Authority") or None,
    )


def code_signature(path: str | None, timeout_s: float = 3.0) -> CodeSignature:
    if not path or not path.startswith("/"):
        return CodeSignature("unknown")
    try:
        proc = subprocess.run([CODESIGN, "-d", "--verbose=2", path], capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return CodeSignature("unknown")
    return parse_codesign(proc.stderr + proc.stdout, proc.returncode)


def launch_from_helper(message: dict, mono: float) -> AppLaunch:
    launched = message.get("launched_at")
    return AppLaunch(
        name=message.get("name"),
        bundle_id=message.get("bundle_id"),
        executable_path=message.get("executable_path"),
        bundle_path=message.get("bundle_path"),
        pid=message.get("pid"),
        launched_at_ms=int(launched * 1000) if launched else None,
        observed_at_ms=int(float(message.get("observed_at") or time.time()) * 1000),
        observed_mono=mono,
        detection="nsworkspace_notification",
    )


class NSWorkspaceAppWatcher:
    method = "nsworkspace_notification"

    def __init__(self, on_problem: OnProblem | None = None, ready_timeout_s: float = 8.0):
        self.on_problem = on_problem
        self.ready_timeout_s = ready_timeout_s
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._frontmost: dict | None = None
        self._stopping = False
        self.error: str | None = None

    def start(self, on_launch: OnLaunch) -> dict | None:
        if sys.platform != "darwin":
            raise OSError("NSWorkspace needs macOS")
        self._stopping = False
        self._ready.clear()
        # -I: the helper imports nothing from this project or the current folder.
        self._proc = subprocess.Popen([sys.executable, "-I", str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read, args=(on_launch,), daemon=True, name="app-launches")
        self._reader.start()
        if not self._ready.wait(self.ready_timeout_s):
            self.stop()
            raise OSError("the NSWorkspace helper did not become ready")
        return self._frontmost

    def _read(self, on_launch: OnLaunch) -> None:
        proc = self._proc
        for line in proc.stdout:
            mono = time.monotonic()
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "ready":
                self._frontmost = message.get("frontmost")
                self._ready.set()
            elif kind == "launched":
                try:
                    on_launch(launch_from_helper(message, mono))
                except Exception:
                    log.exception("handling an application launch failed")
            elif kind == "error":
                self.error = str(message.get("error"))[:200]
                log.warning("NSWorkspace helper: %s", self.error)
        if not self._stopping:
            self.error = f"the NSWorkspace helper exited with {proc.wait()}"
            log.warning(self.error)
            if self.on_problem is not None:
                self.on_problem(self.error)

    def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # the helper exits when this closes
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=2)
        self._proc = None


def bundle_from_path(executable: str | None) -> tuple[str | None, str | None]:
    """(bundle path, bundle identifier) for a process running from inside an .app."""
    match = _APP_BUNDLE.match(executable or "")
    if not match:
        return None, None
    bundle = match.group(1)
    try:
        with open(Path(bundle) / "Contents" / "Info.plist", "rb") as f:
            return bundle, plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return bundle, None


class ProcessSnapshotWatcher:
    """Fallback, and labelled as such in every event: reports new processes running from an .app bundle, up to one interval late.
    Anything that starts and exits between two looks is missed."""

    method = "process_snapshot"

    def __init__(self, interval_s: float = SNAPSHOT_INTERVAL_S, on_problem: OnProblem | None = None, list_processes: Callable[[], dict[int, tuple[str | None, str | None, float | None]]] | None = None):
        self.interval_s = interval_s
        self.on_problem = on_problem
        self._list = list_processes or _list_processes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_launch: OnLaunch) -> dict | None:
        known = set(self._list())
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_launch, known), daemon=True, name="app-snapshot")
        self._thread.start()
        return None

    def _run(self, on_launch: OnLaunch, known: set[int]) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                now = self._list()
            except Exception as exc:
                log.warning("listing processes failed: %s", exc)
                continue
            mono, at_ms = time.monotonic(), int(time.time() * 1000)
            for pid in now.keys() - known:
                name, exe, created = now[pid]
                bundle, bundle_id = bundle_from_path(exe)
                if bundle is None:
                    continue
                on_launch(AppLaunch(name, bundle_id, exe, bundle, pid, int(created * 1000) if created else None, at_ms, mono, "process_snapshot"))
            known = set(now)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 3)


def _list_processes() -> dict[int, tuple[str | None, str | None, float | None]]:
    out: dict[int, tuple[str | None, str | None, float | None]] = {}
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        info = proc.info
        out[info["pid"]] = (info.get("name"), info.get("exe"), info.get("create_time"))
    return out


def create_app_watcher(prefer: str = "nsworkspace", on_problem: OnProblem | None = None) -> AppWatcher:
    return ProcessSnapshotWatcher(on_problem=on_problem) if prefer == "snapshot" or sys.platform != "darwin" else NSWorkspaceAppWatcher(on_problem=on_problem)
