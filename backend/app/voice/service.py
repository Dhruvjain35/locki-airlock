"""Voice input service: microphone -> VAD / push-to-talk -> Whisper -> runtime.

Modes:
  push_to_talk  audio between ptt_start() and ptt_stop() (+ a short tail) is transcribed;
                the most reliable mode in a loud hackathon hall
  wake_word     VAD segments everything; the runtime acts only on utterances that start
                with "LOCKI" (or arrive inside a short follow-up window)
  always        VAD segments everything and every utterance is interpreted

Echo handling: while the robot is speaking (and ~400 ms after), the hands-free modes keep
listening but mark what they hear as ``during_playback``. The runtime then drops anything
that matches LOCKI's own line (its echo through the speaker); a real question said over it is
taken and LOCKI stops talking (barge-in). A strong voice onset well above the speaker's echo
stops a non-urgent line at once (``barge_in`` event), before the words are transcribed; P0
security warnings are never cut off. Push-to-talk always listens.

Turn endings: the VAD closes a short turn after 650 ms of silence and a long one after 950 ms.
A turn whose words sound unfinished ("...and", "um") is held for CONTINUATION_GRACE_S; if the
user keeps talking, the next part is joined to it instead of answering half a thought.

``conversation_state`` reports what the audio and the models are really doing: IDLE (no
microphone), LISTENING, USER_SPEAKING, ENDPOINTING, THINKING, SPEAKING or INTERRUPTED.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable

import numpy as np

from .input import BLOCK_S, AudioCapture, LevelMeter, make_vad, rms_db
from .transcription import Transcriber, is_hallucination, make_transcriber

log = logging.getLogger(__name__)

PTT_TAIL_S = 0.25
MIC_STALL_S = 3.0  # no blocks for this long = the input device is gone
PTT_MAX_S = 10.0
SILENT_INPUT_S = 2.5
CONTINUATION_GRACE_S = 1.2  # an unfinished-sounding turn waits this long after its end for the rest
MAX_JOINED_PARTS = 3
_UNFINISHED = re.compile(r"(?:,|\b(?:and|but|so|or|because|cause|um+|uh+|erm|like|the|a|an|to|with|of|if|then|my|your|is|are|was|were|that|which|when|where))\W*$", re.I)
# Barge-in onset: while LOCKI is talking, a voice this much louder than the speaker's echo, with this speech probability, for this long.
BARGE_IN_OVER_ECHO_DB = 9.0
BARGE_IN_PROB = 0.85
BARGE_IN_BLOCKS = 8  # 240 ms
ECHO_LEARN_S = 0.9  # the start of every line teaches the echo level: covers a Bluetooth speaker's ~0.5 s delay plus 0.4 s of its echo


def sounds_unfinished(text: str) -> bool:
    return bool(_UNFINISHED.search((text or "").strip()))


class VoiceService:
    def __init__(self, settings, emit: Callable[[str, dict], None], speaker=None, transcriber: Transcriber | None = None,
                 on_transcribing: Callable[[bool], None] | None = None):
        self.settings = settings
        self.emit = emit
        self.speaker = speaker
        self.transcriber = transcriber
        self.capture: AudioCapture | None = None
        self.vad = make_vad(getattr(settings, "vad_engine", "silero"), getattr(settings, "models_dir", None))
        self.meter = LevelMeter()
        self.mode = "push_to_talk"
        self.device = None
        self.running = False
        self._thread: threading.Thread | None = None
        self._stt_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=4)
        self._stt_thread: threading.Thread | None = None
        self._ptt_active = False
        self._ptt_buf: list[np.ndarray] = []
        self._ptt_stop_at: float | None = None
        self._zero_since: float | None = None
        self.input_warning: str | None = None
        self.utterances = 0
        self.dropped_echo_blocks = 0  # blocks heard while the robot spoke (checked for echo, not dropped)
        self._last_block_at: float | None = None
        self.stalled = False
        self._seg_overlap = False
        self.last_text: str | None = None
        self.transcribing = False  # Whisper is working on an utterance (dashboard: "thinking")
        self.on_transcribing = on_transcribing  # tells the language model worker to leave the GPU to Whisper
        self.thinking: Callable[[], bool] | None = None  # set by the runtime: a reply is being composed
        # turn endings
        self._held: dict | None = None  # an unfinished-sounding turn waiting for the rest
        self._last_seg_end: float | None = None
        self.joined_turns = 0
        self.held_turns = 0
        # barge-in
        self._echo_db: float | None = None
        self._echo_learn_until = 0.0
        self._was_playing = False
        self._onset_blocks = 0
        self._barged_this_line = False
        self.barge_ins = 0
        self.last_barge_in_at: float | None = None
        self.input_held = False  # a recorded command is being handled: the live microphone is ignored meanwhile

    # ------------------------------------------------------------------ control
    def start(self, device=None, mode: str = "push_to_talk") -> None:
        if self.running:
            self.stop()
        self.device = device
        self.mode = mode
        if self.transcriber is None:
            self.transcriber = make_transcriber(self.settings)
        self.capture = AudioCapture(device)
        self.capture.start()  # raises if the device cannot be opened
        self.running = True
        self.vad.reset()
        self._thread = threading.Thread(target=self._run, name="voice-input", daemon=True)
        self._thread.start()
        self._stt_thread = threading.Thread(target=self._stt_loop, name="voice-stt", daemon=True)
        self._stt_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        for t in (self._thread, self._stt_thread):
            if t is not None:
                t.join(timeout=2)
        self._thread = self._stt_thread = None
        self._ptt_active = False

    def shutdown(self) -> None:
        self.stop()
        if self.transcriber is not None:
            self.transcriber.close()

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.vad.reset()
            self._ptt_active = False

    def ptt_start(self) -> None:
        if self.mode != "push_to_talk":
            return
        self._ptt_buf = []
        self._ptt_stop_at = None
        self._ptt_active = True
        self.emit("speech_started", {"mode": "push_to_talk"})

    def ptt_stop(self) -> None:
        if self._ptt_active and self._ptt_stop_at is None:
            self._ptt_stop_at = time.monotonic() + PTT_TAIL_S

    def level(self) -> dict:
        return {"rms_db": round(self.meter.get(), 1), "speaking": self.vad.in_speech or self._ptt_active}

    def hold_input(self, held: bool) -> None:
        """Ignore (or listen again to) the live microphone, so a recorded command and the room never mix into one turn."""
        if held == self.input_held:
            return
        self.input_held = held
        self.vad.reset()
        self._held = None
        self._ptt_active = False
        self._ptt_buf = []

    def conversation_state(self) -> str:
        """What the audio and the models are really doing right now."""
        if not self.running:
            return "IDLE"
        now = time.monotonic()
        if self.last_barge_in_at is not None and now - self.last_barge_in_at < 1.0:
            return "INTERRUPTED"
        if self.vad.in_speech or self._ptt_active:
            return "USER_SPEAKING"
        if self._held is not None or not self._stt_q.empty() or self.transcribing:
            return "ENDPOINTING"
        if self.thinking is not None and self.thinking():
            return "THINKING"
        if self.speaker is not None and getattr(self.speaker, "speaking", False):
            return "SPEAKING"
        return "LISTENING"

    def snapshot(self) -> dict:
        return {
            "conversation_state": self.conversation_state(),
            "input_held_for_recorded": self.input_held,
            "turns": {"held_unfinished": self.held_turns, "joined": self.joined_turns, "barge_ins": self.barge_ins},
            "running": self.running,
            "mode": self.mode,
            "device": self.device,
            "ptt_active": self._ptt_active,
            "listening": self.vad.in_speech,
            "transcribing": self.transcribing,
            "stt": self.transcriber.describe() if self.transcriber else {"engine": "none", "state": "unavailable"},
            "utterances": self.utterances,
            "dropped_echo_blocks": self.dropped_echo_blocks,
            "noise_floor_db": round(self.vad.floor_db, 1),
            "vad": type(self.vad).__name__,
            "speech_prob": round(getattr(self.vad, "last_prob", 0.0), 2) if hasattr(self.vad, "last_prob") else None,
            "warning": self.input_warning,
            "stalled": self.stalled,
            "reason": None if self.running else "voice input not started",
        }

    # ------------------------------------------------------------------ threads
    def _run(self) -> None:
        cap = self.capture
        while self.running and cap is not None:
            try:
                block = cap.queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_finish_ptt()
                self._check_stalled()
                continue
            self._last_block_at = time.monotonic()
            if self.stalled:
                self.stalled = False
                self.input_warning = None
                self.emit("recovered", {"message": "microphone is delivering audio again"})
            db = rms_db(block)
            self.meter.set(db)
            self._check_silent_input(block)
            if self.input_held:
                continue
            if self.mode == "push_to_talk":
                if self._ptt_active:
                    self._ptt_buf.append(block)
                    if len(self._ptt_buf) * BLOCK_S > PTT_MAX_S:
                        self._ptt_stop_at = time.monotonic()
                self._maybe_finish_ptt()
                continue
            during = self.speaker is not None and self.speaker.recently_spoke()
            if during:
                self.dropped_echo_blocks += 1
            for kind, audio in self.vad.process(block):
                if kind == "start":
                    self._seg_overlap = during
                    self.emit("speech_started", {"mode": self.mode})
                elif kind == "end":
                    self.emit("speech_ended", {"mode": self.mode, "kept": audio is not None})
                    if audio is not None:
                        self._last_seg_end = time.monotonic()
                        self._submit(audio, during_playback=self._seg_overlap or during)
                    self._seg_overlap = False
            if during and self.vad.in_speech:
                self._seg_overlap = True
            self._watch_barge_in(db)

    def _watch_barge_in(self, db: float) -> None:
        """While LOCKI's own line plays, learn how loud its echo is at the mic, and call a strong, sustained voice above it a barge-in.
        The runtime stops the line at once (never a P0 warning); what the person says is then transcribed and answered as usual."""
        speaking = self.speaker is not None and bool(getattr(self.speaker, "speaking", False))
        now = time.monotonic()
        if not speaking:
            self._was_playing, self._onset_blocks, self._barged_this_line = False, 0, False
            return
        if not self._was_playing:  # a new line started: learn its echo over the first ECHO_LEARN_S
            self._was_playing, self._echo_learn_until, self._echo_db = True, now + ECHO_LEARN_S, None
        if now < self._echo_learn_until:
            self._echo_db = db if self._echo_db is None else max(self._echo_db, db)
            return
        if self._echo_db is None or self._barged_this_line:
            return
        prob = float(getattr(self.vad, "last_prob", 1.0 if self.vad.in_speech else 0.0))
        if prob >= BARGE_IN_PROB and db >= self._echo_db + BARGE_IN_OVER_ECHO_DB:
            self._onset_blocks += 1
        else:
            self._echo_db = max(self._echo_db * 0.98 + db * 0.02, min(self._echo_db, db)) if prob < 0.5 else self._echo_db
            self._onset_blocks = 0
        if self._onset_blocks >= BARGE_IN_BLOCKS:
            self._barged_this_line, self._onset_blocks = True, 0
            self.barge_ins += 1
            self.last_barge_in_at = now
            self.emit("barge_in", {"over_echo_db": round(db - self._echo_db, 1), "speech_prob": round(prob, 2), "onset_ms": round(BARGE_IN_BLOCKS * BLOCK_S * 1000)})

    def _maybe_finish_ptt(self) -> None:
        if self._ptt_active and self._ptt_stop_at is not None and time.monotonic() >= self._ptt_stop_at:
            self._ptt_active = False
            audio = np.concatenate(self._ptt_buf) if self._ptt_buf else np.zeros(0, dtype=np.float32)
            self._ptt_buf = []
            self.emit("speech_ended", {"mode": "push_to_talk", "seconds": round(len(audio) / 16000, 2)})
            if len(audio) / 16000 >= 0.3:
                self._submit(audio)
            else:
                self.emit("rejected", {"reason": "push-to-talk clip too short", "seconds": round(len(audio) / 16000, 2)})

    def _check_stalled(self) -> None:
        """No audio blocks at all for a while: the input device went away (Bluetooth mic off,
        USB unplugged). Say so once instead of silently hearing nothing."""
        if self._last_block_at is None or self.stalled:
            return
        if time.monotonic() - self._last_block_at > MIC_STALL_S:
            self.stalled = True
            self.input_warning = "Microphone stopped delivering audio (unplugged or switched off?). Pick the input again in Settings > Voice."
            self.emit("error", {"error": self.input_warning})

    def _check_silent_input(self, block: np.ndarray) -> None:
        if np.any(block):
            self._zero_since = None
            self.input_warning = None
            return
        now = time.monotonic()
        if self._zero_since is None:
            self._zero_since = now
        elif now - self._zero_since > SILENT_INPUT_S and self.input_warning is None:
            self.input_warning = (
                "Microphone delivers exact silence: on macOS grant Microphone access to the app running the backend."
            )
            self.emit("error", {"error": self.input_warning})

    def _submit(self, audio: np.ndarray, during_playback: bool = False) -> None:
        try:
            self._stt_q.put_nowait((audio, during_playback))
        except queue.Full:
            self.emit("rejected", {"reason": "speech-to-text busy; utterance dropped"})

    def _flush_held(self, force: bool = False) -> None:
        held = self._held
        if held is None:
            return
        waiting = self.vad.in_speech or not self._stt_q.empty()
        if not force and (waiting or time.monotonic() < held["deadline"]):
            return
        self._held = None
        self.emit("utterance", held["payload"])

    def _deliver(self, payload: dict) -> None:
        """A finished transcription: joined to a held unfinished turn, held itself if it sounds unfinished, or handed over."""
        held = self._held
        if held is not None:
            self._held = None
            self.joined_turns += 1
            prev = held["payload"]
            payload = {**payload, "text": f"{prev['text']} {payload['text']}".strip(), "confidence": min(prev["confidence"], payload["confidence"]),
                       "during_playback": prev["during_playback"] or payload["during_playback"], "audio_s": (prev.get("audio_s") or 0) + (payload.get("audio_s") or 0),
                       "parts": held["parts"] + 1}
            parts = held["parts"] + 1
        else:
            parts = 1
        if self.mode != "push_to_talk" and parts < MAX_JOINED_PARTS and sounds_unfinished(payload["text"]):
            end = self._last_seg_end or time.monotonic()
            self._held = {"payload": payload, "parts": parts, "deadline": end + CONTINUATION_GRACE_S}
            self.held_turns += 1
            return
        self.emit("utterance", payload)

    def _stt_loop(self) -> None:
        while self.running:
            try:
                audio, during_playback = self._stt_q.get(timeout=0.1)
            except queue.Empty:
                self._flush_held()
                continue
            tr = self.transcriber
            if tr is None or tr.state != "ready":
                self.emit("rejected", {"reason": f"speech-to-text {getattr(tr, 'state', 'unavailable')}: {getattr(tr, 'error', '') or 'still loading'}"})
                continue
            self.transcribing = True
            if self.on_transcribing is not None:
                self.on_transcribing(True)
            try:
                result = tr.transcribe(audio)
            finally:
                self.transcribing = False
                if self.on_transcribing is not None:
                    self.on_transcribing(False)
            if result is None:
                self.emit("rejected", {"reason": "transcription failed or timed out"})
                continue
            text = (result.get("text") or "").strip()
            if is_hallucination(text):
                self.emit("rejected", {"reason": "no speech recognised", "text": text})
                continue
            self.utterances += 1
            self.last_text = text
            self._deliver(
                {
                    "text": text,
                    "confidence": float(result.get("confidence", 0.0)),
                    "latency_ms": result.get("roundtrip_ms", result.get("latency_ms")),
                    "audio_s": result.get("audio_s"),
                    "during_playback": during_playback,
                }
            )
        self._flush_held(force=True)
