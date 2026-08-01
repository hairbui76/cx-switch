"""Codex Multi-Account Switcher.

Only ~/.codex/auth.json is per-account. Everything else in CODEX_HOME
(sessions, history, config.toml, skills, plugins, memories, the sqlite state)
stays in place and is therefore shared by every account automatically.
"""

import os


def _read_version() -> str:
    """The version string, read from the repo-root VERSION file.

    Kept in a plain file rather than hard-coded here so `cx update` can learn
    the published version from a single raw URL, without downloading and
    unpacking a whole tree just to compare.
    """
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, "VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()

__all__ = ["VERSION"]
