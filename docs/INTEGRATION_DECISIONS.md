# Integration decisions (proposed, pending Sid's review)

This snapshot joins two development lanes that diverged from the same base:

| Side | Branch | Commit |
|---|---|---|
| base | merge base | `6078e56` |
| ours | `feature/airlock-dhruv`: Mac backend, CV, security modes, demo, and all voice work | `294c1ff` |
| theirs | `feature/airlock-sid`: SpacetimeDB module, dashboard, bridges, CI | `00f3c44` |

The commit IDs belong to the original `Dhruvjain35/locked-in` repository. None of its history is in this repository.

As Sid asked in `docs/airlock/M4_INTEGRATION.md`, each conflict below records the intent of both sides, the selected resolution, who owns the
edit, and the test that shows both requirements survive. **The resolutions are proposals.** The rows owned by Sid, or by Sid and Dhruv, are not
agreed until Sid approves them in review. Apart from the one-line docstring in row 4, neither side was taken wholesale, and the SpacetimeDB bindings were not regenerated:
`spacetime/src/index.ts` and `frontend/src/airlock-bindings/` are exactly Sid's `00f3c44`.

## The seven textual conflicts

| # | File | Ours (intent) | Theirs (intent) | Proposed resolution | Owner | Test that both survive |
|---|---|---|---|---|---|---|
| 1 | `backend/app/intelligence/airlock_agent.py` (add/add) | The Python investigator: 10 gated tools, NeMo Relay, deterministic report checks. `security/service.py` imports it | A bounded Nemotron HTTP transport, called by `scripts/airlock_nemotron.ts`. The TS adapter owns the tools and publishing | Keep ours at this path. Sid's file moves unchanged to `nemotron_transport.py`, and its two callers (`scripts/airlock_nemotron.ts`, `scripts/airlock_nemotron_test.py`) and `docs/airlock/NVIDIA.md` point there. **Open: which investigator consumes incidents in the demo.** Only one may | Sid and Dhruv | `backend/tests/test_airlock_agent.py`; `scripts/airlock_nemotron_test.py` (Sid's transport at its new path); `scripts/airlock_nemotron_tools.test.ts` |
| 2 | `Makefile` | `airlock` starts the Mac backend with security, camera and voice | `airlock` starts SpacetimeDB and the dashboard; also `airlock-cloud`, `airlock-build`, `airlock-verify` | Sid keeps `airlock` and his three targets. Ours becomes `airlock-mac` (same flags: `VISION=1`, `QUIET=1`, `LOCK=`), with `.PHONY` a union. `setup`/`preflight` stay ours (`scripts/setup_mac.sh` installs the same deps as Sid's `uv sync` line). Every doc and script that said `make airlock` for the Mac backend now says `make airlock-mac` | Sid and Dhruv | `make -n airlock`, `make -n airlock-cloud`, `make -n airlock-mac` each expand to their own side's command (see MIGRATION.md) |
| 3 | `frontend/src/App.tsx` | `#demo` and a Demo button open the live demo inside the Mac view | `App` routes to `AirlockApp` (his dashboard) by default and to `LegacyApp` with `?product=legacy` | Keep Sid's router and default. The Mac view is `LegacyApp`, which still opens the demo on `#demo`. The router also sends `#demo` to `LegacyApp` and follows hash changes, so a "Demo" link from the dashboard works without `?product=legacy` | Sid | `npm run build` (type check) and `npm test` in `frontend/`; by hand: `/` shows the security dashboard, `/#demo` the demo, `/?product=legacy` the Mac view |
| 4 | `backend/app/security/__init__.py` (add/add) | Package docstring: the Mac adapter, detection only | One-line docstring: workstation security is separate from robot motion safety | Ours. Sid's sentence ("Workstation security is separate from robot motion safety.") was tried and fails our guard `test_nothing_in_the_security_package_can_move_the_robot_or_touch_firmware`, which forbids the package from mentioning robot safety. Its meaning is that guard, so the test keeps it | Dhruv | `backend/tests/test_security_watchers.py` |
| 5 | `docs/airlock/CONTRACTS.md` (add/add) | Mac and camera messages, camera kind mapping, security modes and visual state, voice-arming proposal | Sid's answers: Mac subscriptions, exact metadata keys, health statuses and components, verification boundary | Both texts whole. Sid's comes first because his module is the authoritative contract; ours follows as "The Mac side". Where they differ, the module wins | Both | Both full texts are present unchanged (diff-checked in MIGRATION.md) |
| 6 | `README.md` | LOCKED IN description; "Built by Dhruv and Mustafa" | AIRLOCK introduction and dashboard quick start; all four builders | A new README for this repository. Both previous texts are kept whole in `docs/legacy/README_BEFORE_MIGRATION.md`. Sid's dashboard quick start is linked from `docs/airlock/README.md` | Dhruv | Doc only |
| 7 | `firmware/README.md` | Warning: do not flash patch v2 | AIRLOCK: archived, no flash, no motor commands | Both warnings kept in the merge. `firmware/` itself is **not shipped** here, because AIRLOCK uses no robot firmware | Dhruv | Doc only |

## Semantic conflicts that merged without a marker

| # | What | Why it matters | Proposed resolution | Owner |
|---|---|---|---|---|
| 8 | `Makefile`: Sid's new top-level `SPACETIME` variable is the path of the `spacetime` CLI. Our Mac target read `$(SPACETIME)` as the database URL | Unchanged, `make airlock-mac` would have pointed the Mac backend at the CLI binary's path as its server | Our variable is renamed `SPACETIME_URL` (`make airlock-mac SPACETIME_URL=... SPACETIME_DB=...`) | Dhruv |
| 9 | Sid's `backend/app/security/voice_service.py` (standalone loopback speech service), `announcements.py`, and `mac_observer.py` (USB snapshot poller) sit beside our voice stack and IOKit watcher | They are a second incident-to-speech path and a second USB monitor. `mac_observer.py` doesn't take our `usb-monitor.lock`, so it can run beside the backend. Nothing of ours imports them, and only Sid's `scripts/airlock_mac_bridge.ts`, `airlock_voice_bridge.ts` and `airlock_observer_smoke.ts` start them | Both kept and neither wired up. **Rule: one voice consumer and one USB monitor per workstation.** For the demo that's the Mac backend (`make airlock-mac`), so don't start Sid's voice or observer bridges beside it. Which one is the long-term path is for Sid and Dhruv | Sid and Dhruv |
| 10 | Sid's ARM delegation (`authorize_arm`, `arm_session_delegated`, `revoke_arm_authorization`, `docs/airlock/ARM_DELEGATION.md`) | It's additive and tested locally, but **not deployed** to `locki-w91bw`, and no client issues a grant yet. Sid asks that it be wired into Dhruv's voice path explicitly, not through his generic speech handler | Not wired. Voice arming still asks the owner to confirm on the dashboard, and voice disarm stays denied. Wiring it is a later PR on `feature/voice`, after Sid deploys the reducers. A grant must come from the owner's persistent SDK WebSocket | Dhruv after Sid deploys |
| 11 | Old checkout paths in `docs/airlock/DEMO.md` (`~/locked-in`) | They would point at the old repository | Now `~/locki-airlock` | Dhruv |

## Requirements both sides keep

These are acceptance requirements, whichever side of a conflict they came from:

- device-scoped views and identities
- original capture timestamps
- sanitized camera metadata
- durable queues
- fresh-baseline checks before arming
- reviewed owner policy before any lock
- dry-run locks
- owner-only arming and disarming
- a face match never disarms
