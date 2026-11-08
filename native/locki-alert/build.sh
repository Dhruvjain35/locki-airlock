#!/usr/bin/env bash
# Builds LOCKI Alert (release), assembles "LOCKI Alert.app", ad-hoc signs it and installs it to ~/.locki/alert/.
# Idempotent: re-running replaces the bundle in place. Build products live outside the repo (~/.locki/alert/.build).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="${LOCKI_ALERT_DEST:-$HOME/.locki/alert}"
SCRATCH="$DEST/.build"
APP="$DEST/LOCKI Alert.app"
STAGE="$DEST/.stage"
FONT="$REPO/frontend/node_modules/@fontsource-variable/archivo/files/archivo-latin-wght-normal.woff2"

mkdir -p "$DEST"
echo "==> swift build -c release"
swift build -c release --package-path "$HERE" --scratch-path "$SCRATCH"
BIN="$(swift build -c release --package-path "$HERE" --scratch-path "$SCRATCH" --show-bin-path)"

echo "==> assemble bundle"
rm -rf "$STAGE"
mkdir -p "$STAGE/LOCKI Alert.app/Contents/MacOS" "$STAGE/LOCKI Alert.app/Contents/Resources"
cp "$BIN/locki-alert" "$STAGE/LOCKI Alert.app/Contents/MacOS/locki-alert"
cp "$HERE/Info.plist" "$STAGE/LOCKI Alert.app/Contents/Info.plist"
plutil -lint "$STAGE/LOCKI Alert.app/Contents/Info.plist" >/dev/null
if [[ -f "$FONT" ]]; then
  cp "$FONT" "$STAGE/LOCKI Alert.app/Contents/Resources/"
else
  echo "    note: Archivo not found at $FONT; the app falls back to SF Pro"
fi

echo "==> ad-hoc codesign"
codesign --force --sign - --timestamp=none "$STAGE/LOCKI Alert.app"
codesign --verify --strict "$STAGE/LOCKI Alert.app"

rm -rf "$APP"
mv "$STAGE/LOCKI Alert.app" "$APP"
rmdir "$STAGE"
cp "$BIN/locki-alert-selftest" "$DEST/locki-alert-selftest"
echo "==> installed: $APP"
echo "    binary:    $APP/Contents/MacOS/locki-alert"
echo "    selftest:  $DEST/locki-alert-selftest"
