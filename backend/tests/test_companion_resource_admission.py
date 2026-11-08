"""No model, microphone or network: startup resource admission and routing only."""
from types import SimpleNamespace

import pytest

from app.intelligence import companion, local_llm, ollama_llm


def resources(monkeypatch, *, available=16 * 1024**3, swap=0):
    monkeypatch.setattr(companion.psutil, "virtual_memory", lambda: SimpleNamespace(available=available))
    monkeypatch.setattr(companion.psutil, "swap_memory", lambda: SimpleNamespace(percent=swap))


def installed(tmp_path):
    folder = tmp_path / "llm" / "test-model"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"weights")
    return folder


def runtime(tmp_path, *, backend="ollama", url="http://127.0.0.1:11435"):
    settings = SimpleNamespace(llm_provider="local", local_llm_backend=backend,
                               ollama_url=url, ollama_model="nemotron-3.5-bolt:e4b",
                               local_llm_model="test-model", models_dir=tmp_path, local_llm_timeout_s=10)
    return SimpleNamespace(settings=settings, enable_llm=True,
                           bus=SimpleNamespace(add_listener=lambda listener: None),
                           rs=SimpleNamespace(conversation=SimpleNamespace(generated_replies=True)))


class FakeModel:
    state = "ready"

    def __init__(self, model, *args, **kwargs):
        self.model, self.options = model, kwargs


@pytest.mark.parametrize("available,swap", [(1024**3, 0), (16 * 1024**3, 90), (16 * 1024**3, float("nan")), (-1, 0)])
def test_unsafe_or_unknown_resources_never_construct_local_model(tmp_path, monkeypatch, available, swap):
    installed(tmp_path)
    resources(monkeypatch, available=available, swap=swap)
    monkeypatch.setattr(ollama_llm, "serves", lambda *a: False)
    monkeypatch.setattr(local_llm, "LocalLLM", lambda *a: pytest.fail("must not construct MLX worker"))
    c = companion.Companion(runtime(tmp_path))
    c.start()
    assert not c.available
    assert c.llm.state == "unavailable"
    assert not c.llm.describe()["admission"]["ready"]
    c.close()


def test_healthy_local_fallback_is_preserved(tmp_path, monkeypatch):
    installed(tmp_path)
    resources(monkeypatch)
    monkeypatch.setattr(ollama_llm, "serves", lambda *a: False)
    monkeypatch.setattr(local_llm, "LocalLLM", FakeModel)
    c = companion.Companion(runtime(tmp_path))
    c.start()
    assert isinstance(c.llm, FakeModel)
    assert c.llm.model == "test-model"
    assert c.available


def test_explicit_mlx_uses_same_admission(tmp_path, monkeypatch):
    installed(tmp_path)
    resources(monkeypatch, available=1024**3)
    monkeypatch.setattr(local_llm, "LocalLLM", lambda *a: pytest.fail("must not construct MLX worker"))
    c = companion.Companion(runtime(tmp_path, backend="mlx"))
    c.start()
    assert c.llm.state == "unavailable"


def test_unknown_weight_size_fails_closed(tmp_path, monkeypatch):
    resources(monkeypatch)
    assert not companion.local_model_admission("test-model", tmp_path)["ready"]
    folder = installed(tmp_path)
    (folder / "model.safetensors").unlink()
    assert not companion.local_model_admission("test-model", tmp_path)["ready"]
    (folder / "model.safetensors").touch()
    assert not companion.local_model_admission("test-model", tmp_path)["ready"]


def test_admission_sizes_all_configured_shards_without_loading(tmp_path, monkeypatch):
    folder = installed(tmp_path)
    # Sparse fixtures measure file sizes; they contain no model and consume little storage.
    with (folder / "model.safetensors").open("wb") as f:
        f.truncate(2 * 1024**3)
    with (folder / "model-2.safetensors").open("wb") as f:
        f.truncate(1024**3)
    resources(monkeypatch, available=7 * 1024**3)
    decision = companion.local_model_admission("test-model", tmp_path)
    assert decision["weight_bytes"] == 3 * 1024**3
    assert decision["minimum_available_bytes"] == 8 * 1024**3
    assert not decision["ready"]
    resources(monkeypatch, available=8 * 1024**3, swap=30)
    assert companion.local_model_admission("test-model", tmp_path)["ready"]


def test_remote_serving_model_never_uses_local_guard_or_warmup(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_llm, "serves", lambda *a: True)
    monkeypatch.setattr(ollama_llm, "OllamaLLM", FakeModel)
    monkeypatch.setattr(companion, "local_model_admission", lambda *a: pytest.fail("remote model is not M4 resident"))
    monkeypatch.setattr(local_llm, "LocalLLM", lambda *a: pytest.fail("no local fallback"))
    c = companion.Companion(runtime(tmp_path))
    c.start()
    assert c.llm.options == {"base": "http://127.0.0.1:11435", "warm": False}
    assert c.available


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434@outside.example", "http://localhost:11434/proxy", "https://127.0.0.1:11434"])
def test_invalid_ollama_endpoint_does_not_probe_or_fall_back(tmp_path, monkeypatch, url):
    monkeypatch.setattr(ollama_llm, "serves", lambda *a: pytest.fail("must not probe refused URL"))
    monkeypatch.setattr(local_llm, "LocalLLM", lambda *a: pytest.fail("must not fall back"))
    c = companion.Companion(runtime(tmp_path, url=url))
    c.start()
    assert c.llm.provider == "ollama"
    assert c.llm.state == "unavailable"
    assert not c.available


def test_resource_probe_failure_fails_closed(tmp_path, monkeypatch):
    installed(tmp_path)
    def failed():
        raise OSError("probe unavailable")
    monkeypatch.setattr(companion.psutil, "virtual_memory", failed)
    assert not companion.local_model_admission("test-model", tmp_path)["ready"]
