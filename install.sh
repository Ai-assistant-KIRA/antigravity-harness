#!/usr/bin/env sh
# Harness² bootstrap installer for macOS and Linux.
#
# Works two ways:
#   1. From a checkout:  sh install.sh [installer args]
#   2. From the web:     curl -fsSL https://raw.githubusercontent.com/Ai-assistant-KIRA/antigravity-harness/main/install.sh | sh
#
# It finds Python 3.10+, downloads the source when it is not run from a
# checkout, and runs install_plugin.py (which backs up any existing install,
# keeps memory, rules, and user skills, smoke-tests the hooks, and runs
# `harness doctor`).
#
# The last line is machine-readable for agents:
#   HARNESS_INSTALL: ok version=<v> target=<path>
#   HARNESS_INSTALL: failed reason=<text>
#
# Environment overrides:
#   HARNESS_PYTHON      interpreter to use
#   HARNESS_SOURCE_URL  tar.gz archive to install from (default: main branch)
set -eu

REPO_TARBALL="https://github.com/Ai-assistant-KIRA/antigravity-harness/archive/refs/heads/main.tar.gz"

say() { printf '[harness] %s\n' "$*"; }
fail() {
    printf '[harness] ERROR: %s\n' "$*" >&2
    printf 'HARNESS_INSTALL: failed reason=%s\n' "$*"
    exit 1
}

python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
    for candidate in "${HARNESS_PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3 python; do
        [ -n "$candidate" ] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        if python_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python) || fail "Python 3.10 or newer was not found. Install it (macOS: 'brew install python@3.12'; Debian/Ubuntu: 'sudo apt install python3') and run this installer again."
say "using $("$PYTHON" -c 'import sys; print("Python %d.%d at %s" % (sys.version_info[0], sys.version_info[1], sys.executable))')"

# A script piped into sh has no file of its own; $0 is the shell.
SCRIPT_DIR=""
case "$0" in
    */install.sh|install.sh) SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) ;;
esac

CLEANUP=""
cleanup() {
    if [ -n "$CLEANUP" ]; then rm -rf "$CLEANUP"; fi
}
trap cleanup EXIT INT TERM

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/install_plugin.py" ]; then
    SOURCE="$SCRIPT_DIR"
    say "installing from checkout $SOURCE"
else
    URL="${HARNESS_SOURCE_URL:-$REPO_TARBALL}"
    CLEANUP=$(mktemp -d 2>/dev/null || mktemp -d -t harness-install)
    say "downloading $URL"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$CLEANUP/source.tar.gz" || fail "download failed: $URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$URL" -O "$CLEANUP/source.tar.gz" || fail "download failed: $URL"
    else
        fail "neither curl nor wget is available to download $URL"
    fi
    tar -xzf "$CLEANUP/source.tar.gz" -C "$CLEANUP" || fail "could not extract the source archive"
    SOURCE=$(dirname "$(find "$CLEANUP" -maxdepth 3 -name install_plugin.py | head -n 1)")
    [ -f "$SOURCE/install_plugin.py" ] || fail "the downloaded archive does not contain install_plugin.py"
fi

say "running install_plugin.py $*"
"$PYTHON" -X utf8 "$SOURCE/install_plugin.py" "$@" || fail "install_plugin.py failed (see the output above)"
