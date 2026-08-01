#!/usr/bin/env sh
# One-line installer for codex-switch.
#
#   curl -fsSL https://raw.githubusercontent.com/hairbui76/cx-switch/main/install.sh | sh
#
# Needs curl (or wget), tar and Python 3.7+. Does not need git.
# Re-running it is also how you upgrade, though `cx update` is quicker once
# installed.
#
# Environment overrides:
#   CODEX_SWITCH_CHANNEL  main (every push, default) | stable (releases)
#   CODEX_SWITCH_HOME     install root
#   CODEX_SWITCH_BIN      directory the launcher goes into
#   CODEX_SWITCH_PYTHON   interpreter to use
#   CODEX_SWITCH_REPO     owner/name to install from
#   CODEX_SWITCH_NAME     what to call the command (default: cx)

set -e

REPO="${CODEX_SWITCH_REPO:-hairbui76/cx-switch}"
CHANNEL="${CODEX_SWITCH_CHANNEL:-main}"

die() {
    printf '[xx] %s\n' "$1" >&2
    exit 1
}

# --- interpreter ------------------------------------------------------------
# Candidates must be *probed*, not just found on PATH: on Windows `python3` is
# usually the Microsoft Store stub, which exists but is not an interpreter.
PY=""
for candidate in "$CODEX_SWITCH_PYTHON" python3 python py; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' \
        >/dev/null 2>&1 || continue
    PY="$candidate"
    break
done

[ -n "$PY" ] || die "Python 3.7+ not found. Install it, or set CODEX_SWITCH_PYTHON=/path/to/python"
printf '[ok] %s at %s\n' "$("$PY" --version 2>&1)" "$(command -v "$PY")"

# --- downloader -------------------------------------------------------------
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$1" "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO "$1" "$2"; }
else
    die "need curl or wget to download"
fi

command -v tar >/dev/null 2>&1 || die "need tar to unpack the download"

# --- bootstrap --------------------------------------------------------------
# Only a throwaway copy is fetched here, just enough to run `setup`. That copy
# then resolves the requested channel and installs the real, pinned build, so
# all the install logic lives in one place instead of being duplicated in
# shell, PowerShell and Python.
TMP=$(mktemp -d 2>/dev/null || mktemp -d -t codex-switch)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

printf '[--] fetching %s\n' "$REPO"
fetch "$TMP/bootstrap.tar.gz" "https://codeload.github.com/$REPO/tar.gz/main" \
    || die "download failed - check the repo name and your network"
tar -xzf "$TMP/bootstrap.tar.gz" -C "$TMP" || die "could not unpack the download"

BOOT=""
for dir in "$TMP"/*/; do
    if [ -f "$dir/bin/codex-accounts.py" ]; then
        BOOT="${dir%/}"
        break
    fi
done
[ -n "$BOOT" ] || die "the download does not look like $REPO"

"$PY" "$BOOT/bin/codex-accounts.py" setup --bootstrap --channel "$CHANNEL"
