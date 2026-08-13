#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="${KANNON_VENV:-${ROOT_DIR}/.venv}"
BIN_DIR="${KANNON_BIN_DIR:-${HOME}/.local/bin}"
CHECK_ONLY=0

case "${1:-}" in
    --check)
        CHECK_ONLY=1
        ;;
    -h|--help)
        echo "Usage: ./install.sh [--check]"
        echo "Creates a local virtual environment and installs Kannon without touching system Python."
        exit 0
        ;;
    "")
        ;;
    *)
        echo "error: unknown option: $1" >&2
        echo "Usage: ./install.sh [--check]" >&2
        exit 2
        ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required but was not found on PATH." >&2
    echo "Install Python 3.10 or newer with your system package manager, then rerun ./install.sh." >&2
    exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "Kannon installer check"
    echo "  root: $ROOT_DIR"
    echo "  venv: $VENV_DIR"
    echo "  command link: $BIN_DIR/kannon"
    echo "  python3: $(command -v python3)"
    echo "Installer check finished. Run ./install.sh to install."
    exit 0
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment: $VENV_DIR"
    python3 -m venv "$VENV_DIR" || {
        echo "error: could not create a Python virtual environment." >&2
        echo "On Debian/Kali/Ubuntu, install venv support with:" >&2
        echo "  sudo apt install python3-venv python3-full" >&2
        exit 1
    }
fi

echo "Installing Kannon Python dependencies into: $VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[pdf]"

mkdir -p "$BIN_DIR"
ln -sf "$ROOT_DIR/kannon" "$BIN_DIR/kannon"

echo
echo "Installed Kannon command: $BIN_DIR/kannon"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "Note: $BIN_DIR is not on PATH."
        echo "Add this to your shell profile:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo
echo "Optional system tools:"
if command -v chafa >/dev/null 2>&1; then
    echo "  chafa: found at $(command -v chafa)"
else
    echo "  chafa: missing. Install for better terminal graphics:"
    echo "    sudo apt install chafa"
fi

if command -v pdftoppm >/dev/null 2>&1; then
    echo "  pdftoppm: found at $(command -v pdftoppm)"
else
    echo "  pdftoppm: missing. Install Poppler fallback PDF tools:"
    echo "    sudo apt install poppler-utils"
fi

if command -v xdg-open >/dev/null 2>&1; then
    echo "  xdg-open: found at $(command -v xdg-open)"
else
    echo "  xdg-open: missing. Install desktop open support:"
    echo "    sudo apt install xdg-utils"
fi

echo
echo "Try:"
echo "  kannon ~/Documents"
