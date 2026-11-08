# Existing-owner consent window

The dashboard's **Authorize next voice ARM** control uses its existing persistent
owner SDK session. A browser profile name or CLI login does not authenticate that
browser. If the owner is authenticated only in the M4 CLI, the optional local owner
consent window can use that existing credential in place. It is a separate consent
process, not another security backend, voice service or USB watcher. It never
returns an owner token to the browser or the voice backend.

The M4 integrator must verify the CLI configuration path and token key locally,
then create a private mode-0600 JSON configuration. Values below are a template;
absolute local paths must be verified on the M4. No token belongs in this file.

```json
{
  "repositoryPath": "/ABSOLUTE/PATH/TO/NEW/INTEGRATION/CHECKOUT",
  "nodePath": "/ABSOLUTE/PATH/TO/node",
  "pythonExecutable": "/ABSOLUTE/PATH/TO/python3.11-or-newer",
  "server": "https://maincloud.spacetimedb.com",
  "database": "locki-w91bw",
  "expectedOwnerIdentity": "c200bf80129f645f49c762279a175c3c3e84cf56af437c06191a237ac3eac422",
  "workstationId": "dhruv-m4-air",
  "deviceId": "mac-707bf6209e27",
  "expectedMacIdentity": "c200b00c80d0775987509eee7514c0a711520552effa3e549b7f6c9940fcadab",
  "cliConfigPath": "/VERIFIED/ABSOLUTE/PATH/TO/cli.toml",
  "cliTokenKeyPath": ["spacetimedb_token"]
}
```

The identities and device above were verified against live enrollment. The token
key was verified on Sid's CLI, not on Dhruv's M4; do not assume its path/layout.
The SDK must authenticate as the exact expected owner or the window refuses all
operations. Credential/configuration files must be owned by the current OS user,
regular files without symlinks, and inaccessible to other users. Existing frontend
dependencies, Node and Python with `tomllib` are required; no model is loaded.

Integrator packaging: `scripts/airlock_owner_app.py` builds **LOCKI Owner Consent.app**
under `output/airlock-owner/`. The launcher requires macOS `/usr/bin/python3` and
opens a configuration file picker. It does not contain credentials or launch a
terminal. Build/install this app from the new audited integration checkout.

The owner's demo flow is:

1. Open the app and select its private configuration.
2. Review the exact workstation, enrolled Mac, fresh inventory, active application,
   approved applications, duration and auto-lock-off policy.
3. Approve once and keep the consent window connected.
4. Within 30 seconds, speak the ARM request to the existing M4 voice system.

Consent creates a fresh random authorization/session pair. The voice adapter
discovers exactly one matching current grant; it does not receive owner credentials
or require a terminal/restart for each grant. The grant cannot authorize disarming.
Closing the window requests revocation and disconnects its issuing owner SDK.
An authenticated-page heartbeat lease closes abandoned connections after 15 seconds;
grant expiry independently limits permission. A consumed grant does not disarm an
already armed workstation when the window closes.

The broker binds only loopback on an OS-selected port. Its local page uses a random
256-bit session secret, exact Host/Origin checks, JSON-only authenticated operations,
no cross-origin access, no remote page resources and a restrictive content policy.
Every issuance revalidates consent, identities, current connection, freshness,
inventory, active app and policy. The server atomically checks target membership.
Unconfirmed revocation closes the owner connection instead of leaving it authorized.

43 focused broker/security tests and an actual isolated-cloud consent-to-Mac-adapter
test passed. See [evidence/m4-integrated-arm-verification.json](evidence/m4-integrated-arm-verification.json).
The Finder launcher was built and syntax/manifest checked. Actual M4 installation,
existing credential/configuration, real microphone and audible confirmation remain
unverified. Port 8077 and its restart remain owned by the M4 CV Claude.
