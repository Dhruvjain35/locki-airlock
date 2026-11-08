#!/bin/bash
# LOCKI AIRLOCK: keeps the M4 -> M5 investigator tunnels up. Run by launchd (com.locki.airlock.m5-tunnel, KeepAlive), never by hand.
#
#   127.0.0.1:18787 -> M5 127.0.0.1:8787   Mustafa's Nemotron investigator API (Bearer key stays in ~/.locki/investigator.key)
#   127.0.0.1:11435 -> M5 127.0.0.1:11434  the M5's Ollama (nemotron model)
#
# Both forwards bind loopback only, so nothing is exposed to the network. Existing SSH auth is reused (the key named in
# ~/.locki/launcher.env, default ~/.ssh/id_ed25519); host keys must already be known (StrictHostKeyChecking=yes).
# If another healthy tunnel already serves a port (e.g. one started by hand), this leaves it alone and only takes over when it
# stops answering. A stale `ssh -N -L <port>` to the same M5 that has stopped answering for 30 s is ended so the port can be reused.
set -u
CONF="$HOME/.locki/launcher.env"
[ -f "$CONF" ] && . "$CONF"
M5_HOST="${LOCKI_M5_HOST:?LOCKI_M5_HOST missing in ~/.locki/launcher.env}"
M5_USER="${LOCKI_M5_USER:?LOCKI_M5_USER missing in ~/.locki/launcher.env}"
M5_KEY="${LOCKI_M5_KEY:-$HOME/.ssh/id_ed25519}"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

answers() {  # does something on this local port speak HTTP (any status)?
  local code
  code=$(/usr/bin/curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$1/$2" 2>/dev/null)
  [ -n "$code" ] && [ "$code" != "000" ]
}
holder() { /usr/sbin/lsof -t -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }

declare -a SICK=(0 0)
read -r -a PORTS <<< "${LOCKI_TUNNEL_PORTS:-18787 11435}"; REMOTE=(8787 11434); PATHS=(health api/tags)  # ports overridable only for tests
while true; do
  forwards=()
  for i in 0 1; do
    p=${PORTS[$i]}; pid=$(holder "$p")
    if [ -z "$pid" ]; then forwards+=(-L "127.0.0.1:$p:127.0.0.1:${REMOTE[$i]}"); SICK[$i]=0; continue; fi
    if answers "$p" "${PATHS[$i]}"; then SICK[$i]=0; continue; fi
    SICK[$i]=$(( ${SICK[$i]} + 1 ))
    cmd=$(/bin/ps -o command= -p "$pid" 2>/dev/null)
    if [ "${SICK[$i]}" -ge 3 ] && [[ "$cmd" == ssh*"-L"*"$p:127.0.0.1:${REMOTE[$i]}"*"$M5_HOST"* ]]; then
      log "port $p: stale ssh pid $pid has not answered for 30 s; ending it so the tunnel can be restored"
      kill "$pid" 2>/dev/null; sleep 1; SICK[$i]=0
    else
      log "port $p is held by pid $pid and is not answering (check ${SICK[$i]}/3)"
    fi
  done
  if [ ${#forwards[@]} -eq 0 ]; then sleep 10; continue; fi
  log "starting tunnel: ${forwards[*]} -> $M5_USER@$M5_HOST"
  /usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "$M5_KEY" \
    "${forwards[@]}" "$M5_USER@$M5_HOST" &
  SSH=$!; BAD=0
  sleep 5
  # While it runs: restart it if one of its ports stops answering for 30 s, or if a port it doesn't carry becomes free.
  while kill -0 "$SSH" 2>/dev/null; do
    restart=""
    for i in 0 1; do
      p=${PORTS[$i]}; pid=$(holder "$p")
      if [ -z "$pid" ]; then restart="port $p is free"; elif [ "$pid" = "$SSH" ] && ! answers "$p" "${PATHS[$i]}"; then BAD=$((BAD + 1)); [ $BAD -ge 3 ] && restart="port $p stopped answering"; fi
    done
    if [ -n "$restart" ]; then log "restarting tunnel: $restart"; kill "$SSH" 2>/dev/null; break; fi
    sleep 10
  done
  wait "$SSH" 2>/dev/null
  log "tunnel exited with status $?; retrying in 3 s"
  sleep 3
done
