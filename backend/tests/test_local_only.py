"""The backend answers only this Mac's own dashboard: a foreign Origin, a rebinding Host and a body-less cross-site POST are refused
before any route runs; the dashboard, its dev proxy and requests with no Origin still work."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.local_only import loopback_origin
from app.main import create_app
from app.runtime import LockedInRuntime

from .test_voice_trials import RecordingTTS


@pytest.fixture
def client(settings):
    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    with TestClient(create_app(settings, runtime)) as c:
        c.runtime = runtime
        yield c


def test_a_foreign_origin_cannot_act(client):
    for origin in ("https://evil.example", "http://rebind.example:8077", "null", "http://127.0.0.1.evil.example"):
        r = client.post("/api/command", json={"text": "what happened"}, headers={"origin": origin})
        assert r.status_code == 403, origin
    assert not [t for t in client.runtime.voice_turns if t.get("source") == "typed"]


def test_a_body_less_cross_site_post_is_refused(client):
    # A "simple" request a hostile page can send with no CORS preflight: no body, text/plain.
    for path in ("/api/session/pause", "/api/sim/demo", "/api/experiments/false-trigger", "/api/vision/zone/detach"):
        assert client.post(path, headers={"origin": "https://evil.example", "content-type": "text/plain"}).status_code == 403, path
    assert client.delete("/api/sim/demo", headers={"origin": "https://evil.example"}).status_code == 403


def test_a_rebinding_host_is_refused_even_for_reads(client):
    # A rebinding page's requests are same-origin to the browser: its own host name, no preflight.
    assert client.get("/api/health", headers={"host": "rebind.example:8077"}).status_code == 403
    assert client.post("/api/command", json={"text": "what happened"}, headers={"host": "rebind.example:8077", "origin": "http://rebind.example:8077"}).status_code == 403
    assert client.get("/api/health", headers={"host": "127.0.0.1.evil.example"}).status_code == 403


def test_the_dashboard_its_proxy_and_local_scripts_still_work(client):
    for host in ("127.0.0.1:8077", "127.0.0.1:5174", "localhost:8077", "[::1]:8077"):
        assert client.get("/api/health", headers={"host": host}).status_code == 200, host
    # The vite proxy (changeOrigin) sends Host 127.0.0.1:8077 and keeps the page's Origin.
    ok = client.post("/api/command", json={"text": "how much time is left"}, headers={"host": "127.0.0.1:8077", "origin": "http://127.0.0.1:5174"})
    assert ok.status_code == 200
    assert client.post("/api/command", json={"text": "how much time is left"}, headers={"host": "127.0.0.1:8077"}).status_code == 200  # curl: no Origin
    assert client.get("/api/security", headers={"host": "127.0.0.1:8077", "origin": "https://evil.example"}).status_code == 200  # reads: CORS hides the reply


def test_websockets_are_local_only(client):
    with client.websocket_connect("/ws", headers={"host": "127.0.0.1:8077", "origin": "http://127.0.0.1:5174"}) as ws:
        assert ws.receive_json()
    for headers in ({"host": "127.0.0.1:8077", "origin": "https://evil.example"}, {"host": "rebind.example:8077", "origin": "http://rebind.example:8077"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers=headers) as ws:
                ws.receive_json()


def test_the_production_default_is_loopback_only(tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("LOCKEDIN_TRUSTED_HOSTS", raising=False)
    assert Settings(_env_file=None, data_dir=tmp_path).trusted_hosts == ["127.0.0.1", "localhost", "::1"]
    assert loopback_origin("http://localhost:5174") and loopback_origin("http://[::1]:5174")
    assert not loopback_origin("http://localhost.evil.example") and not loopback_origin("null") and not loopback_origin("file://")
