#!/bin/bash
# install.sh — wire ~/Development/cerebro/bin into PATH (idempotent).
set -euo pipefail

CEREBRO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$CEREBRO_DIR/bin"
SHELL_RC="${ZDOTDIR:-$HOME}/.zshrc"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""

# Sanity checks
for tool in python3 lsof git ps; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[cerebro] WARNING: $tool not found in PATH — cerebro will be degraded." >&2
  fi
done

chmod +x "$BIN_DIR/cerebro"

if [ ! -f "$SHELL_RC" ]; then
  echo "[cerebro] $SHELL_RC does not exist — creating it." >&2
  : > "$SHELL_RC"
fi

if grep -Fq "$BIN_DIR" "$SHELL_RC"; then
  echo "[cerebro] PATH already wired in $SHELL_RC — nothing to do."
else
  printf '\n# cerebro\n%s\n' "$PATH_LINE" >> "$SHELL_RC"
  echo "[cerebro] Appended PATH line to $SHELL_RC."
fi

echo
echo "Done. Open a new shell (or 'exec zsh'), then run 'cerebro'."
