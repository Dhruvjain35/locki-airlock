"""Locking this Mac's screen, on the owner's terms. There are three separate states, and each is reported only when it is true:

    LOCK_REQUESTED  someone asked, and every check passed
    LOCK_EXECUTED   the OS mechanism ran without an error
    LOCK_CONFIRMED  the OS itself reports the session as locked

Dry run by default: real locking also needs LOCKEDIN_SECURITY_LOCK_MODE=real and the owner's opt-in for this session.

A locked screen stops someone using the keyboard and mouse. It does not stop programs already running or a device already plugged
in. Normal credentials always unlock it, and this module has no way to prevent that. It also never turns the display off and calls
that a lock, and it does not touch Lockdown Mode, the network, other processes or any setting.

Whether the unlock needs a password depends on the Mac's own settings, which is why preflight reports them.
"""

from __future__ import annotations

import ctypes as ct
import logging
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .contracts import LockRequestV1, LockStatus
from .session import ArmedSession

log = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"
# Control-Command-Q, Apple's documented Lock Screen shortcut (support.apple.com/en-us/102650). Needs Accessibility permission.
LOCK_KEYSTROKE_SCRIPT = 'tell application "System Events" to keystroke "q" using {control down, command down}'
CONFIRM_WITHIN_S = 5.0
COOLDOWN_S = 60.0  # never lock again this soon, so the owner is not locked out in a loop while trying to get back in
OWNER_GRACE_S = 120.0  # after the owner unlocks, automatic locks wait this long
MAX_REMEMBERED = 200


Mechanism = Literal["keystroke", "login_framework"]


class ScreenState(Protocol):
    def is_locked(self) -> bool | None:
        """True or False as the OS reports it. None if it cannot say."""
        ...


class CGSessionState:
    """Reads the login session dictionary. CGSSessionScreenIsLocked is there and true only while the screen is locked."""

    def __init__(self) -> None:
        fw = "/System/Library/Frameworks"
        ptr = ct.c_void_p
        self._cg = ct.CDLL(f"{fw}/CoreGraphics.framework/CoreGraphics")
        self._cf = ct.CDLL(f"{fw}/CoreFoundation.framework/CoreFoundation")
        self._cg.CGSessionCopyCurrentDictionary.restype = ptr
        self._cf.CFDictionaryGetValue.restype, self._cf.CFDictionaryGetValue.argtypes = ptr, [ptr, ptr]
        self._cf.CFStringCreateWithCString.restype, self._cf.CFStringCreateWithCString.argtypes = ptr, [ptr, ct.c_char_p, ct.c_uint32]
        self._cf.CFBooleanGetValue.restype, self._cf.CFBooleanGetValue.argtypes = ct.c_bool, [ptr]
        self._cf.CFRelease.argtypes = [ptr]

    def is_locked(self) -> bool | None:
        session = self._cg.CGSessionCopyCurrentDictionary()
        if not session:
            return None
        key = self._cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        try:
            value = self._cf.CFDictionaryGetValue(session, key)
            return bool(value) and bool(self._cf.CFBooleanGetValue(value))
        finally:
            self._cf.CFRelease(key)
            self._cf.CFRelease(session)


def accessibility_trusted() -> bool | None:
    """Whether the app running this may send keystrokes. Asks without showing the permission prompt."""
    if sys.platform != "darwin":
        return None
    try:
        ax = ct.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ct.c_bool
        return bool(ax.AXIsProcessTrusted())
    except OSError:
        return None


def screen_lock_setting() -> str | None:
    """What macOS says about needing a password after sleep or the screen saver: "on", "off" or None. Only reads it."""
    try:
        proc = subprocess.run(["/usr/sbin/sysadminctl", "-screenLock", "status"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout + proc.stderr).lower()
    return "off" if "screenlock is off" in text else "on" if "screenlock" in text else None


def run_keystroke() -> tuple[bool, str | None]:
    if accessibility_trusted() is False:
        return False, "the app running LOCKI needs Accessibility permission to send the Lock Screen shortcut"
    try:
        proc = subprocess.run([OSASCRIPT, "-e", LOCK_KEYSTROKE_SCRIPT], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return (True, None) if proc.returncode == 0 else (False, (proc.stderr.strip() or f"osascript exited with {proc.returncode}")[:200])


def run_login_framework() -> tuple[bool, str | None]:
    """An undocumented macOS call. Used only if chosen explicitly, and always labelled as this mechanism."""
    try:
        login = ct.CDLL("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
        login.SACLockScreenImmediate.restype = ct.c_int
        result = login.SACLockScreenImmediate()
    except (OSError, AttributeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return (True, None) if result == 0 else (False, f"SACLockScreenImmediate returned {result}")


# A strict list. Nothing here takes an argument from anywhere, so a request cannot turn into a different command.
MECHANISMS: dict[str, Callable[[], tuple[bool, str | None]]] = {"keystroke": run_keystroke, "login_framework": run_login_framework}


@dataclass
class LockResult:
    action_id: str
    status: LockStatus
    dry_run: bool
    session_id: str | None = None
    incident_id: str | None = None
    mechanism: str | None = None
    detail: str | None = None
    history: list[dict] = field(default_factory=list)  # every state it passed through, with its time and reason
    requested_at_ms: int | None = None
    executed_at_ms: int | None = None
    confirmed_at_ms: int | None = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id, "status": self.status.value, "dry_run": self.dry_run, "session_id": self.session_id, "incident_id": self.incident_id, "mechanism": self.mechanism, "detail": self.detail,
            "history": self.history, "requested_at_ms": self.requested_at_ms, "executed_at_ms": self.executed_at_ms, "confirmed_at_ms": self.confirmed_at_ms,
            "confirm_ms": None if self.confirmed_at_ms is None or self.requested_at_ms is None else self.confirmed_at_ms - self.requested_at_ms,
        }


class LockAdapter:
    """Every request goes through the same checks: not a fixture, in date, this session, this owner's policy, the owner opted in, not a
    repeat, not during the cooldown. A repeat of an action id returns its first result and locks nothing."""

    def __init__(
        self,
        *,
        mode: Literal["dry_run", "real"] = "dry_run",
        mechanism: Mechanism = "keystroke",
        screen: ScreenState | None = None,
        runners: dict[str, Callable[[], tuple[bool, str | None]]] | None = None,
        on_status: Callable[[LockResult], None] | None = None,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        confirm_within_s: float = CONFIRM_WITHIN_S,
        cooldown_s: float = COOLDOWN_S,
        owner_grace_s: float = OWNER_GRACE_S,
    ):
        if mechanism not in MECHANISMS:
            raise ValueError(f"unknown lock mechanism: {mechanism}")
        self.mode = mode
        self.mechanism = mechanism
        self._screen = screen
        self._runners = runners if runners is not None else MECHANISMS
        self.on_status = on_status
        self._clock, self._mono, self._sleep = clock, mono, sleep
        self.confirm_within_s = confirm_within_s
        self.cooldown_s = cooldown_s
        self.owner_grace_s = owner_grace_s
        self._results: OrderedDict[str, LockResult] = OrderedDict()
        self._last_lock_mono: float | None = None
        self._owner_back_mono: float | None = None
        self._lock = threading.Lock()

    def _screen_state(self) -> ScreenState | None:
        if self._screen is None and sys.platform == "darwin":
            try:
                self._screen = CGSessionState()
            except OSError as exc:
                log.warning("cannot read the session state: %s", exc)
        return self._screen

    def is_locked(self) -> bool | None:
        state = self._screen_state()
        return None if state is None else state.is_locked()

    def owner_returned(self) -> None:
        """The owner unlocked: automatic locks hold off for a while, so nothing locks them out as they sit back down."""
        self._owner_back_mono = self._mono()

    # ------------------------------------------------------------------ the request
    def request(self, request: LockRequestV1, session: ArmedSession | None, *, owner_initiated: bool = False) -> LockResult:
        with self._lock:
            earlier = self._results.get(request.action_id)
            if earlier is not None:
                return earlier
            result = LockResult(request.action_id, LockStatus.LOCK_REFUSED, self.mode != "real", request.session_id, request.incident_id)
            self._results[request.action_id] = result
            while len(self._results) > MAX_REMEMBERED:
                self._results.popitem(last=False)
            refusal = self._refusal(request, session, owner_initiated)
            if refusal is not None:
                self._note(result, LockStatus.LOCK_REFUSED, refusal)
                return result
            self._last_lock_mono = self._mono()
        result.requested_at_ms = int(self._clock() * 1000)
        self._note(result, LockStatus.LOCK_REQUESTED, request.reason)
        if self.mode != "real":
            self._note(result, LockStatus.LOCK_DRY_RUN, f"dry run: would use {self.mechanism}. The screen was not locked.")
            return result
        self._execute(result)
        return result

    def _refusal(self, request: LockRequestV1, session: ArmedSession | None, owner_initiated: bool) -> str | None:
        now_ms = int(self._clock() * 1000)
        if request.is_fixture and self.mode == "real":
            return "a fixture can never lock a real screen"
        if now_ms >= request.expiry:
            return "the request has expired"
        if session is None or session.expired(now_ms):
            return "no armed session"
        if request.session_id != session.session_id:
            return "the request is for a different session"
        if request.owner_policy_hash != session.owner_policy_hash:
            return "the request does not match the owner's policy"
        if not owner_initiated and not session.policy.auto_lock:
            return "the owner has not opted in to automatic locking"
        if not owner_initiated and self._owner_back_mono is not None and self._mono() - self._owner_back_mono < self.owner_grace_s:
            return "the owner just came back"
        if self._last_lock_mono is not None and self._mono() - self._last_lock_mono < self.cooldown_s:
            return "a lock was requested moments ago"
        if self.is_locked() is True:
            return "the screen is already locked"
        return None

    def _execute(self, result: LockResult) -> None:
        result.mechanism = self.mechanism
        try:
            ok, detail = self._runners[self.mechanism]()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        if not ok:
            self._note(result, LockStatus.LOCK_FAILED, detail or "the lock mechanism failed")
            return
        result.executed_at_ms = int(self._clock() * 1000)
        self._note(result, LockStatus.LOCK_EXECUTED, f"{self.mechanism} ran. Not confirmed yet.")
        threading.Thread(target=self._confirm, args=(result,), daemon=True, name="lock-confirm").start()

    def _confirm(self, result: LockResult) -> None:
        end = self._mono() + self.confirm_within_s
        while self._mono() < end:
            if self.is_locked() is True:
                result.confirmed_at_ms = int(self._clock() * 1000)
                self._note(result, LockStatus.LOCK_CONFIRMED, "the OS reports the session as locked")
                return
            self._sleep(0.1)
        state = self.is_locked()
        result.detail = f"the OS did not report a locked session within {self.confirm_within_s:.0f} s" if state is False else "the OS session state could not be read, so the lock is unconfirmed"
        log.warning("lock %s: %s", result.action_id, result.detail)
        if self.on_status is not None:
            self.on_status(result)

    def _note(self, result: LockResult, status: LockStatus, detail: str | None) -> None:
        result.status, result.detail = status, detail
        result.history.append({"status": status.value, "at_ms": int(self._clock() * 1000), "detail": detail})
        log.info("lock %s: %s (%s)", result.action_id, status.value, detail)
        if self.on_status is not None:
            try:
                self.on_status(result)
            except Exception:
                log.exception("reporting a lock status failed")

    def result(self, action_id: str) -> LockResult | None:
        return self._results.get(action_id)

    def preflight(self) -> dict:
        """What would happen, without doing anything."""
        return {
            "mode": self.mode,
            "mechanism": self.mechanism,
            "accessibility_trusted": accessibility_trusted(),
            "password_after_sleep_setting": screen_lock_setting(),
            "screen_locked_now": self.is_locked(),
            "note": "A lock stops casual use of the keyboard and mouse. It does not stop programs already running.",
        }
