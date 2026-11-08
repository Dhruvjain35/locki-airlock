"""The optional llama.cpp model backend (app/intelligence/llamacpp_llm.py) used to evaluate
NVIDIA Nemotron-3-Nano-4B. Missing files must leave it unavailable, never crash. The real
model (2.8 GB) runs only with LOCKEDIN_TEST_NEMOTRON=1."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from app.intelligence.actions import ActionName, parse_reply
from app.intelligence.llamacpp_llm import LlamaServerLLM, default_server
from app.intelligence.prompting import SAMPLE_CONTEXT

GGUF = Path(__file__).resolve().parents[1] / "models" / "llm-gguf" / "NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf"


def test_a_missing_model_file_is_unavailable(tmp_path):
    llm = LlamaServerLLM(tmp_path / "missing.gguf", server=tmp_path / "llama-server")
    assert llm.state == "unavailable" and "not downloaded" in llm.error
    g = asyncio.run(llm.generate("anything"))
    assert g.finish == "unavailable" and not g.ok
    llm.prefill("anything")  # no-op
    assert llm.memory() == {} and llm.describe()["state"] == "unavailable"
    llm.close()


def test_a_missing_server_binary_is_unavailable(tmp_path):
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    llm = LlamaServerLLM(gguf, server=tmp_path / "no-such-llama-server")
    assert llm.state == "unavailable" and "llama-server" in llm.error


@pytest.mark.skipif(os.environ.get("LOCKEDIN_TEST_NEMOTRON") != "1", reason="loads 2.8 GB: set LOCKEDIN_TEST_NEMOTRON=1")
async def test_nemotron_answers_in_json_and_reads_only_the_new_line_after_a_prefill():
    if not GGUF.exists() or default_server() is None:
        pytest.skip("Nemotron GGUF or llama-server not downloaded")
    llm = LlamaServerLLM(GGUF)
    try:
        t0 = time.monotonic()
        while llm.state == "loading" and time.monotonic() - t0 < 120:
            await asyncio.sleep(0.2)
        assert llm.state == "ready", llm.error
        ctx = SAMPLE_CONTEXT.replace("{utterance}", "Can you be quiet for a bit?")
        await llm.generate(ctx, max_tokens=40, temperature=0.0, timeout_s=30)  # fills the cache once
        llm.prefill(ctx[: ctx.index("\n\nUSER JUST SAID")])
        await asyncio.sleep(1.0)
        deltas: list[str] = []
        g = await llm.generate(ctx, max_tokens=90, temperature=0.0, timeout_s=20, on_delta=deltas.append)
        assert g.ok and "".join(deltas) == g.text and g.stats["prefilled"] is True
        assert g.stats["prompt_read"] < 100  # the persona and context came from the cache
        reply = parse_reply(g.text, frozenset(ActionName))
        assert reply.say and not [p for p in reply.problems if "JSON" in p]
    finally:
        llm.close()
