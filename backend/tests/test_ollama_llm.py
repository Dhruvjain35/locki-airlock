"""The Ollama adapter against a stand-in that speaks Ollama's HTTP protocol. No model runs here: results
with a real model come from scripts/agent_bench.py and are recorded in docs/legacy/CODE_QUEST.md."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.intelligence.ollama_llm import OllamaLLM

from .conftest import wait_for

MODEL = "nemotron-3.5-bolt:e4b"
REPLY = '{"action": "none", "args": {}, "say": "Hello there."}'


class FakeOllama:
    def __init__(self, models=(MODEL), chunks=None, delay=0.0):
        self.models, self.chunks, self.delay = list(models) if isinstance(models, tuple) else [models], chunks, delay
        self.requests: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json({"models": [{"name": m} for m in fake.models]})
                else:
                    self._json({"models": [{"name": MODEL, "size": 8 * 2**30, "size_vram": 8 * 2**30}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.requests.append(body)
                if not body.get("stream"):
                    self._json({"message": {"content": "ok"}, "done": True})
                    return
                self.send_response(200)
                self.end_headers()
                import time

                pieces = fake.chunks if fake.chunks is not None else [REPLY[:20], REPLY[20:]]
                try:
                    for p in pieces:
                        time.sleep(fake.delay)
                        self.wfile.write(json.dumps({"message": {"content": p}, "done": False}).encode() + b"\n")
                        self.wfile.flush()
                    time.sleep(fake.delay)
                    self.wfile.write(json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 10, "eval_duration": 10**9, "prompt_eval_duration": 10**9}).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.stop()


async def ready(llm) -> bool:
    return await wait_for(lambda: llm.state != "loading", timeout=3.0)


async def test_it_streams_a_reply_with_the_same_persona_as_the_other_models(fake):
    from app.intelligence.prompting import PERSONA

    llm = OllamaLLM(MODEL, base=fake.base)
    assert await ready(llm) and llm.state == "ready"
    deltas = []
    g = await llm.generate("hello", on_delta=deltas.append, timeout_s=3.0)
    assert g.ok and g.text == REPLY and "".join(deltas) == REPLY and g.first_token_ms is not None
    body = fake.requests[-1]
    assert body["model"] == MODEL and body["think"] is False and body["stream"] is True
    assert body["messages"] == [{"role": "system", "content": PERSONA}, {"role": "user", "content": "hello"}]
    assert llm.memory()["footprint_mb"] == 8192.0
    assert llm.describe()["provider"] == "ollama" and llm.describe()["p50_ms"] is not None


async def test_a_missing_model_is_reported_and_never_fetched(fake):
    llm = OllamaLLM("some-other-model:e4b", base=fake.base)
    assert await ready(llm) and llm.state == "unavailable" and "ollama pull" in llm.error
    assert not fake.requests, "nothing was asked of the server"
    g = await llm.generate("hello")
    assert not g.ok and g.finish == "unavailable"


@pytest.mark.parametrize("model", ["nemotron-3.5-bolt", "gpt-oss:20b-cloud", "nemotron-3.5-bolt:cloud"])
def test_cloud_models_and_untagged_names_are_refused(model):
    llm = OllamaLLM(model)
    assert llm.state == "unavailable" and llm.error


def test_only_this_mac_can_be_the_server():
    with pytest.raises(ValueError):
        OllamaLLM(MODEL, base="https://ollama.example.com")


def test_the_conversation_uses_ollama_only_where_it_serves_the_model(fake, monkeypatch):
    from types import SimpleNamespace

    from app.intelligence import companion as companion_mod
    from app.intelligence.ollama_llm import serves

    assert serves(fake.base, MODEL) and not serves(fake.base, "some-other-model:e4b")
    assert not serves("http://127.0.0.1:9", MODEL)  # nothing listening: the M5 tunnel is down
    assert not serves("http://192.168.1.5:11434", MODEL) and not fake.requests  # loopback only, and nothing generated or pulled
    s = SimpleNamespace(ollama_url=fake.base, ollama_model=MODEL, local_llm_model="Qwen3-4B-Instruct-2507-4bit")
    assert companion_mod.Companion._ollama_serves(s)
    assert not companion_mod.Companion._ollama_serves(SimpleNamespace(**{**vars(s), "ollama_url": "http://127.0.0.1:9"}))


async def test_ollama_not_running_is_an_error_not_a_crash():
    llm = OllamaLLM(MODEL, base="http://127.0.0.1:9")
    assert await ready(llm) and llm.state == "unavailable" and "not running" in llm.error


async def test_a_slow_reply_times_out_and_counts_as_failed():
    f = FakeOllama(chunks=['{"action"', ': "none"', ', "args"', ': {}'], delay=0.4)
    try:
        llm = OllamaLLM(MODEL, base=f.base, warm=False)
        assert await ready(llm)
        g = await llm.generate("hello", timeout_s=0.5)
        assert not g.ok and g.finish == "timeout" and llm.failures == 1
    finally:
        f.stop()


async def test_a_person_talking_cancels_a_background_line():
    f = FakeOllama(chunks=["{" + '"a":1,' * 3] * 8, delay=0.1)
    try:
        llm = OllamaLLM(MODEL, base=f.base, warm=False)
        assert await ready(llm)
        background = asyncio.create_task(llm.generate("prepare", background=True, timeout_s=5.0))
        await asyncio.sleep(0.25)
        person = asyncio.create_task(llm.generate("now", timeout_s=5.0))
        g = await background
        assert g.finish == "cancelled"
        await person
    finally:
        f.stop()


async def test_the_runtime_uses_the_backend_it_is_told_to(tmp_path, settings):
    from app.intelligence.companion import Companion
    from app.runtime import LockedInRuntime

    from .conftest import RecordingTTS

    s = settings.model_copy(update={"llm_provider": "local", "local_llm_backend": "ollama", "ollama_model": "gpt-oss:cloud"})
    rt = LockedInRuntime(s, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    assert isinstance(rt.companion, Companion)
    rt.companion.start()
    assert rt.companion.llm.provider == "ollama" and rt.companion.llm.state == "unavailable" and not rt.companion.available


async def test_the_investigator_can_send_its_own_rules_and_a_json_schema():
    from app.intelligence.ollama_llm import OllamaLLM

    llm = OllamaLLM.__new__(OllamaLLM)
    llm.model, llm.keep_alive = "nemotron-3.5-bolt:e4b", "30m"
    body = llm._body("the incident", 100, 0.2, 0.9, None, system="the rules", response_format={"type": "object"})
    assert body["messages"][0] == {"role": "system", "content": "the rules"} and body["format"] == {"type": "object"}
    plain = llm._body("hello", 100, 0.7, 0.9, None)
    assert "format" not in plain and plain["messages"][0]["content"] != "the rules"


def test_ollama_may_be_on_another_of_the_teams_macs_and_nowhere_else():
    import pytest as _pytest

    from app.intelligence.ollama_llm import OllamaLLM, lan_address_ok

    for good in ("http://127.0.0.1:11434", "http://localhost:11434", "http://192.168.1.20:11434", "http://10.155.146.20:11434", "http://172.16.0.9:11434", "http://169.254.3.3:11434", "http://mustafas-mac.local:11434"):
        assert lan_address_ok(good), good
    for bad in ("https://ollama.com", "http://ollama.com:11434", "http://8.8.8.8:11434", "http://example.com:11434", "ftp://192.168.1.2", "http://192.168.1.20:11434/proxy", "https://192.168.1.20:11434"):
        assert not lan_address_ok(bad), bad
    with _pytest.raises(ValueError):
        OllamaLLM("nemotron-3.5-bolt:e4b", base="http://8.8.8.8:11434", allow_lan=True)
    with _pytest.raises(ValueError):
        OllamaLLM("nemotron-3.5-bolt:e4b", base="http://192.168.1.20:11434")  # the conversation model stays on this Mac unless asked
