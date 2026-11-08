"""Ollama origin boundary: in-memory transports only, no sockets or model loading."""
from __future__ import annotations

import json

import httpx
import pytest

from app.intelligence import ollama_llm
from app.intelligence.ollama_llm import OllamaLLM, lan_address_ok, local_ollama_endpoint, serves


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434@outside.example", "http://localhost:11435@outside.example",
    "http://outside.example:11434", "http://localhost.outside.example:11434",
    "http://user:password@127.0.0.1:11434", "http://@localhost:11434",
    "http://127.0.0.1:11434/api", "http://127.0.0.1:11434//",
    "http://127.0.0.1:11434?proxy=outside", "http://127.0.0.1:11434?",
    "http://127.0.0.1:11434#outside", "http://127.0.0.1:11434#",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:abc", "http://localhost:",
    "http://[::1", "http://[::1%eth0]:11434", "http://127.1:11434", "http://2130706433:11434",
    "http://192.168.1.20:11434", "http://machine.local:11434", "https://127.0.0.1:11434",
    " http://127.0.0.1:11434", "http://local\nhost:11434", "http://127.0.0.1:11434\t",
    "http://127.0.0.1:11434/\x00", "http://localhost:11434\\@outside.example",
])
def test_loopback_boundary_rejects_bad_origins_before_any_probe_or_loader(url, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid endpoint must not reach network or loader")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(ollama_llm.threading.Thread, "start", forbidden)
    with pytest.raises(ValueError):
        local_ollama_endpoint(url)
    assert not serves(url, "fixture:model")
    with pytest.raises(ValueError):
        OllamaLLM("fixture:model", base=url)


@pytest.mark.parametrize(("url", "expected"), [
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://localhost:11435/", "http://localhost:11435"),
    ("http://[::1]:11435/", "http://[::1]:11435"),
    ("http://LOCALHOST:11434", "http://localhost:11434"),
    ("http://localhost", "http://localhost"),
    ("http://[::1]/", "http://[::1]"),
    ("http://127.0.0.1:65535", "http://127.0.0.1:65535"),
])
def test_exact_loopback_and_ssh_ports_are_preserved_without_guessing_port(url, expected):
    assert local_ollama_endpoint(url) == expected
    # A refused cloud model initializes configuration but never starts a loader.
    llm = OllamaLLM("fixture:cloud", base=url)
    assert llm.base == expected and llm.state == "unavailable"


@pytest.mark.parametrize("url", [
    "http://user@192.168.1.20:11434", "http://@machine.local:11434",
    "http://192.168.1.20:11434?", "http://machine.local:11434#",
    "http://machine.local:65536", "http://192.168.1.20:0", "http://machine.local:",
    "http://192.168.1.20:11434/proxy", "http://192.168.1.20:11434\n",
])
def test_explicit_lan_option_does_not_bypass_origin_validation(url):
    assert not lan_address_ok(url)
    with pytest.raises(ValueError):
        OllamaLLM("fixture:cloud", base=url, allow_lan=True)


def test_explicit_private_lan_option_remains_distinct_from_default_loopback():
    endpoint = "http://192.168.1.20:11434/"  # documentation-only fixture address
    assert lan_address_ok(endpoint)
    with pytest.raises(ValueError):
        local_ollama_endpoint(endpoint)
    assert OllamaLLM("fixture:cloud", base=endpoint, allow_lan=True).base == endpoint.rstrip("/")


def test_probe_and_load_never_follow_redirects_or_use_ambient_proxy(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(302, headers={"Location": "http://outside.example/api/tags"})
        return httpx.Response(200, json={"models": []})

    def get(url, **kwargs):
        assert kwargs.pop("trust_env") is False
        assert kwargs.pop("follow_redirects") is False
        with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    assert not serves("http://localhost:11435", "fixture:model")
    llm = OllamaLLM.__new__(OllamaLLM)
    llm.base, llm.model = "http://localhost:11435", "fixture:model"
    llm._load(1.0, False)
    assert llm.state == "unavailable"
    llm.memory()
    assert len(requests) == 3 and {r.url.host for r in requests} == {"localhost"}


def test_probe_can_find_model_on_ssh_forward_without_loading_or_generating(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"models": [{"name": "fixture:model"}]})

    monkeypatch.setattr(httpx, "get", get)
    assert serves("http://127.0.0.1:11435/", "fixture:model")
    assert calls == [("http://127.0.0.1:11435/api/tags", {"timeout": 1.5, "trust_env": False, "follow_redirects": False})]


async def test_generation_uses_no_proxy_or_redirect_with_in_memory_transport(monkeypatch):
    monkeypatch.setattr(ollama_llm.threading.Thread, "start", lambda _self: None)
    llm = OllamaLLM("fixture:model", base="http://localhost:11435/", warm=False)
    llm.state = "ready"
    requests = []
    client_type = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        reply = json.dumps({"message": {"content": '{"say":"fixture"}'}, "done": True}) + "\n"
        return httpx.Response(200, text=reply)

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await llm.generate("fixture prompt")
    assert result.ok and len(requests) == 1
    assert str(requests[0].url) == "http://localhost:11435/api/chat"
