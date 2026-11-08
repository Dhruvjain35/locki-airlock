"""Locking: dry run by default, every check, one lock per action, and three states that are each reported only when true. A real lock is
never run by these tests: the mechanism is a stand-in, and tests check it was or was not called."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from app.security import mac_lock
from app.security.contracts import LockRequestV1, LockStatus
from app.security.mac_lock import MECHANISMS, LockAdapter
from app.security.session import ResponsePolicy, new_local_session

FUTURE = lambda: int(time.time() * 1000) + 30000  # noqa: E731


class Screen:
    def __init__(self, locked=False):
        self.locked = locked

    def is_locked(self):
        return self.locked


def session(auto_lock=True):
    return new_local_session({}, ResponsePolicy(auto_lock=auto_lock))


def request(s, action="a1", **kw):
    body = {"session_id": s.session_id, "action_id": action, "expiry": FUTURE(), "reason": "Unknown USB device while armed", "owner_policy_hash": s.policy.digest(), **kw}
    return LockRequestV1(**body)


def adapter(mode="real", runner=None, screen=None, **kw):
    calls = []

    def run():
        calls.append(1)
        if runner is not None:
            return runner()
        if screen is not None:
            screen.locked = True
        return True, None

    scr = screen if screen is not None else Screen()
    kw.setdefault("confirm_within_s", 0.5)
    return LockAdapter(mode=mode, mechanism="keystroke", screen=scr, runners={"keystroke": run}, **kw), calls


def wait_status(result, status, within=2.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if result.status == status:
            return True
        time.sleep(0.01)
    return False


def test_the_default_is_a_dry_run_that_runs_nothing():
    a, calls = adapter(mode="dry_run")
    s = session()
    r = a.request(request(s), s)
    assert calls == [] and r.status == LockStatus.LOCK_DRY_RUN and r.dry_run and r.executed_at_ms is None and r.confirmed_at_ms is None
    assert [h["status"] for h in r.history] == ["LOCK_REQUESTED", "LOCK_DRY_RUN"] and "was not locked" in r.detail
    assert LockAdapter().mode == "dry_run"


def test_a_real_lock_goes_through_all_three_states_only_when_each_is_true():
    screen = Screen()
    a, calls = adapter(screen=screen)
    s = session()
    r = a.request(request(s), s)
    assert wait_status(r, LockStatus.LOCK_CONFIRMED) and calls == [1]
    assert [h["status"] for h in r.history] == ["LOCK_REQUESTED", "LOCK_EXECUTED", "LOCK_CONFIRMED"]
    assert r.requested_at_ms <= r.executed_at_ms <= r.confirmed_at_ms and r.mechanism == "keystroke" and not r.dry_run


def test_a_lock_the_os_does_not_report_stays_executed_and_says_so():
    screen = Screen()
    a, calls = adapter(runner=lambda: (True, None), screen=screen, confirm_within_s=0.15)
    s = session()
    r = a.request(request(s), s)
    time.sleep(0.4)
    assert r.status == LockStatus.LOCK_EXECUTED and r.confirmed_at_ms is None and "did not report" in r.detail and calls == [1]


def test_a_failed_mechanism_is_reported_and_never_as_success():
    a, calls = adapter(runner=lambda: (False, "needs Accessibility permission"))
    s = session()
    r = a.request(request(s), s)
    assert r.status == LockStatus.LOCK_FAILED and "Accessibility" in r.detail and r.executed_at_ms is None and r.confirmed_at_ms is None and calls == [1]


def test_an_exception_in_the_mechanism_is_a_failure():
    def boom():
        raise RuntimeError("osascript exploded")

    a, _ = adapter(runner=boom)
    s = session()
    r = a.request(request(s), s)
    assert r.status == LockStatus.LOCK_FAILED and "exploded" in r.detail


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda s: request(s, expiry=int(time.time() * 1000) - 1), "expired"),
        (lambda s: request(s, session_id="some-other-session"), "different session"),
        (lambda s: request(s, owner_policy_hash="deadbeefdeadbeef"), "policy"),
    ],
)
def test_a_request_that_does_not_check_out_locks_nothing(make, why):
    a, calls = adapter()
    s = session()
    r = a.request(make(s), s)
    assert r.status == LockStatus.LOCK_REFUSED and why in r.detail and calls == []


def test_a_fixture_can_never_lock_a_real_screen():
    a, calls = adapter()
    s = session()
    r = a.request(request(s, is_fixture=True), s)
    assert r.status == LockStatus.LOCK_REFUSED and "fixture" in r.detail and calls == []


def test_without_an_armed_session_nothing_locks():
    a, calls = adapter()
    s = session()
    assert a.request(request(s), None).status == LockStatus.LOCK_REFUSED and calls == []
    old = new_local_session({}, ResponsePolicy(auto_lock=True), duration_s=0, now_ms=0)
    assert a.request(request(old, action="a2"), old).status == LockStatus.LOCK_REFUSED and calls == []


def test_automatic_locking_needs_the_owners_opt_in():
    a, calls = adapter()
    s = session(auto_lock=False)
    r = a.request(request(s), s)
    assert r.status == LockStatus.LOCK_REFUSED and "opted in" in r.detail and calls == []


def test_the_owner_can_ask_for_a_lock_without_having_opted_in_to_automatic_ones():
    screen = Screen()
    a, calls = adapter(screen=screen)
    s = session(auto_lock=False)
    r = a.request(request(s), s, owner_initiated=True)
    assert wait_status(r, LockStatus.LOCK_CONFIRMED) and calls == [1]


def test_the_same_action_locks_once_and_returns_the_same_result():
    a, calls = adapter(screen=Screen())
    s = session()
    first = a.request(request(s), s)
    second = a.request(request(s), s)
    assert first is second and calls == [1]


def test_a_new_action_during_the_cooldown_is_refused_so_the_owner_is_not_locked_in_a_loop():
    now = [100.0]
    a, calls = adapter(screen=Screen(), mono=lambda: now[0], cooldown_s=60.0, confirm_within_s=0.0)
    s = session()
    a.request(request(s, action="a1"), s)
    r = a.request(request(s, action="a2"), s)
    assert r.status == LockStatus.LOCK_REFUSED and "moments ago" in r.detail and calls == [1]
    a._screen = Screen(False)
    now[0] += 61
    a.request(request(s, action="a3"), s)
    assert calls == [1, 1]


def test_automatic_locks_wait_after_the_owner_comes_back():
    now = [100.0]
    a, calls = adapter(mono=lambda: now[0], owner_grace_s=120.0, confirm_within_s=0.0)
    s = session()
    a.owner_returned()
    r = a.request(request(s), s)
    assert r.status == LockStatus.LOCK_REFUSED and "came back" in r.detail and calls == []
    assert a.request(request(s, action="a2"), s, owner_initiated=True).status != LockStatus.LOCK_REFUSED, "the owner can always ask"


def test_an_already_locked_screen_is_not_locked_again():
    a, calls = adapter(screen=Screen(locked=True))
    s = session()
    r = a.request(request(s), s)
    assert r.status == LockStatus.LOCK_REFUSED and "already locked" in r.detail and calls == []


def test_only_listed_mechanisms_exist_and_none_takes_an_argument():
    assert set(MECHANISMS) == {"keystroke", "login_framework"}
    with pytest.raises(ValueError):
        LockAdapter(mechanism="pmset displaysleepnow")


def test_the_module_never_runs_anything_but_the_fixed_commands():
    source = Path(mac_lock.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("run", "Popen", "system", "call", "check_output"):
            first = node.args[0]
            assert isinstance(first, ast.List) and isinstance(first.elts[0], (ast.Name, ast.Constant)), ast.unparse(node)
            assert not any(isinstance(e, (ast.JoinedStr, ast.BinOp)) for e in first.elts), ast.unparse(node)
    code = source.split('"""', 2)[2]  # the module's description names what it refuses to do
    for word in ("pmset", "networksetup", "ifconfig", "shutdown", "shell=True", "os.kill", "SIGKILL", ".terminate(", "os.remove", "unlink"):
        assert word not in code, word


def test_status_changes_are_reported_as_they_happen():
    seen = []
    screen = Screen()
    a, _ = adapter(screen=screen, on_status=lambda r: seen.append(r.status))
    s = session()
    r = a.request(request(s), s)
    assert wait_status(r, LockStatus.LOCK_CONFIRMED)
    assert seen[:3] == [LockStatus.LOCK_REQUESTED, LockStatus.LOCK_EXECUTED, LockStatus.LOCK_CONFIRMED]


def test_a_keystroke_without_accessibility_permission_fails_clearly(monkeypatch):
    monkeypatch.setattr(mac_lock, "accessibility_trusted", lambda: False)
    ran = []
    monkeypatch.setattr(mac_lock.subprocess, "run", lambda *a, **k: ran.append(a))
    ok, detail = mac_lock.run_keystroke()
    assert not ok and "Accessibility" in detail and ran == []


def test_preflight_does_nothing_but_read(monkeypatch):
    a, calls = adapter(mode="dry_run")
    monkeypatch.setattr(mac_lock, "screen_lock_setting", lambda: "off")
    info = a.preflight()
    assert calls == [] and info["password_after_sleep_setting"] == "off" and info["mode"] == "dry_run" and info["screen_locked_now"] is False
