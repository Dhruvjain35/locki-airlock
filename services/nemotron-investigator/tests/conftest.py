"""Shared test helpers: a scripted stand-in for Ollama and fixture paths.

The scripted model is ONLY used by deterministic unit tests of the control
loop and safety checks. Benchmarks and `-m live` tests use the real Nemotron.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from nemotron_investigator.config import Settings
from nemotron_investigator.ollama_client import OllamaClient

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "fixtures" / "scenarios"
A_PATH = SCENARIOS / "A_unknown_usb_armed.json"
A_ID = "incident:fixture-a-unknown-usb"  # fixtures use their own id space (incident:fixture-*)
MODEL = "nemotron-3.5-bolt:e4b"


def incident_of(name: str) -> str:
    return json.loads((SCENARIOS / f"{name}.json").read_text())["tables"]["incidents"][0]["id"]


def calls(*pairs):
    return {"kind": "tool_calls", "calls": list(pairs)}


def text(t="DONE"):
    return {"kind": "text", "text": t}


def final(**overrides):
    a = {
        "risk_assessment": {"rationale": "Unknown device while armed with no camera evidence.",
                            "level": "medium", "confidence": "medium"},
        "observed_facts": [],
        "possible_explanations": [],
        "missing_information": ["who connected the device"],
        "recommended_actions": [{"action": "Verify the device with the owner", "rationale": "unknown device"}],
        "summary": "Unknown device connected while armed.",
        "spoken_summary": "An unknown device was connected while the workstation was armed.",
    }
    a.update(overrides)
    return {"kind": "final", "assessment": a}


class ScriptedOllama:
    """httpx transport that replays a script of model responses."""

    def __init__(self, script, *, delay_s: float = 0.0, prompt_eval_count=100,
                 version: str = "scripted", digest: str = "test", loaded: list[dict] | None = None,
                 runner: str | None = None, loaded_after_first_chat: list[dict] | None = None):
        self.script = list(script)
        self.requests: list[dict] = []
        self.delay_s = delay_s
        self.prompt_eval_count = prompt_eval_count
        self.version, self.digest, self.loaded, self.runner = version, digest, loaded or [], runner
        self.loaded_after_first_chat = loaded_after_first_chat

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": self.digest,
                                                        "capabilities": ["tools"],
                                                        "details": {"runner": self.runner}}]})
        if path == "/api/ps":
            after = self.loaded_after_first_chat is not None and self.requests
            return httpx.Response(200, json={"models": self.loaded_after_first_chat if after else self.loaded})
        body = json.loads(request.content)
        self.requests.append(body)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not self.script:
            step = text("DONE")
        else:
            step = self.script.pop(0)
        if callable(step):
            step = step(body)
        if isinstance(step, Exception):
            raise step
        pec = self.prompt_eval_count(body) if callable(self.prompt_eval_count) else self.prompt_eval_count
        stats = {"total_duration": 5_000_000, "load_duration": 1_000, "prompt_eval_count": pec,
                 "prompt_eval_duration": 1_000_000, "eval_count": 20, "eval_duration": 2_000_000,
                 "done_reason": "stop"}
        if step["kind"] == "tool_calls":
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in step["calls"]]}
        elif step["kind"] == "text":
            msg = {"role": "assistant", "content": step["text"]}
        elif step["kind"] == "final":
            msg = {"role": "assistant", "content": json.dumps(step["assessment"])}
        elif step["kind"] == "raw":
            msg = {"role": "assistant", "content": step["content"]}
        else:
            raise AssertionError(step)
        if body.get("stream"):
            content = msg["content"]
            pieces = [content[i:i + 7] for i in range(0, len(content), 7)] or [""]
            lines = [json.dumps({"message": {"role": "assistant", "content": p}, "done": False}) for p in pieces]
            lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, **stats}))
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
        return httpx.Response(200, json={"message": msg, "done": True, **stats})


def request_tokens(body: dict, scale: float = 1.0) -> int:
    """Prompt tokens proportional to what was sent (about 3 chars per token), like a real runtime."""
    sent = len(json.dumps(body.get("messages") or [])) + len(json.dumps(body.get("tools") or []))
    return max(1, int(sent / 3 * scale))


def scripted_client(script, **kw) -> tuple[OllamaClient, ScriptedOllama]:
    s = ScriptedOllama(script, **kw)
    return OllamaClient("http://ollama.test", MODEL, transport=httpx.MockTransport(s.handler)), s


@pytest.fixture
def settings(tmp_path) -> Settings:
    return replace(Settings(), api_keys_file=str(tmp_path / "keys.json"), store_dir=str(tmp_path / "store"),
                   fixture_path=str(SCENARIOS), step_timeout_s=5.0, tool_timeout_s=1.0, total_timeout_s=20.0)
