# Code Quest

You tell LOCKI what you are building. It turns that into a mission whose checkpoints are your failing tests. You work
in your own editor. Every save runs the tests, and only their results move the mission. When every test passes, the
boss is beaten. No model, voice command or button can finish anything: there is no action for it.

## Try it

```bash
make demo                 # no robot needed. With the robot, on its Wi-Fi: make hardware SPEAKER="JLab GO Party"
```

1. Open http://127.0.0.1:8077. In **Code Quest**, type or say what you are building: "I'm building the checkout for my
   shopping cart."
2. With no folder approved it uses the demo project, a working copy of `examples/code_quest_demo` in
   `data/quests/shopping_cart` with its bugs intact. Open that folder in your editor.
3. Fix a bug in `cart.py` and save. About 2 seconds later the boss takes damage and LOCKI says so.
4. **Reset demo** puts the bugs back.

To use a real project, press **Approve a folder**. Its tests then run whenever you save. The project needs pytest in its
`.venv` or `venv` (otherwise the backend's Python is used).

Voice commands: "start a mission to ...", "I'm building ...", "what's next", "how much health does the boss have",
"run the tests", "why is this failing", "I'm stuck", "write me a fix", "drop the mission". Stop, quiet, pause and "give
me my phone back" work as always. Other questions go to the model with the mission in front of it.

## What counts as progress

`backend/app/missions/engine.py` holds the rules, and it does not import a model, the voice or the network.

- **Boss health** is one point per required test that did not report `passed` in the last conclusive run.
- A test counts only if pytest reported `passed`. Skipped, expected failures and unexpected passes do not count, and a
  missing test is not a passing one.
- **XP** is 10 per test the first time it passes, 50 per checkpoint the first time it is finished, 100 for the boss. It
  cannot be farmed by breaking and fixing the same test.
- Breaking something that passed heals the boss.
- A run that proves nothing (code that does not load, a timeout) changes nothing.
- **Editing or deleting a test file** stops runs from counting until you accept the change on the dashboard. Accepted
  removals stay on the record. Removing every failing test leaves no boss, which is not a win.
- Three saves in a row with no progress on a checkpoint: LOCKI offers a hint.

The evidence comes from a small plugin loaded into the project's own pytest (`missions/pytest_plugin/`), so nothing is
read back out of human-readable output.

## What the agent may do

| Tool | What it does | Limit |
|---|---|---|
| `run_tests` | Runs pytest | The command is fixed: no shell, no arguments from a model |
| `read_file` | Reads a project file for the model's next reply | Approved folder only; never `.env`, keys or `.git`; secrets redacted |
| `read_test_output` | Reads the real failure | |
| `propose_edit` | Puts a change on the dashboard | Changes nothing until you press **Apply it**; never a test file; the lines must match exactly once |
| `abandon_mission` | Drops the mission | |

Tests run in a macOS sandbox: outbound connections blocked, writes only inside the project and temp folders, no API keys
in the environment, stopped after 60 seconds. Those limits are tested against real pytest in `tests/test_mission_workspace.py`.

During a mission the model picks from these tools and a short list (pause, resume, quiet, release phone, stop). With no
mission running, its prompt is exactly what it was, so `docs/NVIDIA_EVALUATION.md` still applies to that path.

A model can word a plan, but only test ids pytest reported can appear in one, and every failing test ends up in a
checkpoint. Lines claiming progress are refused while the tests say otherwise. If a read tool is refused, its reason is
what gets said: asked to describe the failure, Qwen3 invented the contents of a file it had not read.

## Records

Each mission has a folder under `data/missions/<id>/` (git-ignored):

- `mission.json`: its state. A running mission comes back after a restart.
- `trace.jsonl`: **LOCKED IN's own log**, one real event per line: objective, model, tool requests and results,
  verification results, mission changes, what LOCKI said, a confirmed reach, errors, latency. Secrets and the home folder
  are redacted. It is not a NeMo Relay trace.
- `relay/events.jsonl`: only with `RELAY=1`. **Real NeMo Relay output**, in Relay's ATOF format (below).

## Choosing a model

The agent is one code path. `LOCKEDIN_LOCAL_LLM_BACKEND` picks the runtime behind it: `mlx` (Qwen3, the default) or
`ollama` (`make demo LLM=bolt` or `make hardware LLM=bolt`). The Ollama adapter is fixed to 127.0.0.1, refuses cloud models and untagged names, and
never downloads.

`make agent-bench` scores a model doing this job in the real app with real pytest, text only.

Qwen3-4B on this M4 Air (16 GB), 48 turns over 24 scenarios, 2026-11-07 02:26:

| Measure | Result |
|---|---|
| Right action | 97.9% (60.4% before the model's list was narrowed for missions) |
| Tools it invented / bad tool arguments | 0 / 0 |
| Turns where a guard refused a line | 10.4% |
| False progress spoken (read over the spoken words) | 0 |
| Mission changed by talking | 0 |
| Reply contains the fact it needed, where one was checked | 95.5% |
| Reply length | 16 words median, 29 longest |
| First token, p50 / p95 | 257 / 3170 ms |
| Whole turn, p50 / p95 | 1800 / 7970 ms (a read tool takes two passes, after a spoken "let me look") |
| Plans using the model's own grouping / problems the validator corrected | 100% / 2 across 6 plans |
| Plan time, p50 | 4110 ms |
| Model memory (MLX) | 2.55 GB active, 3.1 GB peak |
| Swap in use, before / after | 3576 / 3568 MB (the Mac was already swapping) |

Read these as a comparison between models, not as accuracy: the mission prompt was tuned against these same scenarios, so
expect a lower figure in real use. A few spoken lines are still weak. Text only: the numbers with voice and camera on are still
to come. Raw results are in `data/agent_bench/`.

### When the model fails

A mission doesn't depend on the model. It's saved after each change, and only pytest moves the boss.

| What goes wrong | What happens |
|---|---|
| The model times out or writes nonsense | LOCKI says the model is slow, or falls back to a fixed line. Plans fall back to grouping by test name, and hints use the test's own message |
| The model call raises an error, or a turn hits a bug | The error is logged and LOCKI still answers. The mic loop and the mission carry on |
| The model process dies | It restarts by itself, up to 3 times, and fixed lines cover the gap |
| The model stops answering | After 3 requests in a row with no token, the process is killed and restarted |

Tested on the real Qwen3 process: after `kill -9` it was answering again in 4.5 s. After being frozen with `SIGSTOP`, it was killed
on the third request and back in 4.4 s. A full mission also ran with stand-in models that time out, raise, write nonsense or
are dead, and the boss still moved only on pytest. Reading or refusing a named file needs no model, so it works throughout.

### Which file gets read

The model no longer chooses a file the person named. In that run, asked for `../../secrets.py`, Qwen3 sent `cart.py` both times,
so the refusal never fired and it answered from the wrong file. Now the person's words decide, including spoken forms such as
"tests slash test underscore cart dot py", and `Workspace.resolve` still decides whether that file may be read. Asking to see a
named file skips the model's first pass. A name close to a project file, such as `card.py`, opens that file and says so out loud.
A name that looks secret or leaves the folder is never swapped for another file.

Questions about a failure follow the same idea: "why is shipping failing" gets the shipping failure, not the current checkpoint's.

Checked with real Qwen3 on 16 sentences it had not seen, each twice (text, not speech): the file named was the file used every
time, and secret and outside paths were refused in about 1 ms without the model. When the person names no file the model still
chooses, and its answers can still be weak. "Can you see what's up with the shipping part?" got no tool and a meaningless reply.

### Nemotron 3.5 Bolt E4B: measured so far

Nothing has run yet. It is not downloaded, so there are **no Bolt results** and Qwen3 stays the default.

- Use the exact tag `nemotron-3.5-bolt:e4b`, 7.2 to 7.9 GB. The bare name `nemotron-3.5-bolt` is a **25 GB** model that
  Ollama's page says does not support Apple Silicon. "4GB" in NVIDIA's name is its memory use on their target hardware,
  not this download.
- On the M4 Air (16 GB) the download would leave about 3 GB free on this disk, and swap is already in use. Run it on the
  M5 Pro first: the steps are in [M5_BOLT.md](M5_BOLT.md).
- The model card's `conversational_mode` is a chat-template option. Ollama may not expose it, so a low score here could
  be that. It recommends temperature 1.0 and top_p 0.95; our benchmark uses the app's settings for every model.
- The adapter has only been tested against a stand-in for Ollama's protocol, plus the real local daemon's model list.

Make Bolt selectable as primary only if it beats these Qwen3 numbers within the latency and memory the demo can afford,
measured with voice and camera on.

## NeMo Relay

Tested on this M4 with version 0.11.1, in disposable folders:

- **CLI: works natively.** A wrapped `claude -p` run wrote a real file and Relay recorded 57 events: the turn, 2 model
  calls, the `Write` tool and streamed chunks. It injects hooks and a local gateway for that run only; Claude Code's
  settings were not changed. Persistent hooks (`nemo-relay install claude-code`) were not installed.
- **Python binding: also works natively.** PyPI publishes a macOS arm64 wheel for `nemo-relay`, so the Linux-only note
  is out of date. The documented quick start ran here, and it also ran with outbound connections blocked.

So our FastAPI runtime can be instrumented directly. With `make demo RELAY=1` or `make hardware RELAY=1` (after `scripts/setup_mac.sh --relay`), a
mission records pytest runs and tool calls as Relay tool scopes, model calls as Relay LLM scopes, and mission events as
marks. Relay keeps what it is given, including LLM request bodies with full payloads off, so it only gets names, sizes and
outcomes. If Relay fails, it is turned off for that mission and the mission carries on.

## Two Macs

`scripts/setup_mac.sh` checks chip, memory, disk and tools, then installs from `uv.lock` and `package-lock.json`.
`--check` installs nothing. `--bolt` asks first and refuses on a small disk. `--relay` adds the pinned Relay binding.
Weights stay in `backend/models` and Ollama's folder, keys in `.env`; none of them are in git.

Tested: from a fresh clone on this M4 with `--no-models`, it installed the pinned dependencies and built the dashboard,
94 mission tests passed there, and it correctly reported the models as missing. **Not tested:** the model download from
scratch, or anything on the M5 Pro.

## Cosmos Reason2: later

`backend/app/vision/analyzer.py` is the slot for it. MediaPipe keeps finding events; an analyzer may then be asked about
that frame. Its answer can only become a labelled observation for the mission agent: never a motor command, never evidence
for a checkpoint. Nothing implements it today, and nothing simulates one.

## What has really run

Nothing below has run on the robot. As of 2026-11-07:

| Step | Evidence | Status |
|---|---|---|
| A person speaks an objective into the microphone | none | **not run** |
| Speech recognition hears it | Parakeet + Whisper on the macOS voice from a file (`make quest-check`) | synthetic voice only |
| It becomes a mission with checkpoints | real Qwen3, real pytest | works |
| The dashboard shows it | real browser, with no robot or microphone connected | works |
| Follow-up questions use the mission | real Qwen3, typed and from the synthetic voice | works |
| LOCKI answers out loud | Kokoro rendered the reply; nothing was played | **not heard** |
| A real camera event is recorded in a mission | a simulated reach in tests; the Nov 1 log shows real reach detection, with no mission | **not run** |
| The spoken reply mentions the mission | real Qwen3 text | works as text, not heard |
| Save in an editor, tests decide | real pytest and file watcher; a script did the saving | works, no person yet |
| Stop, quiet, pause, phone release during a mission | tests with a stand-in model | works in tests |
| Bolt | not downloaded | **not run** |
| Relay | real CLI and real Python binding | works, not yet during a live demo |
| Motors | locked. Patch v2 not flashed ([FIRMWARE_AUDIT.md](FIRMWARE_AUDIT.md)) | **not run** |
