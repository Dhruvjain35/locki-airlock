# Nemotron Bolt on the M5 Pro

For Mustafa. The goal is to measure `nemotron-3.5-bolt:e4b` against Qwen3 in our real app, on the same Mac, so we can decide
which model runs the demo. Qwen3 stays the default unless Bolt wins these tests.

## Rules

- Use the exact tag `nemotron-3.5-bolt:e4b` (7.9 GB). Never run `ollama pull nemotron-3.5-bolt`: the bare name is a 25 GB
  model that does not support Apple Silicon.
- Everything runs on this Mac. No NVIDIA Build, no paid APIs.
- No robot in this test. Do not flash anything or connect to `Zeus_Car`.
- Do not change prompts or scenarios to help a model score better. Tell Dhruv what you saw instead.
- You do not need a `.env` file or any API key.

## 1. Get the code

From the bundle Dhruv sends you:

```
git clone -b feature/code-quest locked-in-code-quest.bundle locked-in
cd locked-in
```

Or, if the branch has been pushed: `git clone -b feature/code-quest git@github.com:Dhruvjain35/locked-in.git`.

## 2. Prepare the Mac

1. Plug in the charger and turn Low Power Mode off. It makes the local model about 4x slower.
2. Make sure at least 20 GB is free.
3. Install what is missing: [uv](https://astral.sh/uv), [Node](https://nodejs.org) and [Ollama](https://ollama.com/download).
   Open Ollama once so it is running.
4. Quit other large apps. Close spare browser tabs too.

## 3. Install

```
scripts/setup_mac.sh --bolt
```

This installs the pinned dependencies, downloads the pinned models (about 4.4 GB) and then asks before downloading Bolt. Type
`yes`. If you already have these models on a drive, copying `backend/models` from the other Mac works too; `make verify-models`
checks the copy.

The model download has not been tested from scratch, so if any step fails, send Dhruv the output.

Then check:

```
scripts/setup_mac.sh --check
ollama list
```

`ollama list` must show `nemotron-3.5-bolt:e4b`.

## 4. Quick check, about a minute

This is the first time our Ollama adapter talks to the real model. Until now it has only run against a stand-in.

```
cd backend && .venv/bin/python scripts/agent_bench.py --backend ollama --model nemotron-3.5-bolt:e4b --only T1 --repeats 1; cd ..
```

It should print `model nemotron-3.5-bolt:e4b (ollama) ready in ...` and then a result. If it prints an error, stop and send it
to Dhruv.

## 5. Benchmark both models

Run these one after the other, and leave the Mac alone while they run. Each took about 5 to 7 minutes on the M4 Air.

```
make agent-bench
make agent-bench LLM=bolt
```

Each one is text only: 24 fixed scenarios twice, a tool-failure test and 6 mission plans, using the real app and real pytest.
Neither uses the mic, camera or speaker.

Send Dhruv the two newest files from `data/agent_bench/`. They contain the test prompts and the models' replies on the demo project.

Also send the output of:

```
ollama ps
sysctl vm.swapusage
```

Run those two right after the Bolt benchmark, while the model is still loaded.

## 6. Real app with voice and camera

Do this only if step 5 looks close or better for Bolt. It needs a person talking, and it turns on your mic and camera when you
press the buttons. The app saves text only: transcripts, replies, timings and mission events. It saves no audio or video.

```
make demo LLM=bolt
```

Open http://127.0.0.1:8077. In another terminal, confirm the model:

```
curl -s http://127.0.0.1:8077/api/state | grep -o 'nemotron-3.5-bolt:e4b' | head -1
```

Then:

1. Press **Start mic** and **Start camera**.
2. Hold Space and say: "I'm building a shopping cart."
3. Wait for the boss battle to appear on the dashboard.
4. Hold Space and ask: "Why is this failing?"
5. Hold Space and ask: "What's next?"
6. Open `data/quests/shopping_cart/cart.py` in your editor, fix one bug and save. Watch the boss health.
7. Hold Space and ask: "How's the boss doing?"
8. Reach for the phone zone drawn on the video.
9. Press Ctrl-C in the first terminal.

Get the numbers and send them to Dhruv, along with how it felt to use:

```
cd backend && .venv/bin/python scripts/run_report.py; cd ..
```

Then repeat this step with Qwen3, using `make demo` with no `LLM=`. If a mission is still on the dashboard, drop it with the
trash icon. Then press **Reset demo** so both runs start from the same broken code.

The Fish Audio clips are not in git, so on this Mac every line uses the local Kokoro voice. That does not affect the comparison.

## 7. The AIRLOCK investigator

This is the part the NVIDIA track judges. It uses Bolt on a real incident with real tools, and needs no microphone, camera or speaker. It opens Calculator hidden and closes it again, so close Calculator first.

```
make airlock-investigate ARGS="--require-nemotron --relay"
```

It passes only if the status is `completed` and it says `counts as Nemotron: True`, which takes at least 3 approved tool calls by the exact tag. Send Dhruv the new file in `data/airlock_runs/`.

To serve Bolt to Dhruv's Mac instead, run Ollama with `OLLAMA_HOST=0.0.0.0` on a network you trust, because it has no password. Then on Dhruv's Mac set `LOCKEDIN_AIRLOCK_OLLAMA_URL=http://<this Mac's address>:11434`. Only private network addresses are accepted.

## 8. How we decide

Compare the two models on this Mac only. The M4 Air's numbers are in [CODE_QUEST.md](CODE_QUEST.md).

Bolt becomes the primary model only if all of these hold:

| Measure | Bolt must |
|---|---|
| Right action | match or beat Qwen3 |
| Tools invented, bad tool arguments | be 0 |
| False progress spoken, mission moved by talking | be 0 |
| Recovery after a tool failure | invent nothing |
| Plans the model made itself | match or beat Qwen3 |
| Whole turn, p50 and p95 | be no slower than Qwen3 |
| Swap, with voice and camera on | not grow during the run |
| The live run in step 6 | feel at least as natural |

Otherwise Qwen3 stays the default and Bolt stays optional through `LLM=bolt`.

Keep in mind that the mission prompt was tuned on Qwen3 against these same scenarios, which favours Qwen3. If Bolt loses
narrowly, that is not proof it is worse, but we would need a new held-out set before tuning for it. NVIDIA's model card also
recommends temperature 1.0 and top_p 0.95, and has a `conversational_mode` option that Ollama may not expose. Our benchmark uses
the app's own settings for both models.
