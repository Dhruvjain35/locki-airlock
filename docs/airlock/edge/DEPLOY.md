# LOCKI AIRLOCK edge: Jetson deploy runbook

Every hardware step below is **PENDING**: none has run on a real Jetson yet. Verified so far: the five `scripts/edge_*.sh` pass `bash -n` and `edge/jetson/tests/test_deploy_scripts.py` on the Mac (fixture roots for R32 / R35 / R36 / non-Jetson, logging shims, no device).

The board model is **unknown** until step 4. PRD guess (unverified): original Jetson Nano 4GB on JetPack 4.x (L4T R32, Ubuntu 18.04, Python 3.6.9). It may be an Orin. The scripts detect the family on the device and refuse anything they do not recognize.

## 0. Before any cable: identify board and power

| Label on the board | Power it with | Status |
|---|---|---|
| Jetson Nano Developer Kit (4GB) | 5V⎓4A barrel jack **only with the J48 jumper fitted**, or micro-USB 5V⎓2A (may brown out under camera + TensorRT load) | PENDING |
| Jetson Orin Nano Developer Kit | its own 19V adapter, the one from its box | PENDING |
| Anything else, or unsure | stop: find NVIDIA's spec for that exact board before powering | PENDING |

Never improvise power. Never power the Jetson from the Zeus robot's motor supply or battery. This runbook connects exactly three things: power, the USB webcam, and network. No GPIO, no motor, no Arduino serial.

## Steps

| # | Step | Where | Done when | Status |
|---|---|---|---|---|
| 1 | Get a shell | Jetson | logged in | PENDING |
| 2 | Network check | Jetson | HTTP code prints, clock synchronized | PENDING |
| 3 | Deploy + remote tests | Mac | `PLATFORM=jetson`, `unit tests: PASS` | PENDING |
| 4 | Inventory | Jetson | `hardware/jetson_inventory.md` has VERIFIED rows | PENDING |
| 5 | Dry-run install plan | Jetson | plan matches the detected JetPack family | PENDING |
| 6 | Install with `--yes` | Jetson | ends with `DONE` | PENDING |
| 7 | Camera path | Jetson | by-id path copied, 640x480 MJPG listed | PENDING |
| 8 | Config + token files | Jetson | both `-rw-------` | PENDING |
| 9 | Foreground smoke run | Jetson | JSON log lines, no `config_refused` | PENDING |
| 10 | Service | Jetson | `systemctl is-active` says `active` | PENDING |
| 11 | Health check | Jetson / Mac | `/health` answers JSON | PENDING |

1. **Shell.** Monitor + keyboard, or Ethernet and `ssh <user>@<jetson-ip>`. The original Nano has no Wi-Fi: Ethernet or a USB Wi-Fi dongle. Headless over USB (Nano on barrel + J48, micro-USB to the Mac): `ssh <user>@192.168.55.1`, or the serial console:
   ```bash
   ls /dev/cu.usbmodem*  &&  screen /dev/cu.usbmodemXXXX 115200
   ```
2. **Network** (on the Jetson). A wrong clock breaks TLS to Maincloud.
   ```bash
   ip -br addr && ping -c 3 1.1.1.1
   timedatectl | grep -i synchronized
   curl -sS -m 5 -o /dev/null -w '%{http_code}\n' https://maincloud.spacetimedb.com
   ```
3. **Deploy** (on the Mac, repo root). Sends `edge/jetson`, the `scripts/edge_*.sh`, `docs/airlock/edge` and `hardware/` if present, never `.env` / token files, then runs detect + the `edge/jetson/tests` unit tests from `~/locki-airlock-edge` with the device's `/usr/bin/python3`. One ssh password prompt at most.
   ```bash
   bash scripts/edge_deploy.sh <user>@<jetson-host>
   ```
4. **Inventory** (on the Jetson, read-only), then copy it back to the Mac to read it. It is device data: this repo has no `hardware/` folder, so it is not committed here.
   ```bash
   cd ~/locki-airlock-edge && bash scripts/edge_jetson_inventory.sh
   scp <user>@<jetson-host>:locki-airlock-edge/hardware/jetson_inventory.md .   # on the Mac
   ```
5. **Read the plan.** Dry run by default: prints detected facts and the numbered plan, changes nothing.
   ```bash
   bash scripts/edge_jetson_install.sh
   ```
6. **Install.** JetPack 4.x: apt `python3-numpy v4l-utils alsa-utils rsync`, keeps the shipped cv2 and Python 3.6, adds you to `video` + `audio`, then runs the unit tests and the inventory. Add `--with-detectnet` only with 60+ minutes left (source build, often 30-60+ min on a Nano). `--with-jetpack-meta` installs `nvidia-jetpack` only if apt's candidate matches the detected family.
   ```bash
   bash scripts/edge_jetson_install.sh --yes
   ```
7. **Camera.** Use the stable by-id path, never `/dev/video0`.
   ```bash
   ls -l /dev/v4l/by-id/
   v4l2-ctl -d /dev/v4l/by-id/<usb-046d_...-video-index0> --list-formats-ext | grep -A3 MJPG
   ```
8. **Identity + enrollment.** Mint the Jetson's OWN identity on the Jetson (never copy the Mac's token), then send the printed `identity_hex` to Sid: `enroll_device("dhruv-m4-air", "jetson-01", <identity_hex>, "jetson", "Jetson Logitech camera")`. Wait for it:
   ```bash
   cd ~/locki-airlock-edge/edge/jetson && python3 tools/stdb_live.py mint --out ~/.config/locki-edge/device_token
   python3 tools/stdb_live.py wait-enrolled --token-file ~/.config/locki-edge/device_token
   ```
9. **Config.** Set `AIRLOCK_EDGE_RUNTIME=jetson`, `AIRLOCK_DEVICE_ID=jetson-01`, `AIRLOCK_CAMERA_SOURCE=<by-id path>`, `AIRLOCK_OBSERVER=detectnet` (only if `--with-detectnet` built; `baseline` motion is local-audit only because role `jetson` may not publish `motion_in_zone`). Keep `AIRLOCK_SESSION_ID=auto` and `AIRLOCK_CONTRACT_FILE=contract/locki-w91bw.live.json` from the template: the module only accepts `person_in_zone` with `{}` metadata inside the live armed session, and a heartbeat under 15 s (see `_live_answers` in `edge/jetson/contract/locki-w91bw.live.json`). Also from the template: `AIRLOCK_EXIT_S=15` (arm with the zone empty that long, then walk in) and `AIRLOCK_FORBIDDEN_IDENTITIES` = Dhruv's owner identity (the runtime refuses to start on that token). Set `AIRLOCK_RESTART_ON_STALL_S=60` on the Jetson: a V4L2 read hung that long exits 3 and systemd restarts it in 3 s. Zone: `AIRLOCK_ZONES=desk_01:x1,y1,x2,y2`, normalized to the frame and calibrated for this desk on the device.
   ```bash
   mkdir -p -m 700 ~/.config/locki-edge
   install -m 600 ~/locki-airlock-edge/edge/jetson/airlock.env.example ~/.config/locki-edge/airlock.env
   nano ~/.config/locki-edge/airlock.env && ls -l ~/.config/locki-edge/
   ```
10. **Smoke run** in the foreground before trusting systemd. Then the armed-session proof (EDGE-09): Dhruv arms, someone walks into view, `accept_run` verifies the row as this device.
   ```bash
   cd ~/locki-airlock-edge/edge/jetson && python3 -m locki_edge.runtime --env-file ~/.config/locki-edge/airlock.env --run-seconds 30
   python3 tools/accept_run.py --env-file ~/.config/locki-edge/airlock.env --seconds 1800 --until-accepted
   ```
11. **Service.** `install` alone is a dry run that prints the unit; `--yes` writes it. It refuses if the env file is missing or not chmod 600, and never starts or enables anything itself.
    ```bash
    bash scripts/edge_service.sh install && bash scripts/edge_service.sh install --yes
    sudo systemctl start locki-airlock-edge && journalctl -u locki-airlock-edge -f
    sudo systemctl enable locki-airlock-edge          # start on boot, once it behaves
    bash scripts/edge_service.sh status
    ```
12. **Health.**
    ```bash
    curl -s http://127.0.0.1:8765/health                                         # on the Jetson
    ssh -L 8765:127.0.0.1:8765 <user>@<jetson-host>                              # on the Mac, then:
    curl -s http://127.0.0.1:8765/health
    ```

## When it fails

| Symptom | Meaning | Fix |
|---|---|---|
| `REFUSED ... Nothing was changed.` (exit 2) | wrong board, unknown L4T, macOS, root, or bad env-file permissions | read the reason; never work around it |
| `FINISHED WITH FAILURES` (exit 1) | a post-install check failed (cv2, unit tests, inventory) | rerun the named command with `-v`; the inventory shows what is missing |
| Service stops with status 2, no restart loop | runtime refused its config (`RestartPreventExitStatus=2`) | `journalctl -u locki-airlock-edge -n 50`, fix `airlock.env`, `sudo systemctl restart locki-airlock-edge` |
| No `/dev/v4l/by-id/` | camera not enumerated | replug, `lsusb \| grep 046d`, `dmesg \| tail` |
| TLS / certificate errors | clock not synced yet | `timedatectl`; wait for NTP once the network is up |
| `curl` refused on 8765 | service down, or `AIRLOCK_API_PORT` changed | `bash scripts/edge_service.sh status` |
