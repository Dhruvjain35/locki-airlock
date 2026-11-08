# NVIDIA models evaluated for LOCKED IN (Milestones 5.5 and 5.6)

NVIDIA speech, language and voice models were measured against the Milestone 5 stack
(whisper.cpp, Qwen3-4B on MLX, Kokoro int8) on the demo Mac, on identical inputs, and adopted
only where the numbers showed a real improvement. Every result file is in
`docs/benchmarks/m55-*.json` with the machine state at the time.

| Component | Candidate | Decision | Deciding evidence |
|---|---|---|---|
| Speech recognition | **Parakeet TDT 0.6B v3** (NeMo-Speech.cpp) | **Adopted, paired with whisper.cpp** | Fresh test audio: 7.6% vs 13.9% word errors on clean speech, 13.7% vs 15.8% in noise, more commands right, no words invented from noise, every stop kept, +21 ms |
| Speech recognition | Nemotron 3.5 ASR Streaming 0.6B (NeMo-Speech.cpp) | Not adopted | Worse on short commands (8.7% vs 0.3% word errors) and noise (32% vs 16%); first partial transcript p50 0.76 s |
| Speech recognition runtime | parakeet-mlx (senstella) | Not run | Same Parakeet model as above; 2.5 GB fp32 download vs 714 MB q8_0, with 4-5 GiB of disk left |
| Conversation | NVIDIA-Nemotron-3-Nano-4B (GGUF, llama.cpp) | Not adopted | 83.6% vs 89.0% right actions on 68 scenarios; first spoken sentence p50 2.96 s vs 1.12 s |
| Voice | MagpieTTS Multilingual 357M (NeMo-Speech.cpp) | Not adopted | 0.9x real time idle and 2.8x with the model writing, vs Kokoro 0.1x and 0.7x |

The language model stays Qwen3-4B-Instruct-2507 and the voice stays Kokoro int8 (`af_bella`)
plus the cached Fish Audio clips. Investigating the conversation misses led to a clearer
action guide in the prompt (not an NVIDIA change): 62.5% to 87.5% right actions on a held-out
set written before the change.

Milestone 5.6 (section 14) hardened the combined recogniser so a stop is never weakened or
delayed by the second engine. It also tested a faster reply format, which was not adopted
because it cost accuracy. A script for testing with a real person is ready, but that test has
not been run.

## Before and after

All "before" and "after" numbers were measured on the same Mac on the same day.

| Measure | Before (Milestone 5) | After (Milestone 5.5) | Source |
|---|---|---|---|
| Speech: word errors, clean (fresh validation set, 60 clips) | 13.9% | **7.6%** | `m55-asr-validate-20261101-173400.json` |
| Speech: word errors, noise (validation, 200 clips, 15 and 5 dB) | 15.8% | **13.7%** | same |
| Speech: commands understood, clean / noise | 33/39, 104/130 | **34/39, 113/130** | same (corrected in 5.6) |
| Speech: stops heard in noise | 20/20 | 20/20 | same |
| Speech: text produced from non-speech noise | 30/30 clips | **0/30** | same |
| Speech: real speech returned empty, noise | 0/200 | 2/200 | same |
| Speech: word errors, real human read speech (LibriSpeech, 73 clips) | 11.8% | **4.3%** | `m55-asr-20261101-170419.json` |
| Speech recognition time p50 (app path, idle / vision + model writing) | 92 / 216-305 ms | 113 / 243-388 ms | `m55-asr-validate*.json` |
| Conversation: right action, held-out set 2 (16 scenarios x2) | 62.5% | **87.5%** | `m55-eval-h2-*.json`, `m55-eval-everything-*.json` |
| Conversation: right action, the other 68 scenarios | 89.0% | **97.3%** | `m55-eval-all-Qwen3*.json`, `m55-eval-everything-*.json` |
| Conversation: speak / stay silent decisions | 90% | **100%** | same |
| Conversation: motion requests | 0 | 0 | same |
| End of speech to voice, model turns, vision running (p50) | 1.93 s (n=30) | 1.85 s (n=15) | `m55-latency-*.json` |
| End of speech to voice, short commands (p50) | 0.19 s | 0.19 s | same |
| Speech memory (physical footprint) | 395 MB | 395 + 166-258 MB | `m55-asr*.json` |
| Model files on disk | 3.0 GB | 3.8 GB | `backend/models.sha256` |

The 68-scenario figure includes the Milestone 5 held-out set and the 33 unseen scenarios used
to find the failure types. The held-out set 2 row is the clean measure of the prompt change.
End-to-end latency did not change measurably: the 95% interval of the difference includes
zero.

## Machine and conditions

- **Machine:** MacBook Air (Mac16,12), Apple M4 (4P + 6E cores, 8-core GPU), 16 GB, macOS 26.4.
  Low Power Mode off, on AC power.
- **Background load:** the owner's usual apps kept running. Swap in use grew from 6.8 to
  9.8 GB during the work, and the GPU was 33-78% busy from other apps before some runs. Free
  disk went from 11 GiB to 4.4 GiB.
- **Isolation:**
  - Every NVIDIA runtime ran inside the macOS sandbox with outbound connections blocked
    except localhost, from local model files.
  - No audio was played, no microphone or camera was opened, and the robot was not
    connected.
  - Perception events were injected, and spoken input was macOS voices or recorded files.
- **What is missing:** none of this is a human evaluation, and nothing ran on the robot.

## 1. Speech recognition

### Candidates

| | whisper.cpp base.en (Milestone 5) | Parakeet TDT 0.6B v3 | Nemotron 3.5 ASR Streaming 0.6B |
|---|---|---|---|
| Version | `ggml-base.en.bin` @ `5359861c` | `nvidia/parakeet-tdt-0.6b-v3` @ `541d1f99`, q8_0 GGUF | `nvidia/nemotron-3.5-asr-streaming-0.6b` @ `1c8deaec` (NeMo-Speech index pin), q8_0 GGUF |
| Licence | MIT | CC-BY-4.0 | NVIDIA Open Model License (OpenMDW 1.1) |
| Runtime | pywhispercpp 1.5.1 in a worker process | NeMo-Speech.cpp 0.3.0 `serve` (Apache-2.0, ggml) | same |
| Hardware | Metal (M4 GPU) | Metal | Metal |
| Disk | 148 MB | 714 MB + 19 MB runtime | 742 MB |
| Memory (physical footprint) | 395 MB | 209 MB (166-258 MB in other runs) | 513 MB |
| Start (ready) / first transcript | 602 ms / 88 ms | 812 ms / 57 ms | 13.6 s / 136 ms |
| Streaming | no | no (offline model) | yes, 160 ms chunks |
| Confidence | calibrated (token probabilities) | always 1.0 | always 1.0 |

NeMo-Speech.cpp is NVIDIA's ggml runtime for its speech models. The prebuilt macOS arm64
Metal archive from release v0.3.0 was checked against the release's published SHA-256.
Models are loaded from local paths, so its model index never downloads anything. Its default
CLI would download a model on first use; LOCKED IN always passes a local file.

### Test audio

The same clips were given to every engine (`scripts/bench_nvidia.py asr`):

- **core (104 clips):** the Milestone 5 set, 31 commands in 3 macOS voices plus 11 commands
  played through the Mac's speakers into its microphone. Those 11 are a synthetic voice
  through a real room, not a person.
- **human (73 clips):** LibriSpeech read speech (`hf-internal-testing/librispeech_asr_dummy`
  @ `5be91486`, CC-BY-4.0, one speaker, 8 minutes). This is the only real human speech in the
  test sets.
- **conversational (60 clips):** 30 informal lines with fillers, self-corrections and
  incomplete sentences, in 2 voices not used elsewhere.
- **noise (128 clips):** 16 commands mixed with white noise, background talk, keyboard and
  hum at 10 dB and 0 dB.
- **false activations (48 clips):** noise only, with nothing said.
- **Validation set (296 clips):** written after the combination rule (below) and never used
  to change it. It has new phrases, 3 new voices, new background talkers, new noise seeds and
  levels (15 and 5 dB), and includes two motion commands.

### Results on the design sets

`m55-asr-20261101-170419.json`; word error rate, commands understood, median time.

| Set | whisper.cpp | Parakeet v3 | Nemotron 3.5 |
|---|---|---|---|
| core (104; 59 commands) | 0.3%, 58/59, 88 ms | 0.8%, **59/59**, 46 ms | 8.7%, 50/59, 59 ms |
| conversational (60; 32 commands) | 6.4%, 30/32, 96 ms | **4.0%**, 30/32, 70 ms | 16.1%, 26/32, 87 ms |
| human (73) | 11.8% | **4.3%** | 6.7% |
| noise (128; 88 commands) | **16.1%**, **77/88** | 22.8%, 66/88 | 32.4%, 55/88 |
| noise only, transcribed (48) | 48/48 | 5/48 (all background talk) | 0/48 |

What whisper.cpp produced from noise:
- From silence: "Get out of here." and "I'm going to go."
- From hum and keyboard: "[SOUND]" and "(tapping)".
- From tones: "[Music]".

Its confidence on these was 0.52-0.98, so many would pass the app's 0.45 command gate. Its
known-hallucination filter does not catch them.

Nemotron 3.5 garbled short clips in whole-file mode ("Roast meast meast"). Its streaming
mode is cleaner but slow to show anything: first partial transcript p50 756 ms (p90
1230 ms), final 102 ms after the audio ends, 16.5% word errors. Not adopted.

### Combining Parakeet and whisper.cpp

Parakeet is more accurate on real and informal speech and never invents words, while
whisper.cpp holds up better in steady noise and is the only one of the two with a usable
confidence. The app's command gates need that confidence: 0.45 for any command and 0.8 for
motion. So both engines hear the same audio in parallel and `backend/app/voice/asr_combine.py`
decides:

1. **Parakeet heard nothing:** nothing was said, unless whisper heard a stop. A stop is
   always kept, because stopping is the safe direction.
2. **Both readings mean the same thing:** use Parakeet's words with whisper's confidence.
3. **They disagree:**
   - A stop wins.
   - Otherwise a reading that is a command wins over one that is not.
   - Two different commands go to whisper when it is confident (0.8 or above), else to
     Parakeet.
4. **Motion:** a motion command is never taken from a disagreement. It needs both readings
   and then still passes the runtime's own 0.8 motion gate on whisper's confidence.

A reading taken from Parakeet alone gets confidence 0.5. That is enough for ordinary commands
and never enough to move the robot.

### Validation (fresh audio, the app's own code path)

> **Correction (Milestone 5.6).** The first version of this report counted "commands understood" over every
> phrase, because the scorer compared intent names in the wrong case (`"UNKNOWN"` vs `"unknown"`). The numbers now
> count real commands only (`bench_nvidia.py` fixed). The ranking of the engines did not change.


`m55-asr-validate-20261101-173400.json`, through `CombinedTranscriber`:

| | whisper.cpp | Parakeet | **Combined** |
|---|---|---|---|
| clean (60; 39 commands): word errors, commands | 13.9%, 33/39 | 8.7%, 32/39 | **7.6%, 34/39** |
| noise (200; 130 commands): word errors, commands | 15.8%, 104/130 | 19.5%, 98/130 | **13.7%, 113/130** |
| stops heard in noise | 20/20 | 13/20 | **20/20** |
| text from non-speech (30) | 30/30 | 0/30 | **0/30** |
| background talk transcribed (6) | 6/6 | 1/6 | 1/6 |
| real speech returned empty (noise) | 0/200 | 3/200 | 2/200 |
| motion commands not said but produced | 0 | 0 | 0 |
| time p50 / p90 | 92 / 111 ms | n/a | 113 / 150 ms |

Under load (`m55-asr-validate-load-20261101-173926.json`) the camera pipeline ran at
13.7 fps and Qwen3-4B wrote replies non-stop at 19.7 tokens/s p50. Accuracy was identical. The
combined path's median time was 243 ms on clean audio and 388 ms in noise, against 216 ms and
305 ms for whisper.cpp alone.

**Decision: adopted.** `stt_engine` defaults to `parakeet+whisper`. When the NVIDIA files are
missing the app uses whisper.cpp alone, and faster-whisper if whisper.cpp is unavailable.

## 2. Conversational model

### Candidate

| | Qwen3-4B-Instruct-2507 (Milestone 5) | NVIDIA-Nemotron-3-Nano-4B |
|---|---|---|
| Version | `mlx-community/Qwen3-4B-Instruct-2507-4bit` @ `50d42775` | `nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF` @ `ba223d14`, Q4_K_M, sha256 `be5d9a65…` |
| Licence | Apache-2.0 | NVIDIA Nemotron Open Model License |
| Architecture | transformer, 4B | Mamba2-Transformer hybrid, 3.97B, compressed from Nemotron-Nano-9B-v2 |
| Runtime | MLX worker process (mlx-lm 0.32.2) | llama.cpp b12032 `llama-server` (Metal), `app/intelligence/llamacpp_llm.py` |
| Reasoning | none | turned off through its chat template (`<think></think>`) |
| Disk | 2.28 GB | 2.84 GB (+ 40 MB llama.cpp) |
| Memory | 2.44 GB active, 2.95 GB peak (MLX) | 2.93 GB resident, 1.19 GB footprint |
| Load (cold) | 5.1 s (offline check) | 14.8 s first start, 2.9 s later starts |
| Generation | 37 tokens/s | 22-23 tokens/s |

Nemotron got the same two messages (persona, context), the same prefill while the user
talks, and the same evaluation harness. The llama.cpp wrapper reuses the cached prompt: after
a prefill only the new line, about 23 tokens, was read.

### Results

68 scenarios (35 from Milestone 5, 33 new and unseen), 2 runs each, the same code and prompt
(`conversation_eval.py --set all`):

| | Qwen3-4B | Nemotron-3-Nano-4B |
|---|---|---|
| right action (146 spoken steps) | **89.0%** | 83.6% |
| … Milestone 5 held-out | **75.0%** | 62.5% |
| … 33 unseen | **85.0%** | 82.5% |
| reply in context | 93.2% | 93.8% |
| speak / stay silent (20 events) | 90% | 90% |
| motion requests | 0 | 0 |
| first sentence to the voice p50 / p90 | **1.12 / 1.64 s** | 2.96 / 5.96 s |
| average reply length | 15.4 words | 11.4 words |
| replies refused by the guards | 20 | 9 |

**Decision: not adopted.** Nemotron got more actions wrong and was 2.6x slower to its first
spoken sentence on this Mac. Its replies were shorter and never opened with the phone, which
Qwen often does (below). It was not re-run with the new prompt: the latency alone rules it out.

### The misses, and a prompt change

Both models failed the same indirect requests:
- ending the session: "let's call it here", "that's a wrap for tonight";
- the phone for a stated reason: "I have to show my professor something on my phone";
- "stop" meaning stop talking: "you're so annoying, stop commenting on everything".

Qwen answered several of these with `set_style`, and its reply teased about the phone instead
of acting. The action guide in `backend/app/intelligence/prompting.py` described these actions
too narrowly.

1. A new held-out set (16 scenarios: 10 requests in new words, 6 controls that must not act)
   was written before anything was changed. It was never used to tune the wording.
2. The guide was reworded in general terms:
   - leaving the desk means pause;
   - being finished for the day or night means end;
   - telling it to stop talking or commenting means quiet;
   - needing the phone for something means release;
   - set_style is only for changing how it talks;
   - a complaint about the work is not a request.
3. Results (`m55-eval-everything-20261101-175751.json`):
   - held-out set 2: 62.5% to **87.5%**, and every control still did nothing;
   - the other 68 scenarios: 89.0% to 97.3%;
   - across all 84 scenarios, 12 steps improved (8 outside held-out set 2) and none got worse;
   - speak/silence decisions: 90% to 100%;
   - motion requests: 0.

Still missed:
- "brb, grabbing my charger" (pause), 0 of 2 runs;
- "shh, I'm in the middle of a hard proof" (quiet), 0 of 2 runs;
- "gonna run to the bathroom" and "my mom's calling", 1 of 2 runs each.

**A style request that changes nothing.** Qwen asks for `set_style` in 20-25% of its turns, almost always naming the
style already in use: 20 of 24 such requests with the old prompt, 32 of 36 with the new one. The effect-based scoring
rightly does not count these as wrong, because nothing changes. The action log used to show them as "executed";
they are now recorded as "ignored: style already roast" (`companion.py`, tested).

**Naturalness, measured but not fixed.** Qwen opens 16% of replies with "Phone's…" (22%
before the change), and "throne" appeared 10 times in 146 replies. Within a session the
RECENT LINES rule stops exact repeats but not this habit. Fixing it needs another prompt round
with a fresh held-out set.

## 3. Voice

MagpieTTS Multilingual 357M ran through NeMo-Speech.cpp 0.3.0 on Metal:
- **Version:** `nvidia/magpie_tts_multilingual_357m` @ `19806879` (index pin), f16 GGUF.
- **Codec:** NanoCodec 22 kHz decoder f16 @ `fc00890b`.
- **Licence:** NVIDIA Open Model License.
- **Disk and memory:** 637 MB on disk, 851 MB footprint.
- **Voices:** Aria, Jason, John, Leo, Sofia.

Kokoro int8 `af_bella` (current, CPU) was measured on the same 10 lines. Intelligibility is
the word error rate when whisper.cpp transcribes each rendered line
(`m55-tts-20261101-174259.json`, `m55-tts-load-20261101-174818.json`):

| | Kokoro int8 af_bella | Magpie Aria | Magpie Jason |
|---|---|---|---|
| time per line p50 (real-time factor) | **462 ms (0.1)** | 3598 ms (0.9) | 3349 ms (0.9) |
| same, with Qwen3-4B writing non-stop | **2215 ms (0.7)** | 12034 ms (2.8) | 10796 ms (2.8) |
| first audio, streaming | not streamed (whole sentence) | 206 ms (engine bench) | 206 ms |
| heard-back word errors | 2.2% | 1.1% | 2.9% |

**Decision: not adopted.**
- **Speed:** Magpie starts audio quickly when streaming, but it only just keeps up with
  playback (1.15x real time) when idle. With the model writing it falls far behind real time,
  so speech would stall.
- **Kokoro:** remains the local voice. The Fish Audio E-girl clips stay the character voice
  for fixed lines. No Fish Audio request was made.
- **Not yet judged:** voice quality, expressiveness and pronunciation need a person listening.
  Samples are in `data/voice_samples/` (`magpie-aria-m55-*`, `magpie-jason-m55-*`,
  `kokoro-af_bella-m55-*`). Magpie, Kokoro and Fish Audio are three different voices.

## 4. Response time

### Where the time goes

Model turns, vision running, end of speech to the voice starting, medians
(`m55-latency-silent-vision-*.json`, 6 interleaved runs):

| Stage | whisper.cpp | Parakeet + whisper.cpp |
|---|---|---|
| speech recognition | 176 ms | 201 ms |
| model first token (context already read while the user spoke) | 276 ms | 279 ms |
| model reply complete | 1.38 s | 1.37 s |
| Kokoro render of the first sentence | 521 ms | 440 ms |
| **end of speech to voice** | **1.93 s** (n=30) | **1.85 s** (n=15) |
| short command (parser, cached voice) | 191 ms | 192 ms |

The largest contributors, in order:
1. **The model writing up to its first complete sentence** (about 0.9 s after the
   transcript). The JSON it writes before the spoken words (`{"action": …, "args": …,
   "say": "`) is part of this. That is roughly 12-20 tokens, an estimate rather than a
   measurement.
2. **Kokoro rendering that sentence** (0.44-0.52 s). Kokoro runs 4.8x slower while the model
   is writing (section 3).
3. **Speech recognition** (0.18-0.20 s).

Short commands never reach the model and are answered in about 0.19 s.

### Changes tried

- **The model pauses while the voice renders.** Setting `llm_yields_to_voice` makes the
  worker wait between tokens while Kokoro renders a sentence. Results:
  - over 90 model turns: p50 1.81 s vs 1.88 s;
  - 95% bootstrap interval of the difference: -296 to +86 ms;
  - p90: worse with whisper.cpp, better with the pair.

  This is no measurable gain, so the setting stays off. It is kept as a tested option.
- **Streaming recognition (Nemotron 3.5).** The first partial arrives 0.76 s p50 after
  speech starts, too late to help.
- **Prompt reuse, prefill while the user talks, cached fixed lines and interruptible
  background work** were already in place from Milestone 5 and are unchanged.
  `stt_gpu_priority` stays off, per the Milestone 5 A/B.
- **Tried in Milestone 5.6:** have the model write `say` before `action` (section 14.2). It
  was 0.44 s faster, but it chose worse actions and claimed to move, so it was not adopted.

Stop, mute, privacy and other safety-critical commands are unchanged. They are handled by the
deterministic parser and never wait for the model.

## 5. What changed in the code

- **Speech:** `backend/app/voice/asr_combine.py` (the rule) and `backend/app/voice/nemo_asr.py`
  (the sandboxed `nemo-speech serve` on 127.0.0.1). In `backend/app/voice/transcription.py`,
  `CombinedTranscriber` and `make_transcriber` falling back to whisper.cpp.
  `backend/app/config.py`: `stt_engine` default `parakeet+whisper`.
- **Model downloads:** `backend/scripts/download_models.py` fetches NeMo-Speech.cpp v0.3.0
  (archive sha256) and Parakeet (pinned revision, `backend/models.sha256`) by default.
  `--no-nvidia` skips them.
- **Offline check:** `backend/scripts/offline_check.py` checks the Parakeet files and runs
  the pair with the network blocked.
- **Prompt:** the action guide in `backend/app/intelligence/prompting.py`.
- **Optional model backend:** `backend/app/intelligence/llamacpp_llm.py`, the llama.cpp
  backend used to evaluate Nemotron. It is not selectable in the app's settings.
- **Latency option:** `llm_yields_to_voice`, off. It touches `llm_worker.py`, `local_llm.py`,
  `VoiceRouter.on_render` in `tts.py`, and `runtime._voice_rendering`.
- **Dashboard:** `runtime.ai_diagnostics()`, `backend/app/telemetry/resources.py` (memory
  sampler), and a "pipeline" block in `frontend/src/components/CompanionPanel.tsx`.
- **Tools:** `backend/scripts/bench_nvidia.py` (asr, asr-validate, tts).
  `conversation_eval.py` gained `--set m55|h2|everything` and `--backend llamacpp`.
  `latency_check.py` gained `--stt`, `--yield-to-voice` and `--reps`. `soak_companion.py` now
  uses the configured recogniser.
- **Tests:**
  - `test_asr_combine.py` (rules, fake engines, the real Parakeet server when downloaded);
  - `test_llamacpp_llm.py` (the real model only with `LOCKEDIN_TEST_NEMOTRON=1`);
  - `test_voice_render_yield.py`, `test_env_template.py`;
  - `test_download_models.py`, which now also checks archive unpacking.
- **Unchanged:**
  - the Arduino firmware, motor safety logic and safety controller;
  - motion gates and interlocks;
  - whisper.cpp itself, Silero VAD, Qwen3-4B, Kokoro and the Fish Audio cache.

## 6. Dashboard diagnostics

The Companion panel shows a "pipeline" block. Everything in it comes from the running
backend:
- the active recogniser, with each engine's state and the rule that decided the last
  transcript;
- the language model, its runtime, state and last tokens/s;
- the local voice and the cached-clip voice;
- recorded latencies (speech to text, model reply, voice render, end of speech to voice, p50
  over the last turns);
- the camera's real capture and processing fps;
- each AI process's physical footprint, sampled every 15 s and labelled with its age;
- the offline state;
- the robot driver and link state.

## 7. Verification

| Check | Result |
|---|---|
| Backend tests | 519 passed, 3 skipped. The skips are the opt-in real-model tests: 2 Qwen, 1 Nemotron. 496 + 2 skipped before. |
| Ruff | clean |
| Dashboard type-check and production build | pass |
| `make offline-check` | READY, 0 missing, 0 warnings. Combined speech heard "Lock me in for 25 minutes." with the network blocked (transcribe 122 ms); Qwen replied offline; backend start 0 outbound attempts. |
| 20-minute soak (final configuration) | `m55-soak-20.0min-20261101-184452.json`: 29 turns, 0 errors, 0 unanswered, 0 model failures (32 calls). Parakeet and whisper.cpp both ready throughout and agreed on all 29 transcripts. Model turns to the voice p50 2.04 s, p90 2.66 s, max 3.14 s; parser turns p50 0.21 s. No memory growth: the process tree RSS fell 495 MB as macOS paged. Silent output, macOS voices, simulated perception. |
| Dashboard in Chrome | the pipeline block renders, on a separate text-only backend (port 8079, fresh data, camera and microphone off): recogniser `parakeet+whisper` (voice input not started), Qwen3-4B on MLX at 33 tok/s, "text only", one typed turn "think 2414 ms", memory 2.7 GB in 2 processes. A first version said "macOS voice" in text-only mode; fixed. |
| Fresh clone of the code | not repeated for this branch; it is not pushed |

## 8. Memory and storage

- **Adopted, on disk:** Parakeet q8_0 714 MB and NeMo-Speech.cpp runtime 19 MB.
- **Adopted, while running:** the Parakeet server takes 166-258 MB of physical footprint, on
  top of the whisper.cpp worker (334-395 MB).
- **Evaluation only:** about 4.3 GB in total. In Milestone 5.6 everything except the
  LibriSpeech clips was moved to the Trash (section 14.5).
  - Nemotron-3-Nano-4B GGUF: 2.84 GB.
  - llama.cpp b12032: 40 MB.
  - Nemotron 3.5 ASR GGUF: 742 MB.
  - Magpie + NanoCodec: 637 MB.
  - LibriSpeech test clips: 24 MB, kept.

## 9. Recommended default configuration

| Part | Default |
|---|---|
| Speech recognition | `stt_engine=parakeet+whisper` (Parakeet TDT 0.6B v3 via NeMo-Speech.cpp + whisper.cpp base.en, combined) |
| Hands-free speech detection | Silero VAD v6 |
| Language model | Qwen3-4B-Instruct-2507 4-bit on MLX, with the reworded action guide |
| Voice | Kokoro int8 `af_bella` for generated lines; cached Fish Audio E-girl clips for fixed lines; macOS voice last |
| `llm_yields_to_voice` | off |
| `stt_gpu_priority` | off |

## 10. Reproduce

```bash
cd backend
.venv/bin/python scripts/download_models.py                    # pinned models incl. NVIDIA speech (--no-nvidia to skip)
.venv/bin/python scripts/bench_nvidia.py asr                   # 413 clips, three engines
.venv/bin/python scripts/bench_nvidia.py asr-validate [--load] # fresh audio, the app's combined path
.venv/bin/python scripts/bench_nvidia.py tts [--load]          # Kokoro vs Magpie (needs: nemo-speech pull magpie)
.venv/bin/python scripts/conversation_eval.py --set everything # Qwen on all 84 scenarios
.venv/bin/python scripts/conversation_eval.py --set all --backend llamacpp --gguf models/llm-gguf/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf
.venv/bin/python scripts/latency_check.py --vision-load --reps 3 --stt parakeet+whisper [--yield-to-voice]
.venv/bin/python scripts/soak_companion.py --minutes 20
```

These need the evaluation-only files. All of them except the LibriSpeech clips are in the
Trash since Milestone 5.6: put them back, or fetch them again as follows.
- `data/asr_eval/librispeech_dummy`: the LibriSpeech clips (`hf-internal-testing/librispeech_asr_dummy`
  @ `5be91486`, decoded to 16 kHz WAV).
- `backend/models/llm-gguf/`: Nemotron, fetched with `hf_hub_download` at the revision above.
- `backend/models/llama.cpp/b12032/`: from the `ggml-org/llama.cpp` release `b12032`,
  sha256 `00468fe8…`.
- Magpie: `NEMO_SPEECH_MODEL_DIR=backend/models/nemo-speech/models nemo-speech pull magpie`.

## 11. Start the application

```bash
make dev        # simulation: backend :8077 + dashboard http://127.0.0.1:5174
make demo       # one process on http://127.0.0.1:8077
make offline-check && make hardware   # real Zeus Car, offline, no motor commands
```

## 12. Still to test with people and hardware

- **A person talking to it:** speech recognition on real voices, in a quiet room and in a
  noisy one like the venue. All speech figures above come from synthetic voices, read
  speech and speaker-to-microphone playback.
- **Hearing the voices:** Kokoro, Magpie samples and the Fish clips on the Bluetooth speaker.
- **On the robot:**
  - the whole pipeline on the `Zeus_Car` network with the robot camera;
  - the 20-minute live soak (Milestone 5 checklist H8);
  - the robot never moves (H9).
- **Remaining Milestone 5 items:** checklist H1-H10 in MILESTONE5_VERIFICATION.md.

## 13. Rollback

- **Speech:** `LOCKEDIN_STT_ENGINE=whisper.cpp` in `.env`, or delete
  `backend/models/nemo-speech/`. Either way speech recognition is whisper.cpp alone, as in
  Milestone 5.
- **Prompt:** `git revert` the conversation commit.
- **Everything:** `git switch milestone-2`. That branch is untouched and backed up on GitHub.
- **Disk:** the evaluation-only folders (`backend/models/llm-gguf/`,
  `backend/models/llama.cpp/`, and the `nemotron-3.5-asr-streaming-0.6b`,
  `magpie_tts_multilingual_357m` and `nemo-nano-codec-22khz-1.89kbps-21.5fps` folders under
  `backend/models/nemo-speech/models/nvidia/`) were moved to the Trash in Milestone 5.6, as
  `locked-in-*` folders. Emptying the Trash reclaims the space. `data/asr_eval/` (24 MB) is
  still in place.

## 14. Milestone 5.6: speech safety, reply format, a test script for a person

Milestone 5.6 audited the combined recogniser for safety and measured a faster reply format.
It also prepared a test with a real person, which has not been run (section 14.4). Every result
in this document uses synthetic voices, recordings and simulated perception.

### 14.1 Speech safety audit

The combined recogniser (section 1) was checked for any way the second engine could weaken or
delay a stop. Each defect was first shown by a failing test
(`backend/tests/test_asr_combine_safety.py`), then fixed:

| Defect | Before the fix | After |
|---|---|---|
| Agreeing readings kept whisper.cpp's confidence | A stop both engines heard at whisper confidence 0.3 failed the runtime's 0.45 gate. Parakeet alone (0.5) would have passed. | Agreement takes max(whisper, 0.5). Motion still needs whisper's own 0.8. |
| Whisper's stock outputs for noise ("Thank you.", "[SOUND]") counted as readings | They parsed as intents and could outvote Parakeet's real command. | They count as no reading. |
| The pair waited for both engines | A stalled Parakeet held every result, a stop included, for up to 10 s. A shared two-thread pool could queue whisper.cpp behind it. | Each engine has its own thread. A stop from whichever engine finishes first is returned at once, and the other gets at most 1 s. A busy engine is skipped. A crashed engine leaves the other working, and a dead Parakeet server shows as `error`. |

- **Exhaustive test:** every pair of 9 typical readings at 4 confidences (324 cases) keeps
  the stop and motion rules.
- **Unchanged runtime rules:** any stop word halts motion before any gate, and stops go to the
  parser, never the model.
- **Earlier results:** re-scoring the section 1 validation readings with the fixed rule
  changed no transcript.
- **Correction:** `bench_nvidia.py` compared intent names in the wrong case and counted
  "commands understood" over every phrase. The tables now count commands only (validation
  noise: 113/130, against 104/130 for whisper.cpp).

### 14.2 Reply format: does speaking first save time?

The model answers with one JSON object, `{"action": …, "args": …, "say": …}`. The voice
starts once the first sentence of `say` is complete. In the current format, the action and its
arguments are written before any speech.

| Format | Description |
|---|---|
| A, current | `action` first, then `say` |
| B | a short first sentence |
| C | `say` first, then `action`, with a short first sentence |

Method:
- **Held-out set 3:** 16 scenarios with 19 scored steps, run twice. It was written before any
  format was tried and was never used to tune anything (`conversation_eval.py --set h3`).
- **Accuracy:** B and C were also run on every earlier set (`--set all3`, 100 scenarios).
- **Latency:** `latency_check.py --vision-load --stt parakeet+whisper`. The three formats were
  interleaved over two rounds, with 20 model turns each.
- **Code:** the formats were a temporary prompt switch, removed after the decision.

| | A: current | B: short first sentence | C: `say` first + short |
|---|---|---|---|
| End of speech to voice, model turns, p50 (n=20) | 2.00 s (p90 2.52) | 1.86 s (p90 2.24) | **1.56 s** (p90 1.94) |
| Difference from A, 95% bootstrap interval | | -329 to +95 ms | -685 to -184 ms |
| First sentence to voice, p50 (evaluation runs) | 1.09 s | 1.10 s | 0.72 s |
| Right action, held-out set 3 (38 steps) | 76.3% | 73.7% | 78.9% |
| Right action, held-out set 2 (32 steps) | 87.5% | 87.5% | 81.2% |
| Right action, earlier sets (146 steps) | 142 | 143 | 138 |
| Context used correctly, earlier sets (146 steps) | 140 | 129 | 130 |
| Motion requests sent | 0 | 0 | 0 |
| Decision | **kept** | not adopted | not adopted |

Format A's held-out set 2 and earlier-set figures come from the Milestone 5.5 run
(`m55-eval-everything-*.json`). Its set 3 figure comes from `m55-eval-h3-*.json`. The B and C
files are `m55-eval-all3-variant{B,C}-*.json` and `m55-latency-*-variant{B,C}-*.json`.

- **B** was no measurably faster. Openers under 4 words wait for the next sentence, so a
  shorter first sentence rarely starts the voice sooner. It also used context worse: 87.5%
  against 98.8% on the 33 unseen scenarios.
- **C** was about 0.44 s faster, but it was worse in three ways:
  - it chose worse actions on held-out set 2 and on the earlier sets (missed pauses, a goal
    taken from a trivia question);
  - it used context worse;
  - its words come before the action is chosen. It said "I'm already retreating" and "I'm
    pulling back" with the wheels locked.

  These were words only: no format sent a motion request.

The language model keeps format A. C is the largest speed gain measured so far. It could be
tried again only if its accuracy matches A's on a new held-out set.

### 14.3 Reply guard

Before a sentence is spoken, `dialogue.check_reply` refuses claims the robot cannot back up:
moving, seeing, reaching, watching, feeling. Format C exposed two gaps, and both also applied to
format A:
- The model writes "I’m" with a curly apostrophe in 56% of replies, and the patterns only
  matched "I'm".
- Motion claims with an adverb ("I'm already retreating") or with "pulling back" slipped
  through.

The fix:
- apostrophes are normalised before matching;
- the patterns allow up to two adverbs (never "not");
- they catch "pulling back/away" and sentences that start with the move.

All 1,319 earlier spoken replies were checked again, and only real claims are newly refused.
The tests are in `test_companion.py`.

### 14.4 A person talking to it: not run yet

No human result exists yet. `backend/scripts/human_conversation_test.py` is ready and was
checked with recorded files (`--dry-run`):
- push-to-talk in a terminal, on the real runtime with a simulated robot;
- the speaker confirms or corrects each transcript and judges each reply;
- audio and transcripts stay in the git-ignored `data/human_tests/`.

### 14.5 Memory, swap and disk

Memory, swap and disk were sampled every 30 s for two hours (`memory_pressure`,
`vm.swapusage`, `vm_stat`, `df`). The format experiment ran first, with every model loaded:
Qwen3-4B, Kokoro, Parakeet, whisper.cpp and the simulated camera load. The owner's usual apps
stayed open throughout.

| | During the runs (19:07-19:44, 74 samples) | After (19:44-21:07, 166 samples) |
|---|---|---|
| Swap in use | 8.1-11.1 GB (median 9.7) | 7.9-9.9 GB (median 8.4) |
| macOS "memory free" | 25-70% (median 52%) | 57-76% (median 64%) |
| Compressed memory | 0.9-5.1 GB | 1.0-3.9 GB |

- **Low point:** 25% free with 11.1 GB of swap, at 19:09, early in the runs. Swap stayed near
  8 GB once the runs ended.
- **Evaluation-only models moved to the Trash on 2026-11-01:** the five folders (4.0 GB) are Nemotron-3-Nano-4B
  GGUF, llama.cpp b12032, Nemotron 3.5 ASR, Magpie and NanoCodec. The owner approved the move.
  Nothing has been deleted, and the space comes back when the Trash is emptied.
- **Adopted models:** `download_models.py --verify` checks 25 files, all present.
- **Test clips kept:** the LibriSpeech clips (24 MB) stay in `data/asr_eval/`.
- **Free disk:** it moved between 3.0 and 6.7 GiB during the two hours. The swap files alone
  grew by 3 GB.

### 14.6 Verification

Run on 2026-11-06 on battery in Low Power Mode, so timings here are not representative.

| Check | Result |
|---|---|
| Backend tests | 853 passed, 3 skipped (the opt-in real-model tests: 2 Qwen, 1 Nemotron). 324 of the 334 new tests are the exhaustive speech-safety cases. |
| Ruff | clean |
| Dashboard type-check and production build | pass |
| `make offline-check` | READY, 0 missing, 2 warnings (Low Power Mode on, output muted). Combined speech heard "Lock me in for 25 minutes." with the network blocked (transcribe 141 ms); Qwen replied offline; backend start 0 outbound attempts. |
| 20-minute soak | not repeated after these changes; the last result is in section 7 |

### 14.7 Still open

- **Indirect requests:** these are still the weakest point, at 76.3% right actions on
  held-out set 3.
- **No-op style requests:** the model sometimes picks `set_style` when nothing would change.
  The runtime records these as "ignored".
- **Not yet tested:**
  - a noisy room, the venue and other speakers;
  - the Bluetooth delay;
  - the robot network (Z1-Z11);
  - the Milestone 5 checklist (H1-H10).

## 15. Commits

On `feature/nvidia-integration`, local, from `6078e56`:

- `66bab6f` Speech: NVIDIA Parakeet paired with whisper.cpp, the new default
- `ba58d18` Conversation: Nemotron-3-Nano-4B measured (not adopted); clearer action guide
- `5cb0714` Latency audit and AI pipeline diagnostics on the dashboard
- `edf27bf` Honest records: a no-op style request is "ignored"; text-only shows as text only
- `a887b7f` Docs: NVIDIA evaluation, 20-minute soak, README and model notes
- `e551ced` Speech safety audit: a stop is never weakened or delayed by the other recogniser
- `3eec55a` Response format measured (speech-first not adopted); reply guard hardened
- the Milestone 5.6 results and documentation in the commit that follows
