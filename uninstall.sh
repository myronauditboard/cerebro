#!/bin/bash
# uninstall.sh — remove the cerebro PATH line from ~/.zshrc.
set -euo pipefail

CEREBRO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$CEREBRO_DIR/bin"
SHELL_RC="${ZDOTDIR:-$HOME}/.zshrc"

if [ ! -f "$SHELL_RC" ]; then
  echo "[cerebro] $SHELL_RC does not exist — nothing to uninstall."
  exit 0
fi

# Strip the PATH line and the preceding "# cerebro" comment if present.
tmp="$(mktemp)"
awk -v bin="$BIN_DIR" '
  $0 == "# cerebro" { skip_next = 1; next }
  skip_next && index($0, bin) > 0 { skip_next = 0; next }
  { skip_next = 0; print }
' "$SHELL_RC" > "$tmp"
mv "$tmp" "$SHELL_RC"

echo "[cerebro] Removed PATH line from $SHELL_RC. Open a new shell to apply."
