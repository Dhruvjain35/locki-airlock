#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# LOCKI AIRLOCK edge: READ-ONLY hardware inventory (HW-01). Run ON the Jetson:
#   cd ~/locki-airlock-edge && bash scripts/edge_jetson_inventory.sh
# Writes hardware/jetson_inventory.md (summary table on top, raw command output below) and prints the table.
# Changes nothing else: no installs, no settings. nvpmodel is only queried through "sudo -n" (never prompts;
# reported UNAVAILABLE when sudo wants a password). Every missing command or file is tolerated.
# Refuses on macOS. Honors the EDGE_ROOT / EDGE_UNAME / EDGE_PYTHON3 overrides of edge_jetson_detect.sh.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=edge_jetson_detect.sh
. "$SCRIPT_DIR/edge_jetson_detect.sh"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '4,9p' "$0"; exit 0 ;;
  *) echo "usage: bash scripts/edge_jetson_inventory.sh" >&2; exit 2 ;;
esac

edge_detect
if [ "$PLATFORM" = macos ]; then
  cat >&2 <<'EOF'
REFUSED: this is macOS (uname -s = Darwin). The inventory has to be read ON the Jetson:
  bash scripts/edge_deploy.sh <user>@<jetson-host>                    # on the Mac
  cd ~/locki-airlock-edge && bash scripts/edge_jetson_inventory.sh    # on the Jetson
Nothing was written.
EOF
  exit 2
fi

PY="${EDGE_PYTHON3:-python3}"
OUT_DIR="$REPO/hardware"
OUT="$OUT_DIR/jetson_inventory.md"
NOW="$(date '+%Y-%m-%d %H:%M:%S %z')"
HOST="$(hostname 2>/dev/null || echo unknown)"
WORK="$(mktemp -d 2>/dev/null || mktemp -d -t locki-inventory)" || { echo "mktemp failed" >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT
ROWS="$WORK/rows.md"
RAW="$WORK/raw.md"
: > "$ROWS"
: > "$RAW"

have() { command -v "$1" >/dev/null 2>&1; }
limited() { if have timeout; then timeout 20 "$@"; else "$@"; fi; }

# raw TITLE FILE: append one fenced block to the raw section
raw() {
  {
    printf '\n### `%s`\n\n```text\n' "$1"
    if [ -s "$2" ]; then LC_ALL=C tr -d '\000' < "$2" | awk '{ print }'; else echo "(no output)"; fi
    printf '```\n'
  } >> "$RAW"
}

# raw_file PATH: a file read through EDGE_ROOT
raw_file() {
  local f
  f="$(edge_path "$1")"
  if [ -r "$f" ]; then
    raw "cat $1" "$f"
  else
    printf '(missing: %s does not exist on this device)\n' "$1" > "$WORK/missing.out"
    raw "cat $1" "$WORK/missing.out"
  fi
}

# cap NAME TITLE CMD...: run CMD (stdout+stderr) into $WORK/NAME.out, exit code into $WORK/NAME.rc, add a raw block.
# A command that is not installed gets rc 127 and a note instead of an error.
cap() {
  local name="$1" title="$2" rc
  shift 2
  if have "$1"; then
    limited "$@" > "$WORK/$name.out" 2>&1
    rc=$?
  else
    printf '(not available: %s is not installed on this device)\n' "$1" > "$WORK/$name.out"
    rc=127
  fi
  echo "$rc" > "$WORK/$name.rc"
  raw "$title" "$WORK/$name.out"
}
rc_of() { cat "$WORK/$1.rc" 2>/dev/null || echo 127; }
last_line() { awk 'NF { line = $0 } END { print line }' "$WORK/$1.out" 2>/dev/null; }
status_of() { case "$(rc_of "$1")" in 0) echo VERIFIED ;; 127) echo MISSING ;; *) echo UNAVAILABLE ;; esac; }

# row FIELD VALUE STATUS: one summary-table row (pipes escaped, one line, max ~110 chars)
row() {
  local v
  v="$(printf '%s' "$2" | tr '\n\r\t' '   ' | sed -e 's/|/\\|/g' -e 's/  */ /g' -e 's/^ //' -e 's/ $//')"
  [ -n "$v" ] || v="(empty)"
  if [ "${#v}" -gt 110 ]; then v="${v:0:107}..."; fi
  printf '| %s | %s | %s |\n' "$1" "$v" "$3" >> "$ROWS"
}

# installed version of a dpkg package, empty when not installed
pkg_ver() {
  local s
  # shellcheck disable=SC2016  # dpkg-query format, not a shell expansion
  s="$(dpkg-query -W -f='${Status}|${Version}' "$1" 2>/dev/null || true)"
  case "$s" in "install ok installed|"*) printf '%s' "${s#*|}" ;; esac
}

# Python import probe row: FIELD CAPNAME
py_row() {
  case "$(rc_of "$2")" in
    0) row "$1" "$(last_line "$2")" VERIFIED ;;
    127) row "$1" "no python3 interpreter" MISSING ;;
    *) row "$1" "not importable" VERIFIED ;;
  esac
}

# ---- board + L4T --------------------------------------------------------------------------------------------
raw_file /proc/device-tree/model
raw_file /etc/nv_tegra_release
if [ "$BOARD_MODEL" != unknown ]; then row "Board model" "$BOARD_MODEL" VERIFIED; else row "Board model" "no /proc/device-tree/model" MISSING; fi
if [ -n "$L4T_LINE" ]; then row "L4T release line" "$L4T_LINE" VERIFIED; else row "L4T release line" "no /etc/nv_tegra_release (not an L4T system)" MISSING; fi
if [ "$L4T_MAJOR" != unknown ]; then row "L4T version" "R$L4T_MAJOR.$L4T_MINOR" DERIVED; else row "L4T version" "unknown" MISSING; fi
if [ "$JETPACK_FAMILY" != unknown ]; then
  row "JetPack family" "$JETPACK_FAMILY.x (L4T R$L4T_MAJOR maps to JetPack $JETPACK_FAMILY)" DERIVED
elif [ "$L4T_MAJOR" != unknown ]; then
  row "JetPack family" "unknown: L4T R$L4T_MAJOR is not R32/R35/R36" DERIVED
else
  row "JetPack family" "unknown (no L4T release file)" MISSING
fi

# shellcheck disable=SC2016
cap dpkg_core "dpkg-query -W nvidia-l4t-core nvidia-jetpack" dpkg-query -W -f='${Package} ${Version} ${Status}\n' nvidia-l4t-core nvidia-jetpack
for p in nvidia-l4t-core nvidia-jetpack; do
  if have dpkg-query; then v="$(pkg_ver "$p")"; row "$p (dpkg)" "${v:-not installed}" VERIFIED
  else row "$p (dpkg)" "dpkg-query not found" MISSING; fi
done

# ---- OS / kernel / memory / disk / power ----------------------------------------------------------------------
raw_file /etc/os-release
if [ "$OS_PRETTY" != unknown ]; then row "OS" "$OS_PRETTY" VERIFIED; else row "OS" "no /etc/os-release" MISSING; fi
printf '%s\n' "$ARCH" > "$WORK/uname_m.out"; raw "uname -m" "$WORK/uname_m.out"
printf '%s\n' "$KERNEL_RELEASE" > "$WORK/uname_r.out"; raw "uname -r" "$WORK/uname_r.out"
if [ "$ARCH" != unknown ]; then row "Architecture" "$ARCH" VERIFIED; else row "Architecture" "uname -m failed" MISSING; fi
if [ "$KERNEL_RELEASE" != unknown ]; then row "Kernel" "$KERNEL_RELEASE" VERIFIED; else row "Kernel" "uname -r failed" MISSING; fi

cap free "free -h" free -h
if [ "$(rc_of free)" = 0 ]; then
  row "RAM total" "$(awk '/^Mem:/ { print $2 }' "$WORK/free.out")" VERIFIED
  row "Swap total" "$(awk '/^Swap:/ { print $2 }' "$WORK/free.out")" VERIFIED
else
  row "RAM total" "free not available" "$(status_of free)"
  row "Swap total" "free not available" "$(status_of free)"
fi

cap df "df -h /" df -h /
if [ "$(rc_of df)" = 0 ]; then
  row "Disk /" "$(df -Ph / 2>/dev/null | awk 'NR == 2 { print $2 " total, " $4 " free (" $5 " used)" }')" VERIFIED
else
  row "Disk /" "df failed" "$(status_of df)"
fi

if ! have sudo; then
  echo "(not available: sudo not found)" > "$WORK/nvp.out"; echo 127 > "$WORK/nvp.rc"; raw "sudo -n nvpmodel -q" "$WORK/nvp.out"
elif ! have nvpmodel && [ ! -x /usr/sbin/nvpmodel ]; then
  echo "(not available: nvpmodel not found)" > "$WORK/nvp.out"; echo 127 > "$WORK/nvp.rc"; raw "sudo -n nvpmodel -q" "$WORK/nvp.out"
else
  cap nvp "sudo -n nvpmodel -q" sudo -n nvpmodel -q
fi
case "$(rc_of nvp)" in
  0)
    mode="$(sed -n 's/^NV Power Mode:[[:space:]]*//p' "$WORK/nvp.out" | head -n 1)"
    mode_id="$(last_line nvp | tr -dc '0-9')"
    row "Power mode (nvpmodel)" "${mode:-unparsed (see raw)}${mode_id:+ (mode $mode_id)}" VERIFIED ;;
  127) row "Power mode (nvpmodel)" "nvpmodel not found" MISSING ;;
  *) row "Power mode (nvpmodel)" "sudo -n refused (password needed): run sudo nvpmodel -q yourself" UNAVAILABLE ;;
esac

# ---- Python stack ---------------------------------------------------------------------------------------------
cap py_version "$PY --version" "$PY" --version
if [ "$(rc_of py_version)" = 0 ]; then
  row "Python 3" "$(last_line py_version | sed 's/^Python //') ($(command -v "$PY"))" VERIFIED
else
  row "Python 3" "$PY not runnable" "$(status_of py_version)"
fi
cap py_cv2 "$PY -c 'import cv2; print(cv2.__version__)'" "$PY" -c 'import cv2; print(cv2.__version__)'
cap py_numpy "$PY -c 'import numpy; print(numpy.__version__)'" "$PY" -c 'import numpy; print(numpy.__version__)'
cap py_ji "$PY -c 'import jetson_inference'" "$PY" -c 'import jetson_inference; print("importable")'
cap py_jil "$PY -c 'import jetson.inference'" "$PY" -c 'import jetson.inference; print("importable")'
cap py_cvbuild "OpenCV build info (GStreamer / V4L / FFMPEG / CUDA lines)" "$PY" -c \
  'import cv2; print("\n".join(l.strip() for l in cv2.getBuildInformation().splitlines() if any(k in l for k in ("GStreamer", "V4L", "FFMPEG", "CUDA"))))'
py_row "OpenCV (cv2)" py_cv2
py_row "numpy" py_numpy
py_row "jetson_inference" py_ji
py_row "jetson.inference (legacy)" py_jil

if have dpkg-query; then
  # shellcheck disable=SC2016
  dpkg-query -W -f='${db:Status-Abbrev}|${Package}|${Version}\n' 'tensorrt' 'libnvinfer*' 'python3-libnvinfer*' 2>/dev/null \
    | awk -F'|' '$1 ~ /^ii/ { print $2 " " $3 }' > "$WORK/trt.out"
  trt="$(awk '$1 == "tensorrt" { print; exit }' "$WORK/trt.out")"
  [ -n "$trt" ] || trt="$(awk '$1 ~ /^libnvinfer[0-9]+$/ { print; exit }' "$WORK/trt.out")"
  row "TensorRT (dpkg)" "${trt:-not installed}" VERIFIED
else
  echo "(not available: dpkg-query not found)" > "$WORK/trt.out"
  row "TensorRT (dpkg)" "dpkg-query not found" MISSING
fi
raw "dpkg-query: installed tensorrt / libnvinfer* / python3-libnvinfer*" "$WORK/trt.out"

# ---- camera / USB / audio -------------------------------------------------------------------------------------
cap lsusb "lsusb" lsusb
if [ "$(rc_of lsusb)" = 0 ]; then
  n_usb="$(grep -c '^Bus ' "$WORK/lsusb.out" 2>/dev/null || true)"
  logi="$(grep -i -m 1 -e '046d:' -e 'logitech' "$WORK/lsusb.out" 2>/dev/null | sed 's/^Bus [0-9]* Device [0-9]*: //' || true)"
  row "USB devices (lsusb)" "${n_usb:-0} devices${logi:+; Logitech: $logi}" VERIFIED
else
  row "USB devices (lsusb)" "lsusb not available" "$(status_of lsusb)"
fi

byid="$(edge_path /dev/v4l/by-id)"
if [ -d "$byid" ]; then
  ls -l "$byid" > "$WORK/byid.out" 2>&1
  names="$(ls -1 "$byid" 2>/dev/null | awk 'NF { printf "%s%s", sep, $0; sep = ", " }')"
  row "Cameras (/dev/v4l/by-id)" "${names:-empty directory}" VERIFIED
else
  echo "(no /dev/v4l/by-id: no V4L2 camera seen since boot)" > "$WORK/byid.out"
  row "Cameras (/dev/v4l/by-id)" "none (no /dev/v4l/by-id)" VERIFIED
fi
raw "ls -l /dev/v4l/by-id/" "$WORK/byid.out"

vids=""
for d in "$(edge_path /dev)"/video*; do
  [ -e "$d" ] && vids="$vids ${d#"${EDGE_ROOT:-}"}"
done
row "Video nodes" "${vids:- none}" VERIFIED

cap v4l_list "v4l2-ctl --list-devices" v4l2-ctl --list-devices
for d in "$(edge_path /dev)"/video*; do
  [ -e "$d" ] || continue
  cap "v4l_fmt_$(basename "$d")" "v4l2-ctl -d ${d#"${EDGE_ROOT:-}"} --list-formats-ext" v4l2-ctl -d "$d" --list-formats-ext
done

cap aplay "aplay -l" aplay -l
case "$(rc_of aplay)" in
  127) row "Audio playback (aplay -l)" "aplay not available (alsa-utils)" MISSING ;;
  *)
    cards="$(grep '^card' "$WORK/aplay.out" 2>/dev/null | awk '{ printf "%s%s", sep, $0; sep = "; " }')"
    row "Audio playback (aplay -l)" "${cards:-no playback devices listed (see raw)}" VERIFIED ;;
esac

# ---- network / clock / thermals -------------------------------------------------------------------------------
cap ip "ip -br addr" ip -br addr
if [ "$(rc_of ip)" = 0 ]; then
  nets="$(awk '$1 != "lo" { for (i = 3; i <= NF; i++) if ($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\//) { printf "%s%s %s", sep, $1, $i; sep = "; "; break } }' "$WORK/ip.out")"
  row "Network (IPv4)" "${nets:-no IPv4 address on any interface}" VERIFIED
else
  row "Network (IPv4)" "ip not available" "$(status_of ip)"
fi

cap timedatectl "timedatectl" timedatectl
if [ "$(rc_of timedatectl)" = 0 ]; then
  sync="$(sed -n 's/.*synchronized:[[:space:]]*//p' "$WORK/timedatectl.out" | head -n 1)"
  row "Clock synchronized" "${sync:-unparsed} (device time $(date '+%Y-%m-%d %H:%M %Z'))" VERIFIED
else
  row "Clock synchronized" "timedatectl not available" "$(status_of timedatectl)"
fi

: > "$WORK/thermal.out"
max=-1
max_name=""
for z in "$(edge_path /sys/class/thermal)"/thermal_zone*; do
  [ -r "$z/temp" ] || continue
  t="$(cat "$z/temp" 2>/dev/null)"
  ty="$(cat "$z/type" 2>/dev/null)"
  case "$t" in ''|*[!0-9]*) continue ;; esac
  printf '%s %s C\n' "${ty:-$(basename "$z")}" "$(awk -v t="$t" 'BEGIN { printf "%.1f", t / 1000 }')" >> "$WORK/thermal.out"
  case "$ty" in PMIC*) continue ;; esac   # the Nano's PMIC-Die zone always reads a fixed 100.0 C
  if [ "$t" -gt "$max" ]; then max="$t"; max_name="$ty"; fi
done
raw "thermal zones (/sys/class/thermal/thermal_zone*/type + temp)" "$WORK/thermal.out"
if [ "$max" -ge 0 ]; then
  row "Max temperature" "$(awk -v t="$max" 'BEGIN { printf "%.1f", t / 1000 }') C ($max_name)" VERIFIED
else
  row "Max temperature" "no readable thermal zone" MISSING
fi

# ---- write the report -----------------------------------------------------------------------------------------
mkdir -p "$OUT_DIR" || { echo "cannot create $OUT_DIR" >&2; exit 1; }
TMP_OUT="$OUT.tmp.$$"
{
  echo "# LOCKI AIRLOCK: Jetson hardware inventory"
  echo
  echo "Generated $NOW on host \`$HOST\` by \`scripts/edge_jetson_inventory.sh\` (read-only)."
  if [ -n "${EDGE_ROOT:-}" ]; then echo "**Files were read under EDGE_ROOT=\`$EDGE_ROOT\` (fixture/inspection mode), not this machine's /.**"; fi
  if [ "$PLATFORM" != jetson ]; then echo "**WARNING: this machine does not look like a Jetson (PLATFORM=$PLATFORM).**"; fi
  echo
  echo "| Field | Value | Status |"
  echo "|---|---|---|"
  cat "$ROWS"
  echo
  echo "VERIFIED = read from this device by this run. DERIVED = computed from a VERIFIED field. MISSING = the file or command does not exist here. UNAVAILABLE = it exists but could not be read (permission or password)."
  echo
  echo "Re-run on the Jetson: \`cd ~/locki-airlock-edge && bash scripts/edge_jetson_inventory.sh\`"
  echo
  echo "## Raw command output"
  cat "$RAW"
} > "$TMP_OUT" && mv -f "$TMP_OUT" "$OUT" || { rm -f "$TMP_OUT"; echo "could not write $OUT" >&2; exit 1; }

echo "LOCKI AIRLOCK Jetson inventory: $NOW, host $HOST, PLATFORM=$PLATFORM"
echo
echo "| Field | Value | Status |"
echo "|---|---|---|"
cat "$ROWS"
echo
echo "wrote $OUT"
