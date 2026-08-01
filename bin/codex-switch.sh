#!/usr/bin/env sh
# Locates a usable Python and hands off to bin/codex-accounts.py.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Candidates must be *probed*, not just found on PATH: on Windows `python3` is
# usually the Microsoft Store stub, which exists but is not an interpreter.
for py in "$CODEX_SWITCH_PYTHON" python3 python py; do
    [ -n "$py" ] || continue
    command -v "$py" >/dev/null 2>&1 || continue
    "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1 || continue
    exec "$py" "$DIR/codex-accounts.py" "$@"
done

echo "[xx] Python 3.7+ not found. Install it, or set CODEX_SWITCH_PYTHON=/path/to/python" >&2
exit 1
