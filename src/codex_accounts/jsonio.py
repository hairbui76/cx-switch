"""Reading and writing the files this tool owns, always atomically."""

import json
import os
import stat

from .term import CliError


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        raise CliError(f"cannot read {path}: {exc}")


def _atomic_write(path: str, text: str, mode=None) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def write_json(path: str, data, private: bool = False) -> None:
    """Atomic write. `private` chmods the result to 0600 (no-op on Windows)."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    mode = stat.S_IRUSR | stat.S_IWUSR if private else None
    _atomic_write(path, text, mode)


def write_text(path: str, text: str, executable: bool = False) -> None:
    """Atomic write of a plain text file, LF endings, optionally chmod 0755.

    Shims and pointer files are written through here so a crashed or
    interrupted update can never leave a half-written launcher behind.
    """
    _atomic_write(path, text, 0o755 if executable else None)
