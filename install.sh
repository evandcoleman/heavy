#!/bin/sh
# Install heavy, heavy-top, heavy-run and heavyq.py into $PREFIX/bin (default ~/.local/bin).
#
# One line:   curl -fsSL https://raw.githubusercontent.com/evandcoleman/heavy/main/install.sh | sh
# From a clone:
#   ./install.sh            copy the scripts
#   ./install.sh --link     symlink them instead (tracks this checkout)
#
# Env: PREFIX (default ~/.local), HEAVY_REF (git ref to download, default main).
set -eu

REPO="evandcoleman/heavy"
FILES="heavy heavy-top heavy-run heavyq.py"

main() {
  prefix="${PREFIX:-$HOME/.local}"
  bin="$prefix/bin"
  mode="${1:-}"
  case "$mode" in
    "" | --link) ;;
    *) echo "usage: install.sh [--link]" >&2; exit 2 ;;
  esac

  command -v python3 >/dev/null 2>&1 || {
    echo "heavy needs python3 (3.10+)" >&2
    exit 1
  }

  # Run from a checkout? Use its bin/. Piped from curl? Download into a temp dir.
  # ($0 is "sh" when piped, so only trust it when it names this script.)
  here=""
  case "$(basename -- "$0")" in
    install.sh) here="$(cd "$(dirname -- "$0")" && pwd)" ;;
  esac
  if [ -n "$here" ] && [ -f "$here/bin/heavy" ] && [ -f "$here/bin/heavy-top" ] &&
    [ -f "$here/bin/heavy-run" ] && [ -f "$here/bin/heavyq.py" ]; then
    src="$here/bin"
  else
    [ "$mode" = "--link" ] && { echo "--link needs a git checkout" >&2; exit 2; }
    command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    base="https://raw.githubusercontent.com/$REPO/${HEAVY_REF:-main}/bin"
    for f in $FILES; do
      curl -fsSL "$base/$f" -o "$tmp/$f"
    done
    src="$tmp"
  fi

  mkdir -p "$bin"
  for f in $FILES; do
    rm -f "$bin/$f" # also clears a previous --link symlink, which cp would follow
    if [ "$mode" = "--link" ]; then
      ln -s "$src/$f" "$bin/$f"
    else
      cp "$src/$f" "$bin/$f"
      case "$f" in *.py) chmod 644 "$bin/$f" ;; *) chmod 755 "$bin/$f" ;; esac
    fi
  done
  rm -f "$bin"/__pycache__/heavyq.*.pyc # stale bytecode from an older install
  echo "installed heavy, heavy-top, heavy-run, heavyq.py -> $bin"
  case ":$PATH:" in *":$bin:"*) ;; *) echo "add $bin to your PATH" ;; esac
}

# Everything runs from main, so a truncated download can't run a partial script.
main "$@"
