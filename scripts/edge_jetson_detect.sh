#!/usr/bin/env bash
# shellcheck disable=SC2034  # this file sets variables for the scripts that source it
#
# LOCKI AIRLOCK edge: what is this machine? NO side effects (reads a few files, runs uname + python3 -c).
#
#   CLI:      bash scripts/edge_jetson_detect.sh                       # prints KEY=VALUE lines
#   Library:  . scripts/edge_jetson_detect.sh && edge_detect && echo "$JETPACK_FAMILY"
#
# Keys (always set; "unknown" when a fact cannot be read):
#   ARCH            uname -m (every Jetson is aarch64)
#   BOARD_MODEL     /proc/device-tree/model, trailing NUL stripped
#   L4T_MAJOR       32 from "# R32 (release), REVISION: 7.4, ..." (first line of /etc/nv_tegra_release)
#   L4T_MINOR       7.4 from the same line
#   JETPACK_FAMILY  4 | 5 | 6 | unknown, from L4T ONLY: R32 -> 4, R35 -> 5, R36 -> 6, anything else -> unknown
#   UBUNTU          VERSION_ID from /etc/os-release when ID=ubuntu (18.04 / 20.04 / 22.04)
#   PYTHON3         major.minor of $EDGE_PYTHON3 (default: python3)
#   PLATFORM        macos | jetson (Linux + parseable nv_tegra_release) | not_jetson
# Extra variables for sourcing scripts: KERNEL (uname -s), KERNEL_RELEASE (uname -r), L4T_LINE, OS_PRETTY.
#
# Overrides (tests, or inspecting a copied rootfs). Defaults are the real machine:
#   EDGE_ROOT=""           prefix for EVERY file read, e.g. a fixture dir holding etc/nv_tegra_release
#   EDGE_UNAME=uname       command run as "$EDGE_UNAME -s|-m|-r"
#   EDGE_PYTHON3=python3   interpreter whose version is reported
# Safe to source from scripts running under "set -euo pipefail"; sourcing changes no shell options.

edge_path() { printf '%s%s' "${EDGE_ROOT:-}" "$1"; }
edge_uname() { "${EDGE_UNAME:-uname}" "$@" 2>/dev/null; }
edge_py() { "${EDGE_PYTHON3:-python3}" "$@"; }

# First line of a file, NUL/CR removed, surrounding blanks trimmed. Empty if the file is unreadable.
edge_first_line() {
  [ -r "$1" ] || return 0
  LC_ALL=C tr -d '\000\r' < "$1" 2>/dev/null | sed -n '1{s/^[[:space:]]*//;s/[[:space:]]*$//;p;}' 2>/dev/null || true
}

# Value of KEY in an os-release style file, quotes removed. The file is parsed, never sourced.
edge_os_field() {
  sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" 2>/dev/null || true
}

# What NVIDIA ships with each JetPack family (used for mismatch warnings, never for installing).
edge_expected_ubuntu() { case "$1" in 4) echo 18.04 ;; 5) echo 20.04 ;; 6) echo 22.04 ;; *) echo unknown ;; esac; }
edge_expected_python() { case "$1" in 4) echo 3.6 ;; 5) echo 3.8 ;; 6) echo 3.10 ;; *) echo unknown ;; esac; }

edge_detect() {
  local osr id ver
  KERNEL="$(edge_uname -s || true)"; KERNEL="${KERNEL%%[[:space:]]*}"
  ARCH="$(edge_uname -m || true)"; ARCH="${ARCH%%[[:space:]]*}"
  KERNEL_RELEASE="$(edge_uname -r || true)"; KERNEL_RELEASE="${KERNEL_RELEASE%%[[:space:]]*}"
  [ -n "$KERNEL" ] || KERNEL=unknown
  [ -n "$ARCH" ] || ARCH=unknown
  [ -n "$KERNEL_RELEASE" ] || KERNEL_RELEASE=unknown

  BOARD_MODEL="$(edge_first_line "$(edge_path /proc/device-tree/model)")"
  [ -n "$BOARD_MODEL" ] || BOARD_MODEL=unknown

  L4T_LINE="$(edge_first_line "$(edge_path /etc/nv_tegra_release)")"
  L4T_MAJOR="$(printf '%s\n' "$L4T_LINE" | sed -n 's/^#[[:space:]]*R\([0-9][0-9]*\).*/\1/p' || true)"
  L4T_MINOR="$(printf '%s\n' "$L4T_LINE" | sed -n 's/.*REVISION:[[:space:]]*\([0-9][0-9.]*\).*/\1/p' || true)"
  L4T_MINOR="${L4T_MINOR%.}"
  [ -n "$L4T_MAJOR" ] || L4T_MAJOR=unknown
  [ -n "$L4T_MINOR" ] || L4T_MINOR=unknown
  case "$L4T_MAJOR" in
    32) JETPACK_FAMILY=4 ;;
    35) JETPACK_FAMILY=5 ;;
    36) JETPACK_FAMILY=6 ;;
    *) JETPACK_FAMILY=unknown ;;
  esac

  UBUNTU=unknown
  OS_PRETTY=unknown
  osr="$(edge_path /etc/os-release)"
  if [ -r "$osr" ]; then
    id="$(edge_os_field ID "$osr")"
    ver="$(edge_os_field VERSION_ID "$osr")"
    OS_PRETTY="$(edge_os_field PRETTY_NAME "$osr")"
    [ -n "$OS_PRETTY" ] || OS_PRETTY=unknown
    if [ "$id" = ubuntu ] && [ -n "$ver" ]; then UBUNTU="$ver"; fi
  fi

  PYTHON3="$(edge_py -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  PYTHON3="${PYTHON3%%[[:space:]]*}"
  case "$PYTHON3" in [0-9]*.[0-9]*) ;; *) PYTHON3=unknown ;; esac

  if [ "$KERNEL" = Darwin ]; then
    PLATFORM=macos
  elif [ "$KERNEL" = Linux ] && [ "$L4T_MAJOR" != unknown ]; then
    PLATFORM=jetson
  else
    PLATFORM=not_jetson
  fi
}

edge_detect_print() {
  printf 'ARCH=%s\nBOARD_MODEL=%s\nL4T_MAJOR=%s\nL4T_MINOR=%s\nJETPACK_FAMILY=%s\nUBUNTU=%s\nPYTHON3=%s\nPLATFORM=%s\n' \
    "$ARCH" "$BOARD_MODEL" "$L4T_MAJOR" "$L4T_MINOR" "$JETPACK_FAMILY" "$UBUNTU" "$PYTHON3" "$PLATFORM"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -u
  case "${1:-}" in
    "") ;;
    -h|--help) sed -n '4,24p' "$0"; exit 0 ;;
    *) echo "usage: bash scripts/edge_jetson_detect.sh [--help]" >&2; exit 2 ;;
  esac
  edge_detect
  edge_detect_print
fi
