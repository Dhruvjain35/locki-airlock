# M4 integration coordination

## Camera enrollment

Read-only maincloud check during this change confirmed workstation `dhruv-m4-air`:

| Device | Role | State |
|---|---|---|
| `mac-707bf6209e27` | `mac` | enabled; existing Mac identity unchanged |
| `mac-dev-cam-01` | `jetson` | enabled; Areeb identity `c200fe52abd5ae6bf337f8bd5ff7995e468aa01382f819a48bd7ccc8e956ff74` |
| `cam-e2d5d60c513c` | not enrolled | M4-reported public identity is in INTEGRATION.md §2; verify the actual configured publisher locally |

Dhruv's audited [INTEGRATION.md](INTEGRATION.md) records the separate camera
identity `c200c34697345ca09abb33ead410578af099974512d4b0bc092971bd64393587`.
That is a team configuration report, not independent access to the current M4
publisher/token. Do not substitute the M4 Mac-agent identity, CLI owner identity,
or Areeb identity for the separate camera. On the M4, inspect the actual camera publisher configuration and
compare its public configured identity with the authenticated identity reported by its
existing connection. Record the config path and public identity, **not the token**.
Do not start a second camera process just to inspect identity.

Once verified, the existing owner CLI calls the unchanged live `enroll_device`
contract in this order: `workstationId:string`, `deviceId:string`, `identityHex:string`,
`role:string`, `label:string`. The camera role is `jetson`, not owner/reviewer and not
a hardware identity. No runnable command with a guessed publisher identity is supplied.
Keep Areeb enabled. Real events require a live, non-simulated armed session, original
capture timestamps, sanitized metadata and owner-scoped event readback by device ID.

## Migration and seven conflicts

The audited M4 snapshot is now available in private `Dhruvjain35/locki-airlock`,
starting at `integration/voice-cv-sid` commit `2212251`. Sid's review preserves the
seven explicit resolutions in [INTEGRATION_DECISIONS.md](../INTEGRATION_DECISIONS.md):
Python investigator retained with Sid transport moved, separate Make targets,
AIRLOCK default routing with retained `#demo`, package guard, both contracts,
new product README and absent robot firmware. No old Git history is merged.

The current review fixes actual voice integration gaps in that snapshot; it does
not replace the M4 runtime with Sid's standalone speech service or USB poller.
The M4 CV Claude owns port 8077 and the controlled restart. See
[M4_VOICE_ARM.md](M4_VOICE_ARM.md) for reviewed state/readback requirements and
[RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md) for open physical/model evidence.

## Accuracy and deployment status

[Volunteer protocol](ACCURACY_TRIALS.md) and [empty trial CSV](evidence/accuracy-trials-template.csv)
measure behavior discrimination and false warnings separately from camera transport.
No detector was added and no volunteer result is fabricated. Current person-in-region
evidence does not identify computer interaction or hostile intent. Production
deployability remains unproven.

ARM delegation is deployed to maincloud, with original owner-only direct
`arm_session`/`disarm_session` preserved. Independent cloud grant/consume/denial
tests passed; see [ARM_DELEGATION.md](ARM_DELEGATION.md). The real M4 owner consent
and voice/CV flow are not yet verified. The actual Jetson `jetson-01` is now enrolled
and enabled alongside the existing Mac and Areeb camera; the ARM deployment preserved
all three. No database reset, screen lock, or blind branch merge was performed.
