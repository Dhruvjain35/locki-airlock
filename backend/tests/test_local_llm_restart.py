"""The language model runs in its own process. If that process dies or stops answering, it comes back by itself, a few
times at most, so a live demo carries on. A small stand-in worker keeps this test fast; the real one is in scripts/ (see docs/legacy/CODE_QUEST.md)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.intelligence import llm_worker
from app.intelligence.local_llm import MAX_WORKER_RESTARTS, STALLS_BEFORE_RESTART, LocalLLM, safe_generate


def stand_in_worker(conn, model_dir, stt_busy, voice_busy):
    """Speaks the worker's protocol. "die" in the context ends the process; "hang" stops answering until killed."""
    import os

    conn.send({"op": "ready", "load_ms": 1})
    while True:
        msg = conn.recv()
        if msg.get("op") == "stop":
            return
        if msg.get("op") != "generate":
            continue
        if "die" in msg["context"]:
            os._exit(1)
        if "hang" in msg["context"]:
            time.sleep(3600)
        conn.send({"op": "delta", "id": msg["id"], "text": "ok"})
        conn.send({"op": "done", "id": msg["id"], "finish": "stop", "text": "ok"})


async def until(llm, state, within=20.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if llm.state == state:
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def llm(tmp_path, monkeypatch):
    (tmp_path / "llm" / "m").mkdir(parents=True)
    (tmp_path / "llm" / "m" / "config.json").write_text("{}")
    monkeypatch.setattr(llm_worker, "worker_main", stand_in_worker)
    model = LocalLLM("m", tmp_path)
    yield model
    model.close()


async def test_a_dead_worker_comes_back_and_answers_again(llm):
    assert await until(llm, "ready")
    assert (await llm.generate("hello")).text == "ok"
    gen = await llm.generate("die now")
    assert not gen.ok and gen.finish == "error"
    assert await until(llm, "ready"), llm.error
    assert (await llm.generate("hello again")).text == "ok"
    assert llm.restarts == 1 and llm.describe()["restarts"] == 1


async def test_a_stuck_worker_is_stopped_after_several_requests_with_no_token(llm):
    assert await until(llm, "ready")
    for _ in range(STALLS_BEFORE_RESTART):
        gen = await llm.generate("hang here", timeout_s=0.1)
        assert gen.finish == "timeout"
    assert llm.state != "ready" or llm.restarts == 1
    assert await until(llm, "ready"), llm.error
    assert llm.restarts == 1 and (await llm.generate("hello")).text == "ok"


async def test_one_timeout_after_tokens_is_slowness_not_a_stuck_worker(llm):
    assert await until(llm, "ready")
    for _ in range(STALLS_BEFORE_RESTART + 1):
        gen = await llm.generate("hello")
        assert gen.ok
    assert llm.restarts == 0


async def test_it_gives_up_after_a_few_restarts_rather_than_looping(llm):
    assert await until(llm, "ready")
    for n in range(MAX_WORKER_RESTARTS):
        await llm.generate("die now")
        assert await until(llm, "ready"), llm.error
        assert llm.restarts == n + 1
    await llm.generate("die now")
    assert await until(llm, "error")
    assert llm.restarts == MAX_WORKER_RESTARTS and not (await llm.generate("hello")).ok


async def test_closing_does_not_restart_it(llm):
    assert await until(llm, "ready")
    llm.close()
    await asyncio.sleep(0.3)
    assert llm.restarts == 0


async def test_any_exception_from_a_model_call_becomes_a_failed_generation():
    async def boom():
        raise BrokenPipeError("gone")

    gen = await safe_generate(boom)
    assert not gen.ok and gen.finish == "error" and "BrokenPipeError" in gen.error
