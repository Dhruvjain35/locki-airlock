"""What a mission may read and run, and what the real pytest runner reports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.missions.evidence import run_pytest
from app.missions.trace import MissionTrace
from app.missions.workspace import (
    PolicyError,
    ProjectRegistry,
    Workspace,
    changed_tests,
    is_test_file,
    redact,
)

DEMO = Path(__file__).resolve().parents[2] / "examples" / "code_quest_demo"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\nfrom app import add\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\ndef test_ok():\n    assert True\n"
    )
    (tmp_path / ".env").write_text("FISH_AUDIO_API_KEY=abcdef123456\n")
    (tmp_path / "server.key").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "note.txt").write_text("x")
    return tmp_path


def test_reading_stays_inside_the_project_and_away_from_secrets(project, tmp_path_factory):
    w = Workspace(project)
    assert "def add" in w.read_text("app.py")
    for bad in ("../outside.py", "/etc/hosts", ".env", ".env.local", "server.key", ".venv/note.txt", "", "nope.py", "my_secrets.py"):
        with pytest.raises(PolicyError):
            w.read_text(bad)
    outside = tmp_path_factory.mktemp("outside") / "x.py"
    outside.write_text("secret")
    os.symlink(outside, project / "link.py")
    with pytest.raises(PolicyError):
        w.read_text("link.py")
    assert ".env" not in w.files() and "app.py" in w.files()


def test_secrets_inside_files_and_logs_are_redacted(tmp_path):
    text = 'FISH_AUDIO_API_KEY=abcdef123456 sk-abcdefghijklmnop1234 ghp_abcdefghijklmnopqrstuv password: "hunter2hunter" ' + str(Path.home()) + "/x"
    out = redact(text)
    for leaked in ("abcdef123456", "sk-abcdefghijklmnop1234", "ghp_abcdefghij", "hunter2hunter", str(Path.home())):
        assert leaked not in out
    t = MissionTrace(tmp_path / "m" / "trace.jsonl")
    t.record("tool", args={"key": "API_KEY=abcdef123456"}, path=str(Path.home()) + "/p")
    logged = (tmp_path / "m" / "trace.jsonl").read_text()
    assert "abcdef123456" not in logged and str(Path.home()) not in logged and '"kind": "tool"' in logged


def test_the_signature_notices_an_edit_and_ignores_closed_folders(project):
    w = Workspace(project)
    a = w.signature()
    (project / ".venv" / "note.txt").write_text("changed")
    assert w.signature() == a
    (project / "app.py").write_text("def add(a, b):\n    return a + b\n")
    assert w.signature() != a


def test_edited_or_deleted_tests_are_noticed_but_new_ones_are_not(project):
    w = Workspace(project)
    before = w.test_hashes()
    assert list(before) == ["tests/test_app.py"]
    (project / "tests" / "test_new.py").write_text("def test_n():\n    assert True\n")
    assert changed_tests(before, w.test_hashes()) == []
    (project / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n")
    assert changed_tests(before, w.test_hashes()) == ["tests/test_app.py"]
    (project / "tests" / "test_app.py").unlink()
    assert changed_tests(before, w.test_hashes()) == ["tests/test_app.py"]
    assert is_test_file("test_x.py") and is_test_file("a/tests/helpers.py") and is_test_file("conftest.py") and not is_test_file("app.py")


def test_only_a_real_project_folder_can_be_approved(tmp_path):
    reg = ProjectRegistry(tmp_path / "projects.json")
    home_project = Path.home() / ".locked-in-test-approve"
    home_project.mkdir(exist_ok=True)
    try:
        for bad in (Path.home(), Path.home().parent, "/", "/definitely/not/here", tmp_path):
            if Path(bad).resolve().is_relative_to(Path.home()) and Path(bad).resolve() != Path.home():
                continue  # the temp folder can sit inside home on some Macs
            with pytest.raises(PolicyError):
                reg.approve(bad)
        assert not reg.approved(home_project)
        reg.approve(home_project)
        assert reg.approved(home_project) and ProjectRegistry(tmp_path / "projects.json").approved(home_project)
    finally:
        home_project.rmdir()


async def test_real_pytest_reports_each_test_with_its_failure(project):
    r = await run_pytest(project, trigger="test")
    assert r.status == "ok" and r.exit_code == 1 and r.conclusive
    assert r.outcomes == {"tests/test_app.py::test_add": "failed", "tests/test_app.py::test_ok": "passed"}
    assert "assert" in r.messages["tests/test_app.py::test_add"]
    collected = await run_pytest(project, collect_only=True)
    assert collected.collected == list(r.outcomes)


async def test_the_shipped_demo_really_fails(tmp_path):
    r = await run_pytest(DEMO, trigger="test")
    assert r.status == "ok" and r.counts() == {"passed": 2, "failed": 7}


async def test_broken_code_is_inconclusive_not_a_failure(project):
    (project / "app.py").write_text("def add(:\n")
    r = await run_pytest(project)
    assert r.status == "broken" and not r.conclusive and r.collect_errors


async def test_no_tests_and_a_missing_folder_are_reported(tmp_path):
    (tmp_path / "x.py").write_text("a = 1\n")
    assert (await run_pytest(tmp_path)).status == "no_tests"
    missing = await run_pytest(tmp_path / "nope")
    assert missing.status == "error" and "not a folder" in missing.error


async def test_a_hung_test_is_stopped_and_proves_nothing(project):
    (project / "tests" / "test_app.py").write_text("import time\n\ndef test_hang():\n    time.sleep(60)\n")
    r = await run_pytest(project, timeout_s=1.5)
    assert r.status == "timeout" and not r.conclusive and r.duration_s < 5


async def test_tests_run_without_network_secrets_or_writes_outside_the_project(project, monkeypatch, tmp_path_factory):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "must-not-reach-the-tests")
    outside = Path.home() / ".locked-in-test-write"
    (project / "tests" / "test_app.py").write_text(
        "import os, socket, pathlib\n"
        "def test_no_key():\n    assert 'FISH_AUDIO_API_KEY' not in os.environ\n\n"
        "def test_no_network():\n"
        "    s = socket.socket()\n    s.settimeout(2)\n"
        "    assert s.connect_ex(('1.1.1.1', 443)) != 0\n\n"
        "def test_no_write_outside():\n"
        f"    p = pathlib.Path({str(outside)!r})\n"
        "    try:\n        p.write_text('x')\n    except OSError:\n        return\n    raise AssertionError('wrote outside the project')\n\n"
        "def test_write_inside():\n    pathlib.Path('inside.txt').write_text('x')\n"
    )
    r = await run_pytest(project)
    assert r.outcomes and all(v == "passed" for v in r.outcomes.values()), r.messages
    assert not outside.exists()
