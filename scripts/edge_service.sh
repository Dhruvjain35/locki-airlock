#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# LOCKI AIRLOCK edge: systemd unit for the runtime. Run ON the Jetson as your normal user:
#   bash scripts/edge_service.sh install            # DRY RUN: checks + prints the unit, changes nothing
#   bash scripts/edge_service.sh install --yes      # writes /etc/systemd/system/locki-airlock-edge.service
#   bash scripts/edge_service.sh uninstall [--yes]  # stop + disable + remove the unit (dry run without --yes)
#   bash scripts/edge_service.sh status             # read-only: unit file, env file, systemd state, /health
# install never starts or enables the service; it prints those commands. It refuses (exit 2) when the env file
# ~/.config/locki-edge/airlock.env is missing, not owned by you, or readable by group/world (must be chmod 600).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=edge_jetson_detect.sh
. "$SCRIPT_DIR/edge_jetson_detect.sh"

UNIT_NAME=locki-airlock-edge
UNIT_PATH="/etc/systemd/system/$UNIT_NAME.service"
OLD_UNIT_PATH=/etc/systemd/system/locki-edge.service
WORKDIR="$REPO/edge/jetson"
CONF_DIR="$HOME/.config/locki-edge"
ENV_FILE="$CONF_DIR/airlock.env"
TOKEN_FILE="$CONF_DIR/device_token"
TMP_UNIT=""
trap '[ -z "$TMP_UNIT" ] || rm -f "$TMP_UNIT"' EXIT

usage() { sed -n '4,10p' "$0"; }
refuse() { printf 'REFUSED: %s\nNothing was changed.\n' "$1" >&2; exit 2; }
say() { printf '%s\n' "$*"; }
run() {
  if [ "$YES" != 1 ]; then say "  would run: $*"; return 0; fi
  say "  \$ $*"
  "$@" || { printf 'FAILED (exit %s): %s\n' "$?" "$*" >&2; exit 1; }
}

CMD="${1:-}"
if [ $# -gt 0 ]; then shift; fi
YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
case "$CMD" in
  install|uninstall|status) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

edge_detect
[ "$PLATFORM" != macos ] || refuse "this is macOS (no systemd). Run this ON the Jetson."
[ "$(id -u)" != 0 ] || refuse "running as root. Run as the user the service should run as; the script calls sudo itself."
USER_NAME="$(id -un)"

# portable stat: GNU (Jetson) first, BSD (Mac test runs) second
# -L: judge a symlinked env file by its target (GNU stat reports the link itself as 777)
file_mode() { stat -L -c '%a' "$1" 2>/dev/null || stat -L -f '%Lp' "$1" 2>/dev/null || true; }
file_owner() { stat -L -c '%U' "$1" 2>/dev/null || stat -L -f '%Su' "$1" 2>/dev/null || true; }
loose_perms() { local m; m="$(file_mode "$1")"; [ -z "$m" ] || [ $((8#$m & 8#077)) -ne 0 ]; }

unit_text() {
  cat <<EOF
# Written by scripts/edge_service.sh (LOCKI AIRLOCK edge). Re-run that script instead of editing by hand.
[Unit]
Description=LOCKI AIRLOCK edge runtime (camera -> SecurityEventV1 -> SpacetimeDB)
After=network-online.target
Wants=network-online.target
# the older prototype unit must never hold the camera at the same time
Conflicts=locki-edge.service

[Service]
Type=simple
User=$USER_NAME
SupplementaryGroups=video audio
WorkingDirectory=$WORKDIR
ExecStart=/usr/bin/python3 -m locki_edge.runtime --env-file $ENV_FILE
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3
# exit 2 = the runtime refused its config: fix the env file instead of restarting every 3 s
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=15
NoNewPrivileges=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}

health_port() {
  local p=""
  if [ -r "$ENV_FILE" ]; then p="$(sed -n 's/^AIRLOCK_API_PORT=[[:space:]]*//p' "$ENV_FILE" 2>/dev/null | tail -n 1 | tr -dc '0-9')"; fi
  printf '%s' "${p:-8765}"
}

print_next() {
  say ""
  say "Not started (by design). Next:"
  say "  start now:      sudo systemctl start $UNIT_NAME"
  say "  logs:           journalctl -u $UNIT_NAME -f"
  say "  health:         curl -s http://127.0.0.1:$(health_port)/health"
  say "  start on boot:  sudo systemctl enable $UNIT_NAME"
  if [ -f "$OLD_UNIT_PATH" ]; then
    say "  old prototype unit found: sudo systemctl disable --now locki-edge   (this unit Conflicts= with it)"
  fi
}

do_install() {
  local mode owner
  [ "$PLATFORM" = jetson ] || refuse "platform is '$PLATFORM', not a Jetson (no parseable /etc/nv_tegra_release)"
  case "$WORKDIR$ENV_FILE" in
    *[[:space:]]*|*%*) refuse "the repo or home path contains a space or '%' (breaks the unit's ExecStart): $WORKDIR / $ENV_FILE" ;;
  esac
  if [ ! -f "$WORKDIR/locki_edge/runtime.py" ] && [ ! -f "$WORKDIR/locki_edge/runtime/__init__.py" ]; then
    refuse "$WORKDIR/locki_edge/runtime.py not found. Deploy the code first: bash scripts/edge_deploy.sh <user>@<host> (from the Mac)"
  fi
  if [ ! -f "$ENV_FILE" ]; then
    refuse "missing $ENV_FILE. Create it from the template (chmod 600), then edit it:
  mkdir -p -m 700 $CONF_DIR
  install -m 600 $WORKDIR/airlock.env.example $ENV_FILE"
  fi
  mode="$(file_mode "$ENV_FILE")"
  owner="$(file_owner "$ENV_FILE")"
  [ -n "$mode" ] || refuse "cannot read the permissions of $ENV_FILE"
  if loose_perms "$ENV_FILE"; then refuse "$ENV_FILE is group/world accessible (mode $mode). Fix: chmod 600 $ENV_FILE"; fi
  [ "$owner" = "$USER_NAME" ] || refuse "$ENV_FILE is owned by '$owner' but the service runs as '$USER_NAME'. Fix: sudo chown $USER_NAME $ENV_FILE"
  if [ -e "$TOKEN_FILE" ]; then
    if loose_perms "$TOKEN_FILE"; then refuse "$TOKEN_FILE is group/world accessible (mode $(file_mode "$TOKEN_FILE")). Fix: chmod 600 $TOKEN_FILE"; fi
  else
    say "note: no $TOKEN_FILE yet (the runtime needs it to publish; see edge/jetson/airlock.env.example)"
  fi
  [ -x /usr/bin/python3 ] || say "WARNING: /usr/bin/python3 not found; ExecStart will fail"
  say "env file OK: $ENV_FILE (mode $mode, owner $owner)"

  TMP_UNIT="$(mktemp)"
  unit_text > "$TMP_UNIT"
  say ""
  say "Unit $UNIT_PATH:"
  sed 's/^/    /' "$TMP_UNIT"
  say ""
  if [ "$YES" != 1 ]; then
    say "DRY RUN: nothing written. Apply with: bash scripts/edge_service.sh install --yes"
    return 0
  fi
  if [ -f "$UNIT_PATH" ] && cmp -s "$TMP_UNIT" "$UNIT_PATH"; then
    say "unchanged: $UNIT_PATH already matches"
  else
    run sudo install -m 0644 "$TMP_UNIT" "$UNIT_PATH"
  fi
  run sudo systemctl daemon-reload
  print_next
}

do_uninstall() {
  if [ ! -f "$UNIT_PATH" ]; then say "not installed ($UNIT_PATH absent): nothing to do"; return 0; fi
  run sudo systemctl disable --now "$UNIT_NAME"
  run sudo rm -f "$UNIT_PATH"
  run sudo systemctl daemon-reload
  run sudo systemctl reset-failed "$UNIT_NAME"
  if [ "$YES" != 1 ]; then say "DRY RUN: nothing changed. Apply with: bash scripts/edge_service.sh uninstall --yes"; fi
}

do_status() {
  local port
  say "PLATFORM=$PLATFORM  user=$USER_NAME"
  if [ -f "$UNIT_PATH" ]; then
    TMP_UNIT="$(mktemp)"
    unit_text > "$TMP_UNIT"
    if cmp -s "$TMP_UNIT" "$UNIT_PATH"; then say "unit: $UNIT_PATH (matches this script)"; else say "unit: $UNIT_PATH (DIFFERS from this script: re-run install --yes)"; fi
  else
    say "unit: not installed ($UNIT_PATH absent)"
  fi
  if [ -f "$ENV_FILE" ]; then
    say "env file: $ENV_FILE mode $(file_mode "$ENV_FILE") owner $(file_owner "$ENV_FILE")$(loose_perms "$ENV_FILE" && echo '  <- TOO OPEN: chmod 600')"
  else
    say "env file: MISSING ($ENV_FILE)"
  fi
  if command -v systemctl >/dev/null 2>&1; then
    say "enabled: $(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)"
    say "active:  $(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
  else
    say "systemctl not found"
  fi
  port="$(health_port)"
  if command -v curl >/dev/null 2>&1; then
    say "health:  $(curl -s -m 3 "http://127.0.0.1:$port/health" 2>/dev/null || echo "no answer on 127.0.0.1:$port")"
  else
    say "health:  curl not installed; try: wget -qO- http://127.0.0.1:$port/health"
  fi
}

case "$CMD" in
  install) do_install ;;
  uninstall) do_uninstall ;;
  status) do_status ;;
esac
