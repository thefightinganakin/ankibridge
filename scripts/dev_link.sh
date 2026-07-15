#!/usr/bin/env bash
# Symlink the add-on into Anki's addons folder for local development (macOS).
# After running, fully quit and reopen Anki.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../src/ankibridge" && pwd)"
ADDONS_DIR="$HOME/Library/Application Support/Anki2/addons21"
DEST="$ADDONS_DIR/ankibridge"

if [ ! -d "$ADDONS_DIR" ]; then
  echo "Anki addons folder not found at: $ADDONS_DIR"
  echo "Open Anki at least once, then re-run."
  exit 1
fi

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "Removing existing: $DEST"
  rm -rf "$DEST"
fi

ln -s "$SRC" "$DEST"
echo "Linked:"
echo "  $DEST -> $SRC"
echo "Now quit and reopen Anki, then Tools → AnkiBridge."
