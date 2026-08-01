#!/usr/bin/env python3
"""Launcher: puts src/ on sys.path and hands off to the package.

Kept separate from the package so the shell wrappers do not have to fight with
PYTHONPATH quoting across Windows, Git Bash and POSIX shells.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from codex_accounts.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
