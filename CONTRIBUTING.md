# Contributing to LOCKI AIRLOCK

## Branches

| Branch | Rule |
|---|---|
| `main` | Verified code only. It changes only by a reviewed PR from `develop`, with the tests passing |
| `develop` | Integration. Every feature PR targets it |
| `feature/<lane>` | `feature/cv`, `feature/voice`, `feature/jetson-edge`, `feature/spacetimedb`, `feature/nemotron`, `feature/mission-control` |

1. Branch from `develop`.
2. Commit as you go: one commit per real change, saying what changed and why. Don't squash.
3. Run the tests for your area (README, "Tests").
4. Open a PR into `develop`. The path owner reviews it (`.github/CODEOWNERS`).
5. Report a merge conflict in the PR. Don't resolve it on someone else's files without them.

Never force-push a shared branch. Commit messages carry no AI co-author or "generated with" trailers.

## Adding your lane (snapshot, no old history)

This repository doesn't share history with `Dhruvjain35/locked-in`. Never push an old branch here, and never merge one in. Copy a snapshot of
your files instead:

```sh
git clone https://github.com/Dhruvjain35/locki-airlock.git ~/locki-airlock && cd ~/locki-airlock
git switch -c feature/jetson-edge origin/develop            # your lane
# export only tracked files from your old branch: no .git, .env, data/ or models
git -C <old-checkout> archive <your-branch> -- <your paths> | tar -x -C <your target folder>
git status                                                    # check that nothing secret or generated is staged
git add <your target folder> && git commit -m "Add the Jetson edge runtime (snapshot of feature/airlock-edge <sha>)"
git push -u origin feature/jetson-edge                        # then open a PR into develop
```

Where new lanes go:

| Lane | Folder |
|---|---|
| Jetson edge runtime | `edge/jetson/` (Sid's adapter is there now) |
| Mission Control | `tools/mission_control/` |
| Nemotron investigator service | `services/nemotron-investigator/` |

Say in the PR what you changed in shared contracts.

## Never commit

- `.env` and any token: SpacetimeDB tokens, Fish Audio or other API keys, Tailscale keys, SSH keys
- `data/`: device identity and salt, the owner's face template, enrollment samples, recordings, telemetry
- model weights and caches (`backend/models/`, `*.onnx`, `*.tflite`, `*.task`, `*.gguf`, `*.safetensors`, Ollama files)
- camera frames, screenshots, audio or incident evidence from a real run
- fixed private IP addresses in committed config

Before you push, check what's about to leave:

```sh
git diff --stat origin/develop...HEAD
git log origin/develop..HEAD -p | grep -nE 'eyJ[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{20,}|tskey-' || echo "no token patterns"
```
