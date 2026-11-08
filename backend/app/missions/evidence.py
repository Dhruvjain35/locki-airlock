"""Runs a project's tests and reports what really happened. This is the only source of progress.

The command is fixed: the project's Python, ``-m pytest``, no shell and no arguments from a model
(``select`` must be test ids pytest itself reported). It runs in a macOS sandbox with outbound
connections blocked and writes limited to the project and temp folders, with a scrubbed environment
(no API keys), a timeout, and its whole process group killed when the time is up.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_plugin"
DEFAULT_TIMEOUT_S = 60.0
OUTPUT_TAIL = 6000
HOME = str(Path.home())
SECRET_DIRS = (f"{HOME}/.ssh", f"{HOME}/.aws", f"{HOME}/.gnupg", f"{HOME}/.config/gcloud")


def _sandbox_profile(project: Path, tmp: str) -> str:
    deny_read = "".join(f'(deny file-read* (subpath "{d}"))' for d in SECRET_DIRS)
    return (
        '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
        "(allow network-outbound (remote unix-socket))"
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
        f"{deny_read}(deny file-write*)"
        f'(allow file-write* (subpath "{project.resolve()}"))(allow file-write* (subpath "{Path(tmp).resolve()}"))'
        '(allow file-write* (subpath "/private/var/folders"))(allow file-write* (subpath "/private/tmp"))(allow file-write* (subpath "/dev"))'
    )


def project_python(project: Path) -> str:
    """The project's own virtual environment when it has one, else the backend's Python."""
    for rel in (".venv/bin/python", "venv/bin/python"):
        candidate = project / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


@dataclass
class TestRun:
    __test__ = False  # not a pytest class

    run_id: str
    at: float
    project: str
    trigger: str
    status: str  # ok | broken | no_tests | timeout | error
    exit_code: int | None
    duration_s: float
    command: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    collect_errors: list[dict] = field(default_factory=list)
    output_tail: str = ""
    error: str | None = None

    @property
    def conclusive(self) -> bool:
        """Every test got a verdict. Anything else says nothing about the code either way."""
        return self.status == "ok"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.outcomes.values():
            out[v] = out.get(v, 0) + 1
        return out

    def to_payload(self) -> dict:
        p = asdict(self)
        p["counts"] = self.counts()
        p.pop("collected")
        p["messages"] = {k: v[-600:] for k, v in self.messages.items()}
        p["output_tail"] = self.output_tail[-1500:]
        return p


def _status_for(exit_code: int | None, evidence: dict | None, collect_only: bool) -> str:
    # pytest: 0 all passed, 1 some failed, 2 interrupted or collection errors, 3 internal, 4 usage, 5 none collected
    if evidence is None or exit_code is None:
        return "error"
    if evidence.get("collect_errors") or exit_code == 2:
        return "broken"
    if exit_code == 5 or (collect_only and not evidence.get("collected")):
        return "no_tests"
    if exit_code in (0, 1):
        return "ok"
    return "error"


async def run_pytest(
    project: Path,
    *,
    trigger: str = "manual",
    select: list[str] | None = None,
    collect_only: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
) -> TestRun:
    project = Path(project).resolve()
    run = TestRun(uuid.uuid4().hex[:10], time.time(), str(project), trigger, "error", None, 0.0)
    if not project.is_dir():
        run.error = f"not a folder: {project}"
        return run
    py = python or project_python(project)
    cmd = [py, "-m", "pytest", "-p", "locked_in_evidence", "-p", "no:cacheprovider", "-q", "--tb=short", "--color=no"]
    if collect_only:
        cmd.append("--collect-only")
    cmd += list(select or [])
    run.command = ["python", *cmd[1:]]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="locked-in-evidence-") as tmp:
        out_path = os.path.join(tmp, "evidence.json")
        env = {
            "PATH": os.pathsep.join([str(Path(py).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            "HOME": HOME,
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TMPDIR": tmp,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PLUGIN_DIR),
            "LOCKEDIN_EVIDENCE_OUT": out_path,
        }
        full = (["sandbox-exec", "-p", _sandbox_profile(project, tmp), *cmd]) if shutil.which("sandbox-exec") else cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                *full, cwd=str(project), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            run.error = f"could not start the tests: {exc}"
            run.duration_s = round(time.monotonic() - t0, 2)
            return run
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            run.status, run.error = "timeout", f"the tests were still running after {timeout_s:.0f} s and were stopped"
            _kill_group(proc)
            await proc.wait()
            run.duration_s = round(time.monotonic() - t0, 2)
            return run
        except asyncio.CancelledError:
            _kill_group(proc)
            raise
        run.exit_code = proc.returncode
        run.duration_s = round(time.monotonic() - t0, 2)
        run.output_tail = (out or b"").decode("utf-8", "replace")[-OUTPUT_TAIL:]
        evidence = None
        if os.path.exists(out_path):
            try:
                with open(out_path, encoding="utf-8") as f:
                    evidence = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                run.error = f"evidence file unreadable: {exc}"
        if evidence is None and run.error is None:
            run.error = "pytest did not report anything (is pytest installed in this project's Python?)"
    run.status = _status_for(run.exit_code, evidence, collect_only)
    if evidence is not None:
        run.collected = evidence.get("collected", [])
        run.outcomes = evidence.get("outcomes", {})
        run.messages = evidence.get("messages", {})
        run.collect_errors = evidence.get("collect_errors", [])
    return run


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
