#!/bin/sh
# Export a clean source snapshot of this package for services/nemotron-investigator/ in the
# LOCKI AIRLOCK monorepo (fresh history: the standalone git history is NOT exported).
#
#   * source = the COMMITTED tree (git archive HEAD), so untracked or ignored files
#     (.venv, .secrets, var/, caches, API keys, tokens, live evidence) cannot leak;
#   * removes the paths listed in scripts/snapshot_exclude.txt;
#   * runs scripts/audit_snapshot.py (real secret values, credential patterns, weights,
#     live identifiers); the export fails if the audit fails.
#
# Usage: scripts/export_snapshot.sh [OUT_DIR]      (default: ~/locki-airlock-export)
# Output: OUT_DIR/services/nemotron-investigator/, OUT_DIR/AUDIT.md, OUT_DIR/MANIFEST.sha256,
#         OUT_DIR/nemotron-investigator-snapshot.tgz
set -eu
PKG=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(git -C "$PKG" rev-parse --show-toplevel)
PREFIX=$(git -C "$PKG" rev-parse --show-prefix)
OUT=${1:-"$HOME/locki-airlock-export"}
case "$OUT" in "$REPO"|"$REPO"/*) echo "refusing to export inside the source repository" >&2; exit 2 ;; esac
if [ -n "$(git -C "$REPO" status --porcelain -- "$PREFIX")" ]; then
  echo "uncommitted or untracked changes under $PREFIX; commit first (the snapshot is the committed tree)" >&2
  exit 3
fi
COMMIT=$(git -C "$REPO" rev-parse HEAD)
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/ni-snapshot.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
git -C "$REPO" archive --format=tar HEAD "$PREFIX" | tar -x -C "$STAGE"
SRC="$STAGE/${PREFIX%/}"
while IFS= read -r pat || [ -n "$pat" ]; do
  case "$pat" in '' | '#'*) continue ;; esac
  for f in "$SRC"/$pat; do
    [ -e "$f" ] && rm -rf "$f"
  done
done < "$SRC/scripts/snapshot_exclude.txt"
PY="$PKG/.venv/bin/python"
FORBID="$PKG/var/live/forbidden_identifiers.txt"
set +e
"$PY" -I "$PKG/scripts/audit_snapshot.py" "$SRC" --secrets-dir "$PKG/.secrets" --forbid-file "$FORBID" \
  --report "$STAGE/AUDIT.md" --manifest "$STAGE/MANIFEST.sha256" --source-commit "$COMMIT" > /dev/null
rc=$?
set -e
mkdir -p "$OUT"
if [ "$rc" -ne 0 ]; then
  # Never put a failed tree at the deliverable path, and leave any earlier clean export untouched.
  Q="$OUT/AUDIT_FAILED-$COMMIT"
  rm -rf "$Q"
  mkdir -p "$Q"
  mv "$SRC" "$Q/nemotron-investigator"
  mv "$STAGE/AUDIT.md" "$STAGE/MANIFEST.sha256" "$Q/"
  echo "AUDIT FAILED: quarantined in $Q (see $Q/AUDIT.md); the deliverable path was not touched" >&2
  exit 1
fi
mkdir -p "$OUT/services"
DEST="$OUT/services/nemotron-investigator"
rm -rf "$DEST"
mv "$SRC" "$DEST"
mv "$STAGE/AUDIT.md" "$STAGE/MANIFEST.sha256" "$OUT/"
tar -czf "$OUT/nemotron-investigator-snapshot.tgz" -C "$OUT" services/nemotron-investigator
echo "snapshot: $DEST"
echo "audit:    $OUT/AUDIT.md (PASS)"
echo "manifest: $OUT/MANIFEST.sha256"
echo "tarball:  $OUT/nemotron-investigator-snapshot.tgz  sha256 $(shasum -a 256 "$OUT/nemotron-investigator-snapshot.tgz" | cut -d' ' -f1)"
echo "source:   standalone commit $COMMIT (history not exported)"
