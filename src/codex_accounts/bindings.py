"""Which directory runs under which account.

Bindings live in one file in the store rather than inside each repository, so
nothing has to be added to - or excluded from - a project's git history. A
directory inherits the binding of its nearest bound parent, so binding a
workspace root covers every checkout under it.

A `.codex-account` file in a directory wins over the store, for a repository
that would rather carry the decision with it.
"""

import os

from .jsonio import read_json, write_json
from .paths import bindings_path
from .term import CliError

BINDINGS_VERSION = 1

MARKER_FILE = ".codex-account"

# Set by `cx run` in the environment it hands to Codex, so a shell opened
# inside a window - and anything it launches - stays on that window's account.
ENV_VAR = "CODEX_SWITCH_ACCOUNT"


def _normal(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _key(path: str) -> str:
    """Comparable form of a path: case-insensitive on Windows."""
    return os.path.normcase(os.path.normpath(_normal(path)))


def read_bindings():
    """{directory: account}, as written, in insertion order."""
    data = read_json(bindings_path())
    if not isinstance(data, dict):
        return {}
    entries = data.get("bindings")
    return dict(entries) if isinstance(entries, dict) else {}


def _save(entries) -> None:
    write_json(bindings_path(),
               {"version": BINDINGS_VERSION, "bindings": entries})


def bind(path: str, name: str) -> str:
    """Bind a directory to an account. Returns the path as recorded."""
    target = _normal(path)
    if not os.path.isdir(target):
        raise CliError(f"not a directory: {target}")

    entries = {key: value for key, value in read_bindings().items()
               if _key(key) != _key(target)}
    entries[target] = name
    _save(dict(sorted(entries.items())))
    return target


def unbind(path: str):
    """Drop a directory's binding. Returns the account it had, or None."""
    target = _key(path)
    entries = read_bindings()
    found = next((key for key in entries if _key(key) == target), None)
    if found is None:
        return None
    name = entries.pop(found)
    _save(entries)
    return name


def rename_account(old: str, new: str):
    """Point every binding on `old` at `new`. Returns the paths changed."""
    entries = read_bindings()
    changed = [key for key, value in entries.items() if value == old]
    if changed:
        _save({key: (new if value == old else value)
               for key, value in entries.items()})
    return changed


def forget_account(name: str):
    """Drop every binding pointing at `name`. Returns the paths dropped."""
    entries = read_bindings()
    gone = [key for key, value in entries.items() if value == name]
    if gone:
        _save({key: value for key, value in entries.items()
               if value != name})
    return gone


def _read_marker(path: str):
    """The account named by a `.codex-account` file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        return None
    return None


def _self_and_parents(start: str):
    current = _normal(start)
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def resolve(start=None):
    """Which account `start` (default: cwd) runs under.

    Returns (name, source, where). `source` is one of `env`, `file` or
    `binding` and is what `cx bindings` and `cx status` report, so a surprising
    answer can be traced to the thing that produced it. (None, None, None) when
    the directory is not bound to anything.
    """
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env, "env", ENV_VAR

    entries = read_bindings()
    by_key = {_key(key): (key, value) for key, value in entries.items()}

    for directory in _self_and_parents(start or os.getcwd()):
        marker = os.path.join(directory, MARKER_FILE)
        if os.path.isfile(marker):
            name = _read_marker(marker)
            if name:
                return name, "file", marker
        hit = by_key.get(_key(directory))
        if hit:
            return hit[1], "binding", hit[0]

    return None, None, None
