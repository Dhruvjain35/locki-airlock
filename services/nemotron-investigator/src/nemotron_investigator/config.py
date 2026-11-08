"""Service configuration. Every value can be overridden with an NI_* env var."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # services/nemotron-investigator


@dataclass(frozen=True)
class Settings:
    # Model / Ollama (local only; we never change the server's config or unload models)
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "nemotron-3.5-bolt:e4b"
    think: bool = False
    temperature: float = 0.1
    # Agent bounds
    max_model_turns: int = 8          # tool-selection turns (finalize call is extra)
    max_tool_calls: int = 10          # total executed tool calls per investigation
    max_calls_per_turn: int = 4
    step_timeout_s: float = 45.0      # per Nemotron inference request
    tool_timeout_s: float = 5.0       # per tool execution
    total_timeout_s: float = 150.0    # whole investigation
    max_tool_result_chars: int = 2200
    finalize_max_tokens: int = 1600     # hard cap on the assessment generation
    finalize_reserve_s: float = 25.0    # stop selecting tools when less than this remains
    # Prompt budget per model call. /api/ps reports a nominal 4096 for the shared runner, but full
    # retention was verified up to 9.8k tokens on this runtime (bench/context_probe.py, agent.py).
    context_token_budget: int = 6400
    # Optional NVIDIA NeMo Relay policy gate (pip install -r requirements-relay.txt)
    relay_gate: bool = False
    relay_max_usb_lookback_s: int = 600
    # API
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787
    api_keys_file: str = str(PACKAGE_ROOT / ".secrets" / "api_keys.json")
    store_dir: str = str(PACKAGE_ROOT / "var" / "investigations")
    # Default evidence backend: "fixture" (test data, labeled TEST DATA) or "spacetimedb" (live, read-only
    # investigator identity). fixture_enabled additionally accepts evidence_mode="fixture" requests on a
    # live server (for integration testing); fixture runs only ever take incident:fixture-* ids.
    evidence_backend: str = "fixture"
    fixture_enabled: bool = False
    fixture_path: str = str(PACKAGE_ROOT / "fixtures" / "scenarios")  # file or directory
    stdb_host: str = "https://maincloud.spacetimedb.com"
    stdb_database: str = "locki-w91bw"
    stdb_token_file: str = str(PACKAGE_ROOT / ".secrets" / "stdb_investigator.token")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        base = cls()
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"NI_{f.name.upper()}")
            if raw is None or raw == "":
                continue
            current = getattr(base, f.name)
            if isinstance(current, bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                values[f.name] = int(raw)
            elif isinstance(current, float):
                values[f.name] = float(raw)
            else:
                values[f.name] = raw
        values.update(overrides)
        return replace(base, **values)
