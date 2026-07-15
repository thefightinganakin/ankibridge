#!/usr/bin/env bash
# Build a distributable .ankiaddon package (a zip of the add-on folder's
# CONTENTS, not the folder itself, per AnkiWeb requirements).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/src/ankibridge"
OUT="$ROOT/dist"
PKG="$OUT/ankibridge.ankiaddon"

mkdir -p "$OUT"
rm -f "$PKG"

cd "$SRC"
# Exclude caches, logs, and the meta file Anki generates locally.
zip -r "$PKG" . \
  -x '*.pyc' \
  -x '__pycache__/*' \
  -x 'user_files/ankibridge.log' \
  -x 'user_files/pairing_state.json' \
  -x 'user_files/pairing_state.json.tmp' \
  -x 'meta.json' \
  -x '.DS_Store'

echo "Built: $PKG"
