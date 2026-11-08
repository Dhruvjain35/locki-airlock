#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# LOCKI AIRLOCK edge: Jetson installer. Run ON the Jetson as your normal user (it calls sudo per step):
#   bash scripts/edge_jetson_install.sh              # DRY RUN: detect + print the numbered plan, change nothing
#   bash scripts/edge_jetson_install.sh --yes        # apply the plan
# Opt-ins (each still needs --yes before anything happens):
#   --with-detectnet     build dusty-nv/jetson-inference from source, official steps (often 30-60+ min on a Nano)
#   --with-jetpack-meta  apt-install nvidia-jetpack, only if apt's candidate is this device's JetPack family
#   --venv               JetPack 5/6 only: edge/jetson/.venv with --system-site-packages (JetPack cv2 stays visible)
# Refuses (exit 2, nothing changed): macOS, non-aarch64, no /etc/nv_tegra_release, L4T other than R32/R35/R36,
# running as root. Never runs apt upgrade / full-upgrade / dist-upgrade / do-release-upgrade, never flashes
# firmware or storage, never pip-installs into the system Python. Every step checks state before it acts.
# Exit: 0 done (or dry run), 1 a step or the final verification failed, 2 refused.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=edge_jetson_detect.sh
. "$SCRIPT_DIR/edge_jetson_detect.sh"

usage() { sed -n '4,14p' "$0"; }
YES=0
WITH_DETECTNET=0
WITH_JETPACK_META=0
WANT_VENV=0
FLAGS=""
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --with-detectnet) WITH_DETECTNET=1; FLAGS="$FLAGS $arg" ;;
    --with-jetpack-meta) WITH_JETPACK_META=1; FLAGS="$FLAGS $arg" ;;
    --venv) WANT_VENV=1; FLAGS="$FLAGS $arg" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

PY="${EDGE_PYTHON3:-python3}"
BASE_PKGS="python3-numpy v4l-utils alsa-utils rsync"
DETECTNET_PKGS="git cmake libpython3-dev python3-numpy"     # prerequisites from dusty-nv's building-repo-2.md
JI_URL="https://github.com/dusty-nv/jetson-inference"
JI_DIR="$HOME/jetson-inference"
VENV_DIR="$REPO/edge/jetson/.venv"
MODE=plan          # plan: read-only checks + print. apply: re-check + run.
N=0
APT_UPDATED=0
FAILED=""

say() { printf '%s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
step() { N=$((N + 1)); printf '\n%2d. %s\n' "$N" "$*"; }
refuse() {
  if [ "$MODE" = apply ]; then
    printf '\nSTOPPED: %s\nSteps above this point were applied; nothing after it was.\n' "$1" >&2
    exit 1
  fi
  printf '\nREFUSED: %s\nNothing was changed.\n' "$1" >&2
  exit 2
}
fail() {
  FAILED="$FAILED
  - $1"
  note "FAILED: $1"
}

# act CMD...: print it (plan) or print + run it (apply). A failing command stops the run (exit 1).
act() {
  if [ "$MODE" != apply ]; then note "would run: $*"; return 0; fi
  note "\$ $*"
  "$@" || { printf '\nFAILED (exit %s): %s\nStopped here; earlier steps were applied.\n' "$?" "$*" >&2; exit 1; }
}
# act_in DIR CMD...: same as act, inside DIR
act_in() {
  local dir="$1"
  shift
  if [ "$MODE" != apply ]; then note "would run: (cd $dir && $*)"; return 0; fi
  note "\$ (cd $dir && $*)"
  ( cd "$dir" && "$@" ) || { printf '\nFAILED (exit %s): (cd %s && %s)\nStopped here; earlier steps were applied.\n' "$?" "$dir" "$*" >&2; exit 1; }
}
# apt_install PKG...: "apt-get update" once per run, then install exactly these packages. Nothing else.
apt_install() {
  if [ "$APT_UPDATED" = 0 ]; then act sudo apt-get update; APT_UPDATED=1; fi
  act sudo apt-get install -y --no-install-recommends "$@"
}

# read-only probes
pkg_ver() {
  local s
  # shellcheck disable=SC2016  # dpkg-query format, not a shell expansion
  s="$(dpkg-query -W -f='${Status}|${Version}' "$1" 2>/dev/null || true)"
  case "$s" in "install ok installed|"*) printf '%s' "${s#*|}" ;; esac
}
missing_pkgs() {
  local p out=""
  for p in "$@"; do [ -n "$(pkg_ver "$p")" ] || out="$out $p"; done
  printf '%s' "${out# }"
}
py_ok() { "$PY" -c "$1" >/dev/null 2>&1; }
cv2_version() { "$PY" -c 'import cv2; print(cv2.__version__)' 2>/dev/null || true; }
ncpu() { nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2; }
free_gb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { printf "%d", $4 / 1048576 }' || true; }
trt_version() {
  local p v
  for p in libnvinfer10 libnvinfer9 libnvinfer8 libnvinfer7 libnvinfer6 libnvinfer5; do
    v="$(pkg_ver "$p")"
    if [ -n "$v" ]; then printf '%s %s' "$p" "$v"; return 0; fi
  done
}

# ---- detect + refuse -------------------------------------------------------------------------------------------
edge_detect
if [ "$YES" = 1 ]; then
  say "LOCKI AIRLOCK edge installer: APPLY (--yes)"
else
  say "LOCKI AIRLOCK edge installer: DRY RUN (prints the plan, changes nothing; add --yes to apply)"
fi
say "Detected on this device:"
edge_detect_print | sed 's/^/  /'

[ "$PLATFORM" != macos ] || refuse "this is macOS. Run this ON the Jetson; from the Mac use: bash scripts/edge_deploy.sh <user>@<host>"
[ -r "$(edge_path /etc/nv_tegra_release)" ] || refuse "/etc/nv_tegra_release is missing: not an NVIDIA L4T (Jetson) system (PLATFORM=$PLATFORM)"
[ "$ARCH" = aarch64 ] || refuse "CPU architecture is '$ARCH'; every Jetson is aarch64"
[ "$PLATFORM" = jetson ] || refuse "platform is '$PLATFORM' (kernel '$KERNEL'), not a Jetson"
case "$JETPACK_FAMILY" in
  4|5|6) ;;
  *) refuse "L4T R$L4T_MAJOR (REVISION $L4T_MINOR) is not R32/R35/R36, so the JetPack family is unknown. Not guessing." ;;
esac
[ "$(id -u)" != 0 ] || refuse "running as root. Run as your normal user; the script calls sudo for each privileged step."
USER_NAME="$(id -un)"

EXP_UBUNTU="$(edge_expected_ubuntu "$JETPACK_FAMILY")"
EXP_PY="$(edge_expected_python "$JETPACK_FAMILY")"
OS_MATCH=1
say ""
say "Profile: JetPack $JETPACK_FAMILY.x (L4T R$L4T_MAJOR.$L4T_MINOR). NVIDIA ships it with Ubuntu $EXP_UBUNTU + Python $EXP_PY."
if [ "$UBUNTU" != "$EXP_UBUNTU" ]; then
  OS_MATCH=0
  say "WARNING: this image runs Ubuntu $UBUNTU, not $EXP_UBUNTU (non-standard image). apt below only uses this release's own archive; --with-jetpack-meta is refused."
fi
if [ "$PYTHON3" != "$EXP_PY" ]; then
  say "WARNING: python3 here is $PYTHON3, not $EXP_PY. Nothing below installs or switches Python; the service runs /usr/bin/python3."
fi
if [ "$JETPACK_FAMILY" = 4 ]; then
  say "JetPack 4.x rules: system Python $PYTHON3 stays, no venv, the JetPack-shipped cv2 is used as-is."
else
  say "JetPack $JETPACK_FAMILY.x rules: system Python + JetPack cv2; a venv is optional (--venv) and always --system-site-packages."
fi

# ---- steps (each runs twice: MODE=plan for the printed plan, MODE=apply with --yes) ------------------------------
step_base() {
  local miss
  step "apt packages: $BASE_PKGS (Ubuntu $UBUNTU archive)"
  # shellcheck disable=SC2086  # the package list is split on purpose
  miss="$(missing_pkgs $BASE_PKGS)"
  if [ -z "$miss" ]; then note "all installed: skip"; return 0; fi
  note "missing: $miss"
  # shellcheck disable=SC2086
  apt_install $miss
}

step_jetpack_meta() {
  local cur cand v major
  if [ "$WITH_JETPACK_META" != 1 ]; then
    step "nvidia-jetpack meta-package: not requested (add --with-jetpack-meta; never installed by default)"
    return 0
  fi
  step "nvidia-jetpack meta-package for JetPack $JETPACK_FAMILY.x (CUDA, cuDNN, TensorRT, OpenCV: GBs, 20+ min)"
  cur="$(pkg_ver nvidia-jetpack)"
  if [ -n "$cur" ]; then note "installed: nvidia-jetpack $cur: skip"; return 0; fi
  [ "$OS_MATCH" = 1 ] || refuse "--with-jetpack-meta on a non-standard image (Ubuntu $UBUNTU with L4T R$L4T_MAJOR)"
  cand="$(apt-cache policy nvidia-jetpack 2>/dev/null | sed -n 's/^[[:space:]]*Candidate:[[:space:]]*//p' | head -n 1 || true)"
  case "$cand" in
    ""|"(none)") refuse "apt has no candidate for nvidia-jetpack (NVIDIA's L4T R$L4T_MAJOR apt source is missing, or run 'sudo apt-get update' first). Not touching it." ;;
  esac
  v="${cand#*:}"
  major="${v%%[.+-]*}"
  [ "$major" = "$JETPACK_FAMILY" ] || refuse "apt would install nvidia-jetpack $cand (JetPack $major) on a JetPack $JETPACK_FAMILY.x device (L4T R$L4T_MAJOR). Refusing a mismatched JetPack."
  note "apt candidate nvidia-jetpack $cand matches JetPack $JETPACK_FAMILY.x"
  apt_install nvidia-jetpack
}

step_opencv() {
  local ver cand
  step "OpenCV for Python $PYTHON3: use the JetPack-shipped cv2; if missing, NVIDIA's libopencv-python before Ubuntu's"
  ver="$(cv2_version)"
  if [ -n "$ver" ]; then note "cv2 $ver is importable by $PY: skip (left untouched)"; return 0; fi
  note "cv2 is NOT importable by $PY"
  if [ "$WITH_JETPACK_META" = 1 ] && [ "$MODE" = plan ]; then note "(re-checked at apply time: nvidia-jetpack normally brings JetPack's own cv2)"; fi
  # NVIDIA's L4T apt repo ships its OpenCV build (GStreamer-enabled) as libopencv-python; Ubuntu's python3-opencv
  # is a different, older build (3.2 on 18.04) that can shadow it. Prefer NVIDIA's whenever apt offers it.
  cand="$(apt-cache policy libopencv-python 2>/dev/null | sed -n 's/^[[:space:]]*Candidate:[[:space:]]*//p' | head -n 1 || true)"
  case "$cand" in
    ""|"(none)")
      note "NVIDIA libopencv-python has no apt candidate on this image: falling back to Ubuntu's python3-opencv"
      if [ "$JETPACK_FAMILY" = 4 ]; then note "note: Ubuntu 18.04's python3-opencv is OpenCV 3.2, older than the 4.1.1 that JetPack 4.6 images ship"; fi
      apt_install python3-opencv ;;
    *)
      note "NVIDIA libopencv-python $cand (JetPack's OpenCV build) is available: installing it, not Ubuntu's python3-opencv"
      apt_install libopencv-python ;;
  esac
  if [ "$MODE" = apply ]; then
    ver="$(cv2_version)"
    if [ -n "$ver" ]; then note "cv2 $ver now importable"; else fail "cv2 still not importable after the OpenCV install"; fi
  fi
}

step_groups() {
  local have_groups need="" g
  step "groups video + audio for $USER_NAME (camera + speaker access)"
  have_groups=" $(id -nG "$USER_NAME" 2>/dev/null || true) "
  for g in video audio; do
    case "$have_groups" in *" $g "*) ;; *) need="$need${need:+,}$g" ;; esac
  done
  if [ -z "$need" ]; then note "already in video + audio: skip"; return 0; fi
  act sudo usermod -aG "$need" "$USER_NAME"
  note "takes effect at your next login; the systemd unit sets SupplementaryGroups=video audio, so the service does not wait for that"
}

step_venv() {
  if [ "$WANT_VENV" != 1 ]; then
    step "Python venv: not requested (system Python is used; --venv is an option on JetPack 5/6 only)"
    return 0
  fi
  if [ "$JETPACK_FAMILY" = 4 ]; then
    step "Python venv: skipped on JetPack 4.x (system Python $PYTHON3 and the JetPack cv2 are used directly)"
    return 0
  fi
  step "venv at edge/jetson/.venv with --system-site-packages (keeps the JetPack cv2 visible; nothing is pip-installed)"
  if [ -x "$VENV_DIR/bin/python" ]; then
    note "exists: skip creating"
    case "$(cat "$VENV_DIR/pyvenv.cfg" 2>/dev/null || true)" in
      *"include-system-site-packages = true"*) ;;
      *) refuse "$VENV_DIR exists WITHOUT --system-site-packages, so the JetPack cv2 is invisible in it. Remove it (rm -rf $VENV_DIR) and re-run." ;;
    esac
  else
    [ -z "$(missing_pkgs python3-venv)" ] || apt_install python3-venv
    act "$PY" -m venv --system-site-packages "$VENV_DIR"
  fi
  if [ "$MODE" = apply ]; then
    if "$VENV_DIR/bin/python" -c 'import cv2' >/dev/null 2>&1; then note "venv python imports cv2: OK"; else fail "venv python cannot import cv2"; fi
  fi
}

step_detectnet() {
  local miss trt gb
  if [ "$WITH_DETECTNET" != 1 ]; then
    step "jetson-inference (detectNet): not requested (add --with-detectnet; source build, often 30-60+ min on a Nano)"
    return 0
  fi
  step "jetson-inference (detectNet) from source: official dusty-nv steps"
  note "TIME WARNING: this build often takes 30-60+ minutes on a Jetson Nano (longer on a slow SD card)."
  note "cmake can download models and stop to ask questions: keep this terminal attended."
  if py_ok 'import jetson_inference' || py_ok 'import jetson.inference'; then note "jetson_inference already importable: skip"; return 0; fi
  trt="$(trt_version)"
  if [ -n "$trt" ]; then note "TensorRT found: $trt"; else note "WARNING: no TensorRT (libnvinfer) package found; the build needs JetPack's TensorRT (see --with-jetpack-meta)"; fi
  gb="$(free_gb "$HOME")"
  if [ -n "$gb" ] && [ "$gb" -lt 3 ]; then note "WARNING: only ${gb} GB free under $HOME; the build plus models want about 3 GB"; fi
  # shellcheck disable=SC2086  # the package list is split on purpose
  miss="$(missing_pkgs $DETECTNET_PKGS)"
  # shellcheck disable=SC2086
  [ -z "$miss" ] || apt_install $miss
  if [ -d "$JI_DIR/.git" ]; then
    note "$JI_DIR already cloned: skip clone, finish any interrupted submodule checkout"
    act git -C "$JI_DIR" submodule update --init --recursive
  else
    act git clone --recursive --depth=1 "$JI_URL" "$JI_DIR"
  fi
  act mkdir -p "$JI_DIR/build"
  act_in "$JI_DIR/build" cmake ../
  act_in "$JI_DIR/build" make -j"$(ncpu)"
  act_in "$JI_DIR/build" sudo make install
  act sudo ldconfig
  if [ "$MODE" = apply ]; then
    if py_ok 'import jetson_inference' || py_ok 'import jetson.inference'; then note "jetson_inference now importable"; else fail "jetson_inference still not importable after the build"; fi
  fi
  note "the first detectNet run builds a TensorRT engine (minutes, once). Warm it up before any demo:"
  note "  $PY -c \"from jetson_inference import detectNet; detectNet('ssd-mobilenet-v2', threshold=0.5)\""
}

step_verify() {
  local out rc=0
  step "verify: unit tests with $PY + hardware inventory"
  if [ "$MODE" != apply ]; then
    note "would run: $PY -m unittest discover -s edge/jetson/tests   # from the repo root"
    note "would run: bash scripts/edge_jetson_inventory.sh   # writes hardware/jetson_inventory.md"
    return 0
  fi
  # From the repo root: edge/jetson/tests is shared with Sid's adapter tests, which import edge.jetson.*
  note "\$ $PY -m unittest discover -s edge/jetson/tests"
  out="$(cd "$REPO" && "$PY" -m unittest discover -s edge/jetson/tests 2>&1)" || rc=$?
  printf '%s\n' "$out" | tail -n 4 | sed 's/^/      /'
  [ "$rc" = 0 ] || fail "unit tests failed (exit $rc). Details: cd $REPO && $PY -m unittest discover -s edge/jetson/tests -v"
  note "\$ bash scripts/edge_jetson_inventory.sh"
  bash "$SCRIPT_DIR/edge_jetson_inventory.sh" | sed 's/^/      /' || fail "inventory script failed"
}

run_steps() {
  N=0
  APT_UPDATED=0
  step_base
  step_jetpack_meta
  step_opencv
  step_groups
  step_venv
  step_detectnet
  step_verify
}

say ""
say "PLAN"
MODE=plan
run_steps
say ""
say "Never run by this script: system or release upgrades of any kind, firmware/storage flashing, pip into the system Python."
if [ "$YES" != 1 ]; then
  say "DRY RUN: nothing changed. Apply with: bash scripts/edge_jetson_install.sh --yes$FLAGS"
  exit 0
fi

say ""
say "APPLYING (each step re-checks before it acts)"
MODE=apply
run_steps
say ""
if [ -n "$FAILED" ]; then
  say "FINISHED WITH FAILURES:$FAILED"
  exit 1
fi
say "DONE. Next (docs/airlock/edge/DEPLOY.md):"
say "  1. ls -l /dev/v4l/by-id/    -> camera path for AIRLOCK_CAMERA_SOURCE"
say "  2. mkdir -p -m 700 ~/.config/locki-edge && install -m 600 edge/jetson/airlock.env.example ~/.config/locki-edge/airlock.env   (then edit it)"
say "  3. bash scripts/edge_service.sh install --yes"
