# LOCKI AIRLOCK edge runtime

```text
 camera (Mac webcam | Jetson Logitech /dev/v4l/by-id | gst: pipeline)
   │  capture.py: reader thread, newest-frame mailbox, mono+UTC stamps, FPS, drops, reconnect, watchdog
   ▼
 observer (observers.py)          baseline motion | Jetson detectNet | Dhruv's plugin | fixture(simulated)
   │  [VisionObservation(kind, confidence, zone_id, captured_at_utc, provider_version, metadata)]
   ▼
 normalizer.py                    capability guard → debounce/dwell → cooldown → rate limit → allowlist
   │  PendingEvent
   ▼
 contract.py                      SecurityEventV1 (uuid, seq, UTC, truthful source/simulated) → validate
   │                              → 11 positional args pinned from the DEPLOYED ingest_device_event
   ├──► events.jsonl (local audit, metadata only)
   ▼
 outbox.py (sqlite)               frozen args, strict seq order, bounded, expiry, crash-safe
   │  stdb.py: POST /v1/database/locki-w91bw/call/ingest_device_event  (Bearer = this device's own token)
   ▼
 PublishReceipt                   accepted | duplicate | rejected | retryable | ambiguous  (timeout ≠ success)

 monitor thread: watchdog → camera_unavailable (once per outage, no fake clear) → report_health(camera|edge)
 api (127.0.0.1:8765): GET /health  GET /metrics  GET /receipts  POST /local-test-alert
```

## Run it on the Mac (real webcam, local only)

```bash
cd ~/locki-airlock-edge/edge/jetson
AIRLOCK_EDGE_RUNTIME=mac_dev AIRLOCK_PUBLISH=off AIRLOCK_ZONES="desk_01:0,0,1,1" \
  ../../.venv/bin/python -m locki_edge.runtime --run-seconds 60
curl -s localhost:8765/health | python3 -m json.tool
```

## Publish to Sid's real DB (after enrollment)

1. `python3 tools/stdb_live.py mint --out ~/.config/locki-edge/device_token` (once per device; done for the Mac)
2. Owner calls `enroll_device(workstation_id, "mac-dev-cam-01", <identity>, <role>, <label>)`
3. `cp airlock.env.example ~/.config/locki-edge/airlock.env && chmod 600 ~/.config/locki-edge/airlock.env`, fill in `AIRLOCK_WORKSTATION_ID`
4. `../../.venv/bin/python -m locki_edge.runtime --env-file ~/.config/locki-edge/airlock.env`
5. `curl -s localhost:8765/receipts`: every event shows its real reducer result

## Files

| Path | What |
|---|---|
| `edge/jetson/locki_edge/{capture,observers,normalizer,contract,stdb,outbox,config,runtime}.py` | the runtime (stdlib + OpenCV/numpy, Python 3.6+) |
| `edge/jetson/contract/locki-w91bw.pinned.json` | deployed reducer/table signatures (read-only describe) |
| `edge/jetson/tools/stdb_live.py` | mint / whoami / denied / publish / verify against the real DB |
| `edge/jetson/tools/contract_check.py` | drift check vs the deployed module |
| `edge/jetson/tools/soak.py` | EDGE-01 soak → `docs/airlock/edge/soak/*/SUMMARY.md` |
| `edge/jetson/tests/test_contract.py`, `test_runtime_failures.py` | contract, capture, normalizer, outbox, auth, e2e tests |
| `docs/airlock/edge/STATUS.md` · `HANDOFF.md` · `DEPLOY.md` | truth table · what each teammate does · Jetson runbook |

Legacy: `locki_edge/service.py` + `publish.py` speak the superseded single-string contract; kept for history, not used by the runtime except `SequenceStore`.
