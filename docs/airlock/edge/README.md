# LOCKI AIRLOCK edge runtime

```text
 camera (Mac webcam | Jetson Logitech /dev/v4l/by-id | gst: pipeline)
   │  capture.py: reader thread, newest-frame mailbox, mono+UTC stamps, FPS, drops, reconnect, watchdog
   ▼
 observer (observers.py)          baseline motion | Jetson detectNet | OpenCV DNN | Dhruv's plugin | fixture(simulated)
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
   │  accepted person_in_zone only
   ▼
 notify.py                        presence push to ntfy (priority ≤ 3, rate limited, once per visit)

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

## Alerts

One genuine incident gives one consistent owner alert and one authorized Security Desk event, not several alarm
systems. SpacetimeDB decides incidents and severity. The M4 backend's security dispatch
(`backend/app/security/dispatch.py`) sends the owner ONE ntfy message per incident per severity, to the phone
topic in the backend's `data/notify/phone.json`. The Mac listener shows that same message. The Jetson sends
presence only, because it cannot read incidents. A person alone opens no incident and notifies nobody outside the
desk.

| Level | Comes from | ntfy title | Mac presentation |
|---|---|---|---|
| presence | Jetson, after SpacetimeDB accepted a `person_in_zone` | `LOCKI presence · <workstation>` (priority ≤ 3, tags `eyes`, `locki-presence`) | banner only, at most one per workstation per 5 min |
| elevated | reserved for verified interaction with protected equipment: **no producer yet** | none | banner + dialog, no sound |
| incident | M4 dispatch, severity Medium (Low/Info if ever sent) | `LOCKI Medium incident · <workstation>` | banner + dialog |
| alarm | M4 dispatch, severity High/Critical: correlated person + unknown USB, or owner panic | `LOCKI High incident · <workstation>` | banner + dialog + your alert sound, looped for at most 30 s |
| test | `backend/scripts/airlock_notify.py test` | `LOCKI test notification` | banner only |
| unknown | anything else on these topics, or a known format on the wrong topic | any | banner marked "unverified source" |

The dialog has Later / Open console / Acknowledge: Return is Acknowledge, Esc is Later, and keyboard navigation and
VoiceOver reach every button. It gives up after 120 s. Acknowledging there is local to this Mac. The SpacetimeDB
acknowledgement is done in the LOCKI console (owner) or the Security Desk. The listener never changes the volume:
sounds play at the current output volume, and banners follow your notification and Focus settings, so "displayed"
in the ledger means handed to Notification Center and nothing more. On the Jetson side, "accepted" means ntfy's own
receipt (its message id, kept in `push_audit.jsonl`), not proof that any screen showed it. No frames, images or
biometrics ever go over ntfy. No alert text claims that anyone beyond the owner's devices and the authorized
Security Desk was told anything.

**Jetson** (`airlock.env`): `AIRLOCK_PUSH_URL_FILE` (chmod 600 file with `https://ntfy.sh/<presence-topic>`) or
`AIRLOCK_PUSH_URL` (never both; push is off while both are empty), `AIRLOCK_PUSH_PRIORITY=3` (clamped to 1..3),
`AIRLOCK_PUSH_MIN_INTERVAL_S=600` per workstation+zone, plus at most one push per visit. A visit ends when the zone
clears locally (`person_cleared`; the visit ends whether or not the database takes that event). The
URL and topic are never logged; `/health` shows `ntfy …<last4>`. There is no test push from the edge any more:
`POST /local-test-alert` plays the local alarm only.

**Mac listener** (environment only; a URL on the command line is refused):

| Variable | Default | What |
|---|---|---|
| `AIRLOCK_ALERT_PHONE_CONFIG` | none | Dhruv's `data/notify/phone.json` (chmod 600): incidents, alarms, tests |
| `AIRLOCK_PUSH_URL_FILE` / `AIRLOCK_PUSH_URL` | none | the Jetson presence URL, same rules as on the Jetson. At least one source is required |
| `AIRLOCK_ALERT_STATE_DIR` | `~/.local/state/locki-alert` | `state.json` (cursor, dedupe) + `ledger.jsonl`; dir 0700, files 0600 |
| `AIRLOCK_ALERT_MAX_AGE_S` | 300 | older ntfy messages are `stale` and never shown |
| `AIRLOCK_ALERT_PRESENCE_COOLDOWN_S` | 300 | per workstation; extra presence is `suppressed` |
| `AIRLOCK_ALERT_DIALOG_S` / `AIRLOCK_ALERT_MAX_RING_S` | 120 / 30 | dialog give-up / alarm sound limit |
| `AIRLOCK_ALERT_SPEAK` | 0 | 1 = `say` once, alarm only |
| `AIRLOCK_ALERT_SOUND` | your macOS alert sound | file for the alarm loop |
| `AIRLOCK_ALERT_CONSOLE_URL` | `http://127.0.0.1:5174/?product=airlock` | "Open console" (loopback http or https only) |

The ledger has one line per transition: `received`, `duplicate`, `stale`, `suppressed`, `displayed`,
`display_failed`, `acknowledged`, `opened_console`, `not_acknowledged` (Later, Esc, or the dialog gave up). It
stores the parsed fields (key, ntfy id, level, severity, source, workstation, incident suffix, event id), never a
topic or a full body. Dedupe survives restarts (`ntfy:<id>` plus `incident:<suffix>#<severity>` or
`event:<uuid>`). The first start reads from now; later starts and reconnects resume from the last ntfy id. The
listener opens no ports and never calls the M4 backend: its only traffic is outbound HTTPS to the ntfy server.

Install and start on the M4, from the repo root (stop any older alert listener first; one per Mac):

```bash
# 1. Phone channel, once (prints the topic once, for the ntfy app; writes data/notify/phone.json mode 600)
(cd backend && .venv/bin/python scripts/airlock_notify.py setup-ntfy)
# 2. Optional: the Jetson presence topic in an owner-only file (same URL as the Jetson's AIRLOCK_PUSH_URL_FILE)
install -m 600 /dev/null ~/.config/locki-edge/push_url    # then put https://ntfy.sh/<presence-topic> in it
# 3. Check: config without secrets, a local preview, then a dry run
export AIRLOCK_ALERT_PHONE_CONFIG="$PWD/data/notify/phone.json" AIRLOCK_PUSH_URL_FILE=~/.config/locki-edge/push_url
python3 edge/jetson/tools/alert_listener.py status
python3 edge/jetson/tools/alert_listener.py show alarm
python3 edge/jetson/tools/alert_listener.py run --dry-run
# 4. Run it (same exports), then send one test from the backend and look for received + displayed in the ledger
python3 edge/jetson/tools/alert_listener.py run
(cd backend && .venv/bin/python scripts/airlock_notify.py test)
python3 edge/jetson/tools/alert_listener.py status
python3 edge/jetson/tools/alert_listener.py ack <key>      # local acknowledgement only
```

A LaunchAgent is optional: `ProgramArguments` = `/usr/bin/python3`, the absolute path to `alert_listener.py`, `run`;
`EnvironmentVariables` = the two file paths above (paths only, never a topic); `KeepAlive` true;
`StandardOutPath` inside the state dir. Its output never contains the topic.

## Files

| Path | What |
|---|---|
| `edge/jetson/locki_edge/{capture,observers,normalizer,contract,stdb,outbox,config,runtime}.py` | the runtime (stdlib + OpenCV/numpy, Python 3.6+) |
| `edge/jetson/contract/locki-w91bw.pinned.json` | deployed reducer/table signatures (read-only describe) |
| `edge/jetson/locki_edge/notify.py` | the presence push (the edge's only push) |
| `edge/jetson/tools/alert_listener.py` | the Mac alert listener (stdlib, `/usr/bin/python3`) |
| `edge/jetson/tools/stdb_live.py` | mint / whoami / denied / publish / verify against the real DB |
| `edge/jetson/tools/contract_check.py` | drift check vs the deployed module |
| `edge/jetson/tools/soak.py` | EDGE-01 soak → `docs/airlock/edge/soak/*/SUMMARY.md` (local output, not committed) |
| `edge/jetson/tests/test_contract.py`, `test_runtime_failures.py` | contract, capture, normalizer, outbox, auth, e2e tests |
| `docs/airlock/edge/DEPLOY.md` | Jetson runbook |

Legacy: `locki_edge/service.py` + `publish.py` speak the superseded single-string contract; kept for history, not used by the runtime except `SequenceStore`.

## Next to Sid's adapter

`edge/jetson/` also holds Sid's camera adapter (`airlock_camera.py`, `inventory.py`, `edge/jetson/README.md` and
`tests/test_camera_adapter.py`). The two share no code or files. `locki_edge` is the publisher deployed on `jetson-01`
(systemd unit `locki-airlock-edge`, API on 127.0.0.1:8765, state in `~/.local/state/locki-edge`); Sid's adapter serves on
127.0.0.1:8787 with its own state file. Never run both for the same device id: each keeps its own `seq` (the reducer rejects
`seq <= last_seq`) and both open the camera. Start the runtime as `python3 -m locki_edge.runtime` from `edge/jetson`, never as
`edge.jetson.locki_edge`, so its modules load only once.

## Tests

Run them from the repo root, as CI does (Sid's tests import `edge.jetson.*`, so they need the root as working directory):

```bash
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s edge/jetson/tests
```

Tests that need OpenCV skip where `cv2` is not installed, and `test_shellcheck` skips without `shellcheck`. On the device,
`scripts/edge_deploy.sh` and `scripts/edge_jetson_install.sh` run the same folder from `~/locki-airlock-edge` with
`/usr/bin/python3`.
