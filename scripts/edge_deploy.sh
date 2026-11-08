#!/usr/bin/env bash
#
# LOCKI AIRLOCK edge: push the edge code from the Mac to a Jetson and check it there.
#   bash scripts/edge_deploy.sh <user>@<host> [--install [--yes] [--with-detectnet] [--with-jetpack-meta] [--venv]]
# 1. rsync edge/jetson scripts docs/airlock/edge hardware -> ~/locki-airlock-edge/ on the target.
#    Never sent: .venv, __pycache__, *.pyc, *.env, *token*, keys, state dirs. Files already in the device's
#    hardware/ are never overwritten (a device-written inventory survives the next deploy).
# 2. Over ssh: scripts/edge_jetson_detect.sh + the unit tests with the device's own /usr/bin/python3.
# 3. Only with --install: scripts/edge_jetson_install.sh on the device, a DRY RUN unless --yes is also given.
# One ssh connection is shared (ControlMaster), so a password is asked at most once.
set -euo pipefail

usage() { sed -n '4,10p' "$0"; }
TARGET=""
INSTALL=0
YES=0
PASS=""
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --yes) YES=1 ;;
    --with-detectnet|--with-jetpack-meta|--venv) PASS="$PASS $arg" ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$TARGET" ]; then echo "only one target allowed (got '$TARGET' and '$arg')" >&2; exit 2; fi
      TARGET="$arg" ;;
  esac
done
[ -n "$TARGET" ] || { usage >&2; exit 2; }
case "$TARGET" in
  *[!A-Za-z0-9._@:%-]*) echo "refusing target '$TARGET': expected user@host" >&2; exit 2 ;;
esac
if [ "$INSTALL" = 0 ] && { [ "$YES" = 1 ] || [ -n "$PASS" ]; }; then
  echo "--yes / --with-detectnet / --with-jetpack-meta / --venv only mean something together with --install" >&2
  exit 2
fi
for tool in rsync ssh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing on this machine: $tool" >&2; exit 1; }
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
DEST_DIR=locki-airlock-edge
SSH_OPTS="-o ConnectTimeout=10 -o ControlMaster=auto -o ControlPersist=60 -o ControlPath=/tmp/locki-ssh-%C"
# Never sent, and never deleted on the device either (rsync leaves excluded files alone).
EXCLUDES=(--exclude=.git/ --exclude=.venv/ --exclude=venv/ --exclude=__pycache__/ "--exclude=*.pyc" --exclude=.DS_Store
  "--exclude=*.env" --exclude=.env "--exclude=.env.*" "--exclude=*token*" "--exclude=*.pem" "--exclude=*.key"
  "--exclude=id_rsa*" "--exclude=id_ed25519*" --exclude=.locki-edge/ --exclude=.local/ --exclude=locki-edge-state/
  --exclude=zone.jpg)

remote() {
  # shellcheck disable=SC2086  # SSH_OPTS is split on purpose
  ssh $SSH_OPTS "$TARGET" "$@"
}

CODE_PATHS=""
for p in edge/jetson scripts docs/airlock/edge; do
  if [ -e "$p" ]; then CODE_PATHS="$CODE_PATHS $p"; else echo "note: $p not in this checkout, skipped"; fi
done
[ -n "$CODE_PATHS" ] || { echo "nothing to send: run from the locki-airlock-edge repo" >&2; exit 1; }

# shellcheck disable=SC2086
SECRETS="$(find $CODE_PATHS hardware \( -name '*.env' -o -name '.env' -o -name '*token*' -o -name '*.pem' -o -name '*.key' \) -not -path '*/.venv/*' 2>/dev/null || true)"
if [ -n "$SECRETS" ]; then
  echo "never sent (excluded):"
  printf '%s\n' "$SECRETS" | sed 's/^/  /'
fi

echo "1/3 copy to $TARGET:~/$DEST_DIR/"
rc=0
remote "mkdir -p ~/$DEST_DIR/edge ~/$DEST_DIR/docs/airlock ~/$DEST_DIR/hardware && command -v rsync >/dev/null 2>&1" || rc=$?
if [ "$rc" = 255 ]; then echo "ssh to $TARGET failed (exit 255): check the address, cable/Wi-Fi, and that sshd runs on the Jetson" >&2; exit 1; fi
if [ "$rc" != 0 ]; then echo "rsync is missing on the Jetson. On the Jetson run: sudo apt-get install -y rsync   then re-run this." >&2; exit 1; fi
# One rsync per directory, trailing slashes: --delete only prunes stale files INSIDE that directory.
# (With -R, macOS openrsync also deleted in the implied parents edge/ and docs/: never use -R --delete here.)
for p in $CODE_PATHS; do
  rsync -az --delete "${EXCLUDES[@]}" -e "ssh $SSH_OPTS" "$p/" "$TARGET:$DEST_DIR/$p/"
done
if [ -d hardware ]; then
  rsync -az --ignore-existing "${EXCLUDES[@]}" -e "ssh $SSH_OPTS" hardware/ "$TARGET:$DEST_DIR/hardware/"
fi

echo "2/3 detect on the device (read-only)"
DETECT="$(remote "cd ~/$DEST_DIR && bash scripts/edge_jetson_detect.sh" 2>&1 || true)"
printf '%s\n' "$DETECT" | sed 's/^/  /'
case "$DETECT" in
  *PLATFORM=jetson*) ;;
  *) echo "  WARNING: the target did not report PLATFORM=jetson; the installer will refuse there." ;;
esac

echo "  unit tests with the device's /usr/bin/python3 (about a minute on a Nano)..."
TRC=0
TESTS="$(remote "cd ~/$DEST_DIR/edge/jetson && /usr/bin/python3 -m unittest discover -s tests" 2>&1)" || TRC=$?
printf '%s\n' "$TESTS" | tail -n 12 | sed 's/^/  /'
if [ "$TRC" = 0 ]; then echo "  unit tests: PASS"; else echo "  unit tests: FAIL (exit $TRC)"; fi

if [ "$INSTALL" = 0 ]; then
  echo "3/3 install: skipped (add --install for the dry-run plan, --install --yes to apply)"
  if [ "$TRC" = 0 ]; then exit 0; fi
  exit 1
fi
FLAGS="$PASS"
if [ "$YES" = 1 ]; then FLAGS=" --yes$FLAGS"; fi
echo "3/3 installer on the device:$FLAGS$([ "$YES" = 1 ] || echo ' (DRY RUN)')"
TTY=""
if [ -t 0 ] && [ -t 1 ]; then TTY="-t"; fi   # sudo on the device may ask for a password
IRC=0
# shellcheck disable=SC2086
ssh $TTY $SSH_OPTS "$TARGET" "cd ~/$DEST_DIR && bash scripts/edge_jetson_install.sh$FLAGS" || IRC=$?
if [ "$IRC" != 0 ]; then echo "installer exit $IRC (2 = refused, nothing changed; 1 = a step failed)"; exit "$IRC"; fi
# with --yes the installer's own verify step re-ran the unit tests after installing, so its 0 is the newest word
if [ "$YES" = 1 ] || [ "$TRC" = 0 ]; then exit 0; fi
exit 1
