# LOCKED IN. Run `make help` for targets.
PORT ?= 8077
SPACETIME ?= $(shell command -v spacetime 2>/dev/null || echo "$(HOME)/.local/bin/spacetime")

.PHONY: help setup preflight quest-check agent-bench airlock airlock-watch airlock-check airlock-investigate airlock-gate airlock-contracts models verify-models backend frontend dev demo test lint bench clean-data soak build offline-check hardware airlock-build airlock-verify airlock-cloud airlock-mac

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Check this Mac, install pinned backend and frontend deps, download models (scripts/setup_mac.sh)
	./scripts/setup_mac.sh

preflight: ## Check this Mac without installing anything: chip, memory, disk, tools, models, what it can run
	./scripts/setup_mac.sh --check

airlock: ## Start AIRLOCK's real SpacetimeDB and security dashboard; no models or robot commands
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh

airlock-cloud: ## Run the dashboard against the team's existing locki-w91bw cloud deployment
	AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com AIRLOCK_DATABASE=locki-w91bw AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh frontend

airlock-build: ## Publish the local AIRLOCK module without deleting data and build the dashboard
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh build

airlock-verify: ## Exercise the deployed AIRLOCK module using separate authenticated clients
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh verify

models: ## Download the pinned models and check them against backend/models.sha256 (needs internet once)
	cd backend && uv run python scripts/download_models.py

verify-models: ## Check the downloaded models against backend/models.sha256 (offline)
	cd backend && .venv/bin/python scripts/download_models.py --verify

backend: ## Backend only, auto-reload, http://127.0.0.1:$(PORT)
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

frontend: ## Dashboard dev server, http://127.0.0.1:5174 (proxies to the backend)
	cd frontend && npm run dev

dev: ## Backend + dashboard dev server together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM; \
	(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload) & \
	(cd frontend && npm run dev) & \
	wait

# LLM=bolt uses nemotron-3.5-bolt:e4b through Ollama, if pulled. RELAY=1 also records missions with NeMo Relay.
MODEL_ENV = $(if $(filter bolt,$(LLM)),LOCKEDIN_LOCAL_LLM_BACKEND=ollama) $(if $(RELAY),LOCKEDIN_RELAY_ENABLED=true)

demo: ## Build the dashboard and serve everything from one process on :$(PORT). Local only: never contacts Fish Audio or a model hub
	cd frontend && npm run build
	cd backend && LOCKEDIN_OFFLINE=true $(MODEL_ENV) uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --timeout-graceful-shutdown 2

# Real Zeus Car (docs/legacy/HARDWARE_HANDOFF.md). The venv's Python is used directly, not `uv run`,
# so nothing tries to resolve packages on the robot's Wi-Fi (no internet there).
# The robot's voice goes to SPEAKER (an output device name, e.g. make hardware SPEAKER="JLab GO Party");
# empty = the device saved in the dashboard settings. LLM=bolt and RELAY=1 work as for make demo.
ZEUS_HOST ?= 192.168.4.1
SPEAKER ?=
HW_ENV = LOCKEDIN_ROBOT_DRIVER=zeus LOCKEDIN_ZEUS_AUTOCONNECT=true LOCKEDIN_ZEUS_HOST=$(ZEUS_HOST) \
	LOCKEDIN_ZEUS_PORT=8765 LOCKEDIN_ZEUS_CAMERA_URL=http://$(ZEUS_HOST):9000/mjpg LOCKEDIN_CAMERA_SOURCE=zeus \
	LOCKEDIN_VISION_AUTOSTART=true LOCKEDIN_VOICE_AUTOSTART=true LOCKEDIN_OFFLINE=true \
	$(if $(SPEAKER),LOCKEDIN_AUDIO_OUTPUT="$(SPEAKER)") $(MODEL_ENV)

build: frontend/dist/index.html ## Build the dashboard if its source changed (works offline)

frontend/dist/index.html: $(shell find frontend/src -type f) frontend/index.html frontend/package.json frontend/vite.config.ts
	cd frontend && npm run build

offline-check: ## Offline readiness: packages, models, dashboard, voice cache, audio, robot address (run on home Wi-Fi)
	cd backend && .venv/bin/python scripts/offline_check.py

hardware: build ## Real Zeus Car after joining its Wi-Fi: ESP32 camera, voice, monitor link, offline, disarmed, no motor commands
	@cd backend && $(HW_ENV) .venv/bin/python scripts/offline_check.py --no-smoke || { echo; echo "Fix the FAIL lines above first."; exit 1; }
	@echo; echo "LOCKED IN hardware mode -> http://127.0.0.1:$(PORT)   (Ctrl-C stops it; it never arms or moves the robot)"; echo
	cd backend && $(HW_ENV) .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --timeout-graceful-shutdown 2

quest-check: ## Code Quest with the real recognisers, model, voice render and pytest; no mic, camera, speaker or robot
	cd backend && .venv/bin/python scripts/quest_check.py

agent-bench: ## Score the local model doing the agent's job (LLM=bolt for Nemotron Bolt through Ollama)
	cd backend && .venv/bin/python scripts/agent_bench.py $(if $(filter bolt,$(LLM)),--backend ollama --model nemotron-3.5-bolt:e4b)

# AIRLOCK on this Mac: USB and app monitoring on, mic and camera off until pressed, offline. LOCK=real allows a real screen lock (after the owner has agreed to test one).
# OLLAMA=http://<private address>:11434 uses Nemotron on another of the team's Macs. QUIET=1 keeps LOCKI silent (speech is recorded as text only).
# With data/security/spacetime.json (scripts/airlock.py spacetime-setup) it connects to the team's SpacetimeDB, and arming is the owner's there.
airlock-mac: build ## AIRLOCK on this Mac: the backend with security monitoring on. Locks are dry runs unless LOCK=real; VISION=1 turns the desk camera on while armed
	cd backend && LOCKEDIN_SECURITY_ENABLED=true LOCKEDIN_OFFLINE=true $(if $(SPACETIME_URL),LOCKEDIN_SPACETIME_URL=$(SPACETIME_URL)) $(if $(SPACETIME_DB),LOCKEDIN_SPACETIME_DATABASE=$(SPACETIME_DB)) $(MODEL_ENV) $(if $(QUIET),LOCKEDIN_TTS_ENGINE=none) $(if $(LOCK),LOCKEDIN_SECURITY_LOCK_MODE=$(LOCK)) $(if $(LOCK_VIA),LOCKEDIN_SECURITY_LOCK_MECHANISM=$(LOCK_VIA)) $(if $(OLLAMA),LOCKEDIN_AIRLOCK_OLLAMA_URL=$(OLLAMA)) $(if $(VISION),LOCKEDIN_AIRLOCK_VISION=true) .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --timeout-graceful-shutdown 2

airlock-watch: ## AIRLOCK: print real USB and app events as typed events. ARGS="--launch-test-app" opens and closes Calculator
	cd backend && .venv/bin/python scripts/airlock_watch.py $(ARGS)

airlock-check: ## AIRLOCK: end-to-end check on this Mac with real OS calls; no mic, camera, speaker or real lock; each line says REAL or NOT RUN
	cd backend && .venv/bin/python scripts/airlock_check.py

airlock-investigate: ## AIRLOCK: a real investigation of a real app launch with a real model (ARGS="--require-nemotron" on the M5 Pro)
	cd backend && .venv/bin/python scripts/airlock_investigate.py $(ARGS)

airlock-gate: ## AIRLOCK: the malicious README scenario on real NeMo Relay and real pytest (ARGS="--model" also lets the local model try)
	cd backend && .venv/bin/python scripts/airlock_relay_demo.py $(ARGS)

airlock-contracts: ## AIRLOCK: regenerate the JSON Schemas and fixtures in docs/airlock/contracts/
	cd backend && .venv/bin/python scripts/airlock_contracts.py

test: ## Backend tests + frontend type-check/build
	cd backend && uv run pytest -q
	cd frontend && npm run build

lint: ## Ruff on the backend
	cd backend && uv run ruff check app tests scripts

bench: ## Reproduce docs/PERFORMANCE.md measurements (pass IMAGE=path/to/hands.jpg)
	cd backend && uv run python scripts/bench.py all $(if $(IMAGE),--image $(IMAGE),)

soak: ## Scripted demo + mixed sessions back to back on the simulated robot; checks invariants
	cd backend && uv run python scripts/soak_demo.py $(if $(DEMOS),--demos $(DEMOS),) $(if $(SESSIONS),--sessions $(SESSIONS),)

clean-data: ## Delete local telemetry, sessions and caches in data/ (asks first)
	@read -p "Delete everything in data/? [y/N] " ok && [ "$$ok" = "y" ] && rm -rf data/* && echo "data/ cleared"
