#!/bin/bash
# LOCKI AIRLOCK one-action launcher for Dhruv's M4. Started by "LOCKI AIRLOCK.app" on the Desktop.
#
#   locki_launch.sh [--app]    start or reuse everything, wait for READY (max 30 s), open the owner dashboard
#   locki_launch.sh --check    readiness only, starts nothing
#   locki_launch.sh --stop     stop what THIS launcher started (refuses while protection is armed); tunnels keep running
#
# Reuse first: anything already healthy on :8077, :5174, :18787 or :11435 is left exactly as it is, whoever started it.
# It never arms, never turns the camera or microphone on, never enables a real screen lock, never grants evidence.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOCKI="$HOME/.locki"; RUN="$LOCKI/run"; LOGS="$LOCKI/logs"; CONF="$LOCKI/launcher.env"; STATE="$LOCKI/launcher.state"
mkdir -p "$RUN" "$LOGS"; chmod 700 "$LOCKI"
[ -f "$CONF" ] && . "$CONF"
[ -f "$STATE" ] && . "$STATE"
T0=$(/usr/bin/python3 -c 'import time; print(time.time())')
log() { echo "$(date '+%H:%M:%S') $*" >> "$LOGS/launcher.log"; }
elapsed() { /usr/bin/python3 -c "import time; print(f'{time.time()-$T0:.1f}')"; }
code() { /usr/bin/curl -s -o /dev/null -m "${2:-2}" -w '%{http_code}' "$1" 2>/dev/null; }
listener() { /usr/sbin/lsof -t -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }
cwd_of() { /usr/sbin/lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; }

readiness() { LOCKI_INVESTIGATOR_KEY_FILE="$LOCKI/investigator.key" /usr/bin/python3 -I "$HERE/readiness.py" "$@"; }

if [ "${1:-}" = "--check" ]; then readiness; exit $?; fi

if [ "${1:-}" = "--stop" ]; then
  armed=$(/usr/bin/curl -s -m 3 http://127.0.0.1:8077/api/security | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("armed"))' 2>/dev/null)
  if [ "$armed" = "True" ]; then echo "Protection is ARMED. Press STOP PROTECTION in the dashboard first; nothing was stopped."; exit 2; fi
  for svc in frontend backend; do
    f="$RUN/$svc.pid"; [ -f "$f" ] || continue; pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then kill "$pid"; log "stopped $svc pid $pid (launcher-owned)"; echo "stopped $svc"; fi
    rm -f "$f"
  done
  echo "Launcher-owned services stopped. Services started elsewhere and the M5 tunnel agent were left running."; exit 0
fi

# One launch at a time (a second double-click waits for the first).
for _ in $(seq 1 40); do mkdir "$RUN/launch.lock" 2>/dev/null && break; sleep 0.25; done
trap 'rmdir "$RUN/launch.lock" 2>/dev/null' EXIT

# 1. M4 -> M5 tunnels: the launchd agent keeps them up. Load it if it isn't loaded (it never disturbs a healthy tunnel).
AGENT="$HOME/Library/LaunchAgents/com.locki.airlock.m5-tunnel.plist"
if [ -f "$AGENT" ] && ! launchctl print "gui/$(id -u)/com.locki.airlock.m5-tunnel" >/dev/null 2>&1; then
  launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>>"$LOGS/launcher.log" && log "loaded tunnel agent"
fi

# 2. Backend on :8077: reuse if it answers; otherwise start it from the checkout that last served it.
if [ "$(code http://127.0.0.1:8077/api/health)" = "200" ]; then
  pid=$(listener 8077); LAST_BACKEND_ROOT="$(dirname "$(cwd_of "$pid")")"; log "backend: reusing pid $pid ($LAST_BACKEND_ROOT)"
elif [ -n "$(listener 8077)" ]; then
  log "backend: port 8077 is held but not answering yet; waiting"
else
  root="${LOCKI_BACKEND_ROOT:-${LAST_BACKEND_ROOT:-}}"
  if [ -z "$root" ] || [ ! -x "$root/backend/.venv/bin/python" ]; then
    log "backend: no known checkout to start from"; echo "No backend checkout configured (LOCKI_BACKEND_ROOT in ~/.locki/launcher.env)." > "$RUN/last-error"
  else
    ( cd "$root/backend" && LOCKEDIN_SECURITY_ENABLED=true LOCKEDIN_OFFLINE=true LOCKEDIN_SPACETIME_REMOTE_CAMERAS="${LOCKI_REMOTE_CAMERAS:-jetson-01}" \
      LOCKEDIN_LOCAL_LLM_BACKEND=ollama LOCKEDIN_OLLAMA_URL=http://127.0.0.1:11435 LOCKEDIN_AIRLOCK_OLLAMA_URL=http://127.0.0.1:11435 \
      LOCKEDIN_AIRLOCK_FACE_IDENTIFICATION="${LOCKI_FACE_IDENTIFICATION:-true}" \
      nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8077 --timeout-graceful-shutdown 2 >> "$LOGS/backend.log" 2>&1 & echo $! > "$RUN/backend.pid" )
    log "backend: started pid $(cat "$RUN/backend.pid") from $root"
  fi
fi

# 3. Dashboard on :5174: reuse if it serves; otherwise start vite from the checkout that last served it.
if [ "$(code http://127.0.0.1:5174/)" = "200" ]; then
  pid=$(listener 5174); LAST_FRONTEND_DIR="$(cwd_of "$pid")"; log "dashboard: reusing pid $pid ($LAST_FRONTEND_DIR)"
elif [ -z "$(listener 5174)" ]; then
  dir="${LOCKI_FRONTEND_DIR:-${LAST_FRONTEND_DIR:-}}"
  if [ -n "$dir" ] && [ -f "$dir/package.json" ]; then
    ( cd "$dir" && VITE_SPACETIMEDB_URI=https://maincloud.spacetimedb.com VITE_SPACETIMEDB_DATABASE="${LOCKI_SPACETIME_DATABASE:-locki-w91bw}" \
      nohup npm run dev >> "$LOGS/frontend.log" 2>&1 & echo $! > "$RUN/frontend.pid" )
    log "dashboard: started pid $(cat "$RUN/frontend.pid") from $dir"
  fi
fi
printf 'LAST_BACKEND_ROOT=%q\nLAST_FRONTEND_DIR=%q\n' "${LAST_BACKEND_ROOT:-}" "${LAST_FRONTEND_DIR:-}" > "$STATE"

# 4. Wait for READY (at most 30 s from launch), checking once a second.
result=""; state="NOT READY"
while :; do
  result=$(readiness --json 2>/dev/null)
  state=$(printf '%s' "$result" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null || echo "NOT READY")
  [ "$state" = "READY" ] && break
  log "waiting ($(elapsed)s): $(printf '%s' "$result" | /usr/bin/python3 -c 'import json,sys; print(", ".join(i["name"]+" ("+i["detail"]+")" for i in json.load(sys.stdin)["items"] if i["required"] and not i["ok"]))' 2>/dev/null)"
  wait_more=$(printf '%s' "$result" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("worth_waiting", True))' 2>/dev/null || echo True)
  [ "$wait_more" = "False" ] && break
  awk "BEGIN{exit !($(elapsed) >= 30)}" && break
  sleep 1
done
secs=$(elapsed)
# Record the newly served checkouts if this launch started them.
if [ -n "$(listener 8077)" ]; then LAST_BACKEND_ROOT="$(dirname "$(cwd_of "$(listener 8077)")")"; fi
if [ -n "$(listener 5174)" ]; then LAST_FRONTEND_DIR="$(cwd_of "$(listener 5174)")"; fi
printf 'LAST_BACKEND_ROOT=%q\nLAST_FRONTEND_DIR=%q\n' "${LAST_BACKEND_ROOT:-}" "${LAST_FRONTEND_DIR:-}" > "$STATE"
echo "$(date '+%Y-%m-%d %H:%M:%S'),$state,$secs" >> "$LOGS/launch-times.csv"
log "result: $state in ${secs}s"

# 5. Open the owner dashboard in the dedicated, already-authenticated owner browser.
if [ -n "${LOCKI_DASHBOARD_URL:-}" ] && [ "$(code http://127.0.0.1:5174/)" = "200" ]; then
  open -na "Google Chrome" --args --user-data-dir="$HOME/.locki-owner-chrome" --no-first-run --no-default-browser-check \
    --disable-extensions --disable-sync "$LOCKI_DASHBOARD_URL" >/dev/null 2>&1
fi

printf '%s\n' "$state" "$secs"
printf '%s' "$result" | /usr/bin/python3 -c '
import json,sys
r=json.load(sys.stdin)
for i in r["items"]:
    if not i["ok"]: print(("REQUIRED  " if i["required"] else "NOTE      ")+i["name"]+": "+i["detail"]+("\n          → "+i["fix"] if i["fix"] else ""))
' 2>/dev/null
[ "${1:-}" = "--app" ] && exit 0   # the app reads the first line instead of the exit status
[ "$state" = "READY" ]
