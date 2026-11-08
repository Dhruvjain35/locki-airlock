"""The camera and microphone turn on only when someone asks: a dashboard button, or the explicit
flags `make hardware` sets. A setting saved by an earlier run is not a request."""

from __future__ import annotations

from app.config import RuntimeSettings, Settings
from app.runtime import LockedInRuntime

from .conftest import RecordingTTS


async def devices_started_at_boot(tmp_path, *, saved_camera_on: bool, autostart: bool) -> list[str]:
    settings = Settings(_env_file=None, data_dir=tmp_path, robot_driver="mock", mock_autoconnect=True, vision_preload=False, vision_autostart=autostart, voice_autostart=autostart)
    rs = RuntimeSettings()
    rs.vision_enabled = saved_camera_on
    rt = LockedInRuntime(settings, runtime_settings=rs, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=True, enable_voice=True, mock_kwargs={})
    started: list[str] = []

    async def camera(*_a, **_k):
        started.append("camera")

    async def microphone(*_a, **_k):
        started.append("microphone")

    rt.start_vision, rt.start_voice = camera, microphone
    await rt.start()
    await rt.shutdown()
    return started


async def test_a_camera_left_on_last_time_stays_off_after_a_restart(tmp_path):
    assert await devices_started_at_boot(tmp_path, saved_camera_on=True, autostart=False) == []


async def test_a_plain_start_opens_nothing(tmp_path):
    assert await devices_started_at_boot(tmp_path, saved_camera_on=False, autostart=False) == []


async def test_make_hardware_flags_still_start_both(tmp_path):
    assert await devices_started_at_boot(tmp_path, saved_camera_on=False, autostart=True) == ["camera", "microphone"]
