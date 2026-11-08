# Restore the existing M4 owner console

This flow authenticates an existing owner credential. It does not register a
workstation, assign a role, re-enroll a device or reset the database.

## Open on Dhruv's M4

Use the existing Chrome profile and this local dashboard:

<http://127.0.0.1:5174/?client=operator&airlockUri=https%3A%2F%2Fmaincloud.spacetimedb.com&airlockDatabase=locki-w91bw&workstation=dhruv-m4-air>

`127.0.0.1` refers to the computer running Chrome. Opening this address on Sid's
M5 reaches a different computer. The `operator` client name selects saved browser
credentials; it does not confer owner permissions.

The M4 must serve this frontend revision at the existing origin/port. M4 Claude
coordinates any frontend update. Keep the existing security backend on port 8077
running; this access fix requires no second backend or sensor process.

## Recover the existing credential

1. Click **Recover existing owner access** near the top of the main console.
2. Enter the existing workstation ID, **`dhruv-m4-air`**.
3. Choose a saved client slot that already holds the owner credential. If none
   does, select the **existing authenticated M4 CLI credential file locally**.
   Do not paste a token into chat, transfer it to another Mac, or generate a
   replacement owner identity.
4. Review the authentication destination: `https://maincloud.spacetimedb.com`,
   database `locki-w91bw`. Confirm using that existing credential and click
   **Verify existing owner**.
5. Verification reads only the caller-scoped workstation and membership views.
   It must establish both exact workstation ownership and an enabled owner
   membership. Invalid, unrelated or publisher credentials are rejected.
6. Click **Open the verified owner console**. This link uses a new saved client
   slot for the **same verified SpacetimeDB identity**. The old operator slot is
   preserved; the link contains no credential.

The expected existing owner is:

```text
c200bf80129f645f49c762279a175c3c3e84cf56af437c06191a237ac3eac422
```

Use the M4's actual existing CLI file. For CLI 2.14.1, the default is
`~/.config/spacetime/cli.toml`; XDG, `--root-dir` or `--config-path` overrides can
change it. M4 Claude should identify the active local file without printing its
contents. In Chrome's file chooser, **Command–Shift–G** can reach a hidden folder.
The flow accepts a plain token, JSON `token`/`spacetimedb_token`, or the CLI TOML's
top-level `spacetimedb_token`. The file is parsed locally; only the selected token
is sent to the displayed SpacetimeDB destination. [Versioned CLI path source](https://github.com/clockworklabs/SpacetimeDB/blob/v2.14.1/crates/paths/src/lib.rs),
[CLI credential field](https://github.com/clockworklabs/SpacetimeDB/blob/v2.14.1/crates/cli/src/config.rs).

## Arm the real Jetson test

1. Confirm the console is **connected**, the selected workstation is **dhruv-m4-air**, and the role is **Owner**.
2. Confirm `jetson-01` remains enabled with its existing `c2001edc…` identity.
   Enrollment is separate from whether the Mac is configured to consume it.
3. Have M4 Claude verify the running Mac's effective `remote_cameras` includes
   `jetson-01`. The M4's `/api/security` status exposes the actual subscriber
   workstation/device, camera-row count and last remote-camera receipt. A fresh
   receipt from this camera is functional evidence; a database device row alone
   is not a dump of the Mac's effective configuration.
4. Click **Arm workstation**, choose **Live → Owned workstation**, review the
   enrolled Mac's fresh captured USB baseline and approved applications, select
   a short test duration, and verify **Auto-lock policy: Disabled**.
5. Confirm **Arm live session**. Wait for the committed session readback.
6. Read **Session ID** and **Session provenance: Live · simulated=false** in the
   workstation panel. Response policy must still show automatic locking disabled.
   Give that actual session ID to Areeb; an ARM click or toast is not the receipt.

An explicit unavailable workstation never falls back to another owned desk or
offers workstation creation. A recovered link opened without its saved credential
stops before connecting instead of creating a replacement identity.

## Approve Mustafa's evidence

The owner-only **Investigator evidence access** panel calls the existing
`grant_incident_evidence` / `revoke_incident_evidence` contract. Enter Mustafa's
existing public identity, choose one genuine incident, explicitly select its
linked event subset and optionally its frozen session baseline, then review and
commit. No membership or session/action permissions are granted. Any evidence,
identity, owner connection or scope change invalidates the confirmation.

The grant has no automatic TTL; revoke after the investigation. A reducer receipt
confirms only the grant transaction. Mustafa must still verify his own identity's
approved views and denied out-of-scope/private access, then provide the genuine
model trace. See [investigator integration](INVESTIGATOR_INTEGRATION.md).

Source validation and mock tests do not mean the M4 owner has already logged in,
a real session has been armed, or Mustafa has actually read evidence.
