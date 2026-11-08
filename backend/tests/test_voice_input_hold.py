"""Recorded-input holds invalidate old live audio without restarting capture."""

import queue
import threading
from types import SimpleNamespace

import numpy as np

from app.voice.service import VoiceService


def voice(emit, transcriber=None):
    service = VoiceService(SimpleNamespace(vad_engine="energy", models_dir=None), emit, transcriber=transcriber)
    service.running = True
    return service


def test_inflight_and_queued_live_stt_stay_stale_after_hold_release():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    seen, calls = [], []

    def transcribe(audio):
        value = int(audio[0])
        calls.append(value)
        if value == 1:
            entered.set()
            assert release.wait(2)
        return {"text": "arm my setup" if value == 1 else "What happened today?", "confidence": 1.0}

    def emit(kind, payload):
        if kind == "utterance":
            seen.append(payload)
            done.set()

    service = voice(emit, SimpleNamespace(state="ready", transcribe=transcribe))
    epoch = service.capture_epoch
    service._submit(np.array([1], dtype=np.float32))
    service._submit(np.array([3], dtype=np.float32))
    worker = threading.Thread(target=service._stt_loop, args=(epoch,))
    worker.start()
    try:
        assert entered.wait(2)
        service.hold_input(True)
        service._submit(np.array([4], dtype=np.float32))  # held input cannot enter the queue
        service.hold_input(False)
        assert service.capture_epoch == epoch
        service._submit(np.array([2], dtype=np.float32))
        release.set()
        assert done.wait(2)
    finally:
        release.set()
        service.running = False
        worker.join(2)
    assert not worker.is_alive() and calls == [1, 2]
    assert len(seen) == 1 and seen[0]["text"] == "What happened today?"
    assert seen[0]["input_generation"] == service.input_generation == 2
    assert service.utterances == 1 and service.last_text == "What happened today?"


def test_hold_transitions_clear_raw_audio_stt_and_deferred_turns():
    seen = []
    service = voice(lambda kind, payload: seen.append((kind, payload)))
    service.mode = "always"
    service.capture = SimpleNamespace(queue=queue.Queue())
    epoch = service.capture_epoch
    service._deliver({"text": "watch my", "confidence": 1, "during_playback": False})
    assert service._held is not None
    for held in (True, False):
        service.capture.queue.put(np.ones(10))
        service._stt_q.put((np.ones(10), False, epoch, service.input_generation))
        service.hold_input(held)
        assert service.capture.queue.empty() and service._stt_q.empty() and service._held is None
    service._flush_held(force=True)
    service._deliver({"text": "arm", "confidence": 1, "during_playback": False, "input_generation": 0})
    assert not seen
    service._deliver({"text": "Hello there", "confidence": 1, "during_playback": False, "input_generation": 2})
    assert seen[-1][1]["text"] == "Hello there" and service.capture_epoch == epoch


def test_capture_worker_survives_hold_and_discards_prehold_dequeued_block():
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()

    class PausedQueue(queue.Queue):
        first = True

        def get(self, *args, **kwargs):
            block = super().get(*args, **kwargs)
            if self.first:
                self.first = False
                entered.set()
                assert release.wait(2)
            return block

    service = voice(lambda *args: None)
    service.mode = "always"
    service.capture = SimpleNamespace(queue=PausedQueue())
    service.vad = SimpleNamespace(reset=lambda: None, process=lambda block: [("end", block)], in_speech=False)
    original = service._submit

    def submit(*args, **kwargs):
        original(*args, **kwargs)
        submitted.set()
        service.running = False

    service._submit = submit
    epoch = service.capture_epoch
    service.capture.queue.put(np.ones(100, dtype=np.float32))
    worker = threading.Thread(target=service._run, args=(epoch,))
    worker.start()
    try:
        assert entered.wait(2)
        service.hold_input(True)
        service.hold_input(False)
        service.capture.queue.put(np.full(100, 2, dtype=np.float32))
        release.set()
        assert submitted.wait(2)
    finally:
        release.set()
        service.running = False
        worker.join(2)
    assert not worker.is_alive() and service.capture_epoch == epoch
    audio, _, capture_epoch, generation = service._stt_q.get_nowait()
    assert np.all(audio == 2) and capture_epoch == epoch and generation == 2
    assert service._stt_q.empty()
