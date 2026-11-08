# A new demo Mac (Areeb's): clean install from GitHub

Status: **not yet verified on Areeb's Mac.** Don't call it ready until the last step passes there. Dhruv's M4 stays the working fallback; nothing here touches it.

The clone brings code only. Each Mac gets its **own** identities and enrollment. Never copy Dhruv's `data/`, `~/.locki/`, SSH keys, investigator key or SpacetimeDB tokens.

## 1. Code and dependencies (Areeb, about 15 min plus a 4.5 GB model download)

```sh
git clone https://github.com/Dhruvjain35/locki-airlock.git ~/locki-airlock
cd ~/locki-airlock
git checkout <judging tag>        # the pinned release, never a moving branch on demo day
make setup                        # checks the Mac, installs pinned uv/npm dependencies, downloads models checked against backend/models.sha256
```

It needs macOS on Apple Silicon plus `uv`, `node`/`npm`, `git` and the SpacetimeDB CLI (`spacetime`). `make setup` names anything missing.

## 2. Areeb's own identities (needs Areeb, then the workstation owner)

| Step | Who | Command or action |
|---|---|---|
| SpacetimeDB login for the owner console and launcher checks | Areeb | `spacetime login` |
| A workstation for this Mac, with Areeb as its owner | Areeb | In the dashboard: **Create my workstation**. Or have Dhruv enroll Areeb on an existing one; never share a token. |
| This Mac's device identity | Areeb | `cd backend && .venv/bin/python scripts/airlock.py spacetime-setup --workstation <this Mac's workstation id>` (writes `data/security/spacetime.json`) |
| Enroll this Mac's verified identity as `mac` | The workstation owner | Dashboard: **Enroll identity** |
| Enroll the Jetson as this workstation's `jetson` camera, and point its publisher at it | Areeb (owns the Jetson) | See `edge/jetson/README.md`. One workstation per publisher config. |

## 3. Nemotron access (needs Mustafa)

- Mustafa adds **Areeb's own** SSH public key to the M5 account used for the tunnel, ideally restricted to port forwarding.
- Mustafa issues Areeb an investigator key. Save it as `~/.locki/investigator.key` and run `chmod 600` on it.
- Evidence still needs per-incident owner consent. The investigator can read only incidents the owner grants.

## 4. The launcher (Areeb, 1 min)

```sh
LOCKI_M5_HOST=<M5 Tailscale address> LOCKI_M5_USER=<M5 account> bash scripts/airlock_launcher/install.sh
```

This creates `~/Desktop/LOCKI AIRLOCK.app` and the M5 tunnel agent. A first install points the launcher at this clone.

## 5. Acceptance on Areeb's Mac (required before "ready")

1. Double-click **LOCKI AIRLOCK**, paste the owner dashboard address once, and reach **READY** in under 30 s. Do this 3 times and record `~/.locki/logs/launch-times.csv`.
2. **START PROTECTION**, then a real Jetson person event, then an unknown USB device. You should hear the audible warning and get a fresh incident.
3. **EXPLAIN INCIDENT**, approve, and hear the real Nemotron answer. Then **STOP PROTECTION**, and the grant revoke is confirmed.
4. Recovery: turn the M5 Wi-Fi off and back on (the tunnel heals), unplug the Jetson's network (READY drops, then returns), and switch the JBL off (the voice falls back to the Mac speakers).
