#!/bin/sh
# Install heavy and heavy-top into $PREFIX/bin (default ~/.local/bin).
# Usage: ./install.sh            copy the scripts
#        ./install.sh --link     symlink them instead (tracks this checkout)
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
SRC="$(cd "$(dirname "$0")" && pwd)/bin"

command -v sem >/dev/null 2>&1 || {
  echo "heavy needs GNU parallel's \`sem\` on PATH." >&2
  echo "  macOS:  brew install parallel" >&2
  echo "  Debian: apt install parallel" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || echo "note: heavy-top needs python3 (3.10+); heavy itself does not." >&2

mkdir -p "$BIN"
for f in heavy heavy-top; do
  if [ "${1:-}" = "--link" ]; then
    ln -sf "$SRC/$f" "$BIN/$f"
  else
    cp "$SRC/$f" "$BIN/$f"
    chmod 755 "$BIN/$f"
  fi
done
echo "installed heavy, heavy-top -> $BIN"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to your PATH" ;; esac
