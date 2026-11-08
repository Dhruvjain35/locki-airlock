"""Loaded into the project's own pytest with ``-p locked_in_evidence``. Records what pytest itself
decided about each test, so nothing has to be read back out of human-readable output."""

import json
import os

_collected = []
_outcomes = {}
_messages = {}
_collect_errors = []


def _short(report) -> str:
    return str(report.longrepr)[-1500:] if report.longrepr else ""


def pytest_collection_finish(session):
    _collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.append({"nodeid": report.nodeid, "message": _short(report)})


def pytest_runtest_logreport(report):
    node = report.nodeid
    if report.when == "call":
        if hasattr(report, "wasxfail"):
            _outcomes[node] = "xpassed" if report.passed else "xfailed"
        else:
            _outcomes[node] = report.outcome
        if report.failed:
            _messages[node] = _short(report)
    elif report.failed:  # a fixture broke: the test did not pass, whatever the call did
        _outcomes[node] = "error"
        _messages[node] = _short(report)
    elif report.skipped and node not in _outcomes:
        _outcomes[node] = "skipped"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("LOCKEDIN_EVIDENCE_OUT")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"collected": _collected, "outcomes": _outcomes, "messages": _messages, "collect_errors": _collect_errors, "exit": int(exitstatus)},
            f,
        )
