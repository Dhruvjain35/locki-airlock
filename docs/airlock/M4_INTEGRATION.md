# M4 integration coordination

## Camera enrollment

Read-only maincloud check during this change confirmed workstation `dhruv-m4-air`:

| Device | Role | State |
|---|---|---|
| `mac-707bf6209e27` | `mac` | enabled; existing Mac identity unchanged |
| `mac-dev-cam-01` | `jetson` | enabled; Areeb identity `c200fe52abd5ae6bf337f8bd5ff7995e468aa01382f819a48bd7ccc8e956ff74` |
| `cam-e2d5d60c513c` | not enrolled | its configured publisher identity is not available in Sid's checkout |

Do not substitute the M4 Mac-agent identity, CLI owner identity, or Areeb identity for
the separate camera. On the M4, inspect the actual camera publisher configuration and
compare its public configured identity with the authenticated identity reported by its
existing connection. Record the config path and public identity, **not the token**.
Do not start a second camera process just to inspect identity.

Once verified, the existing owner CLI calls the unchanged live `enroll_device`
contract in this order: `workstationId:string`, `deviceId:string`, `identityHex:string`,
`role:string`, `label:string`. The camera role is `jetson`, not owner/reviewer and not
a hardware identity. No runnable command with a guessed publisher identity is supplied.
Keep Areeb enabled. Real events require a live, non-simulated armed session, original
capture timestamps, sanitized metadata and owner-scoped event readback by device ID.

## Seven reported merge conflicts

No unmerged paths exist in Sid's working tree. The GitHub branch inventory currently
contains `feature/airlock-sid`, `main`, and `milestone-2`; Dhruv's local seven conflict
paths and opposite-side commit are not available here. They are **not resolved**.
Coordination request posted to [PR #1](https://github.com/Dhruvjain35/locked-in/pull/1#issuecomment-6715447150).

Dhruv should provide these read-only outputs from his current merge, plus a pushed
branch or patch that excludes credentials:

```bash
git status --short --branch
git diff --name-only --diff-filter=U
git rev-parse HEAD MERGE_HEAD
```

If no merge is in progress, supply the two branch commit IDs instead of `MERGE_HEAD`.
For each actual conflict, record base/ours/theirs, intended behavior on both sides,
selected resolution, owner of that edit, and a test proving both requirements survive.
Do not accept an entire side wholesale or regenerate bindings from an unresolved schema.
Preserve local work until the final contract is agreed.

The [ARM contract](ARM_DELEGATION.md) is additive and locally tested. Direct ARM and
DISARM remain owner-only. It must be integrated into Dhruv's voice path explicitly;
do not replace his implementation with Sid's generic speech handler. Spacetime
schema/reducer source is the source of truth; regenerate bindings after resolving it.
Camera metadata/timestamps, Mac identities, durable queues, fresh-baseline checks,
and reviewed owner policy are acceptance requirements on either side of a conflict.

## Accuracy and deployment status

[Volunteer protocol](ACCURACY_TRIALS.md) and [empty trial CSV](evidence/accuracy-trials-template.csv)
measure behavior discrimination and false warnings separately from camera transport.
No detector was added and no volunteer result is fabricated. Current person-in-region
evidence does not identify computer interaction or hostile intent. Production
deployability remains unproven.

New ARM code: isolated local deployment only. Maincloud still exposes its original
owner-only `arm_session`/`disarm_session`; no production grant table/reducer deployment,
M4 camera enrollment, permission widening, reset, screen lock or branch merge was
performed as part of this change.
