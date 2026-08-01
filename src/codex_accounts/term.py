"""Terminal output: colours, status lines, and the shared error type."""

import os
import re
import sys

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class CliError(Exception):
    """Anything the user can fix. Reported without a traceback."""


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING on stdout
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOR = _supports_color()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t): return _c(t, "1")
def dim(t): return _c(t, "2")
def green(t): return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t): return _c(t, "31")
def cyan(t): return _c(t, "36")


def ok(msg): print(f"{green('[ok]')} {msg}")
def info(msg): print(f"{cyan('[--]')} {msg}")
def warn(msg): print(f"{yellow('[!!]')} {msg}")
def err(msg): print(f"{red('[xx]')} {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# debug tracing
# --------------------------------------------------------------------------

_DEBUG = bool(os.environ.get("CODEX_SWITCH_DEBUG"))


def enable_debug() -> None:
    global _DEBUG
    _DEBUG = True


def debug_enabled() -> bool:
    return _DEBUG


def debug(msg) -> None:
    """Trace line on stderr, so it never pollutes piped output."""
    if _DEBUG:
        print(f"{dim('[db]')} {msg}", file=sys.stderr)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def pad(text: str, width: int) -> str:
    """ljust that ignores colour codes when measuring."""
    return text + " " * max(0, width - len(strip_ansi(text)))
