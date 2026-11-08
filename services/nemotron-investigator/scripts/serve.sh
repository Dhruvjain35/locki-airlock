#!/bin/sh
# Start the Nemotron investigator API on 127.0.0.1:8787 (loopback only).
#   default evidence: live LOCKI SpacetimeDB through the read-only investigator identity
#   NI_FIXTURE_ENABLED=1 additionally accepts evidence_mode="fixture" (TEST DATA) requests
# Usage: scripts/serve.sh            (foreground)
#        scripts/serve.sh --daemon   (background; log var/server_<port>.log, pid var/server_<port>.pid)
#        scripts/serve.sh --stop     (stop the daemon recorded in the pid file)
# The daemon start refuses if the port is already in use, and reports success only after the new
# process answers an unauthenticated /health with 401 (so a stale server can never pass as new).
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
export NI_EVIDENCE_BACKEND="${NI_EVIDENCE_BACKEND:-spacetimedb}"
export NI_FIXTURE_ENABLED="${NI_FIXTURE_ENABLED:-1}"
export NI_BIND_HOST="${NI_BIND_HOST:-127.0.0.1}"
export NI_BIND_PORT="${NI_BIND_PORT:-8787}"
PIDF="var/server_${NI_BIND_PORT}.pid"
LOG="var/server_${NI_BIND_PORT}.log"
mkdir -p var && chmod 700 var

listening() { lsof -nP -iTCP:"$NI_BIND_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

case "${1:-}" in
  --stop)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      kill "$(cat "$PIDF")" && rm -f "$PIDF" && echo "stopped"
    else
      echo "no running daemon recorded in $PIDF" >&2; exit 1
    fi ;;
  --daemon)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDF")); use --stop first" >&2; exit 1
    fi
    if listening; then
      echo "port $NI_BIND_PORT is already in use by another process; not starting" >&2; exit 1
    fi
    nohup "$PY" -m nemotron_investigator serve >> "$LOG" 2>&1 &
    pid=$!
    i=0
    while [ $i -lt 40 ]; do
      code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$NI_BIND_PORT/health" 2>/dev/null || true)
      if [ "$code" = "401" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid" > "$PIDF"
        echo "started pid $pid on 127.0.0.1:$NI_BIND_PORT (unauthenticated /health -> 401); log: $LOG"
        exit 0
      fi
      kill -0 "$pid" 2>/dev/null || break
      i=$((i + 1)); sleep 0.25
    done
    echo "server did not come up; last log lines:" >&2
    tail -n 15 "$LOG" >&2
    kill "$pid" 2>/dev/null || true
    exit 1 ;;
  "")
    exec "$PY" -m nemotron_investigator serve ;;
  *)
    echo "usage: $0 [--daemon|--stop]" >&2; exit 2 ;;
esac
