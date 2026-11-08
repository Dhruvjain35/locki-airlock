#!/bin/bash
# One-time setup on Dhruv's M4 (before judging). Safe to run again: it updates files and never touches a running service.
#   install.sh            install launcher files, the M5 tunnel agent and the Desktop app
#   install.sh --uninstall remove the app and the agent (tunnels started by the agent stop; nothing else is touched)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.locki/launcher"; AGENT="$HOME/Library/LaunchAgents/com.locki.airlock.m5-tunnel.plist"; APP="$HOME/Desktop/LOCKI AIRLOCK.app"
if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/com.locki.airlock.m5-tunnel" 2>/dev/null || true
  pid=$(cat "$HOME/.locki/run/alert.pid" 2>/dev/null || true); [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  rm -f "$AGENT"; rm -rf "$APP" "$HOME/.locki/alert"; echo "Removed the app, the alert window and the tunnel agent. ~/.locki (config, logs, key) was kept."; exit 0
fi
mkdir -p "$DEST" "$HOME/.locki/logs" "$HOME/.locki/run" "$HOME/Library/LaunchAgents"; chmod 700 "$HOME/.locki" "$DEST"
install -m 700 "$HERE/locki_launch.sh" "$HERE/tunnel.sh" "$DEST/"; install -m 600 "$HERE/readiness.py" "$DEST/"
CONF="$HOME/.locki/launcher.env"
if [ ! -f "$CONF" ]; then
  # The M5 address and user of the tunnel that already works (no secrets: the key stays in ~/.ssh).
  # A new Mac: start LOCKI from the clone this installer runs from (backend/.venv and frontend/ made by `make setup`).
  REPO="$(cd "$HERE/../.." && pwd)"
  printf '%s\n' "LOCKI_M5_HOST=${LOCKI_M5_HOST:?set LOCKI_M5_HOST}" "LOCKI_M5_USER=${LOCKI_M5_USER:?set LOCKI_M5_USER}" "LOCKI_M5_KEY=${LOCKI_M5_KEY:-\$HOME/.ssh/id_ed25519}" \
    "LOCKI_BACKEND_ROOT=$REPO" "LOCKI_FRONTEND_DIR=$REPO/frontend" > "$CONF"
fi
chmod 600 "$CONF"
sed "s#__HOME__#$HOME#g" "$HERE/com.locki.airlock.m5-tunnel.plist" > "$AGENT"
if launchctl print "gui/$(id -u)/com.locki.airlock.m5-tunnel" >/dev/null 2>&1; then launchctl kickstart -k "gui/$(id -u)/com.locki.airlock.m5-tunnel"
else launchctl bootstrap "gui/$(id -u)" "$AGENT"; fi
rm -rf "$APP"; osacompile -o "$APP" "$HERE/LOCKI_AIRLOCK.applescript"
# The native alert window (Swift, needs Xcode or the Command Line Tools). A failed build leaves LOCKI usable; readiness reports it.
if bash "$HERE/../../native/locki-alert/build.sh" >> "$HOME/.locki/logs/alert-build.log" 2>&1; then echo "Built the alert window: ~/.locki/alert/LOCKI Alert.app"
else echo "The alert window did not build; see ~/.locki/logs/alert-build.log. Critical events will only show in the dashboard."; fi
echo "Installed. Double-click: $APP"
