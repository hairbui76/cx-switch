"""Where Codex and this tool keep their files.

Resolved lazily on every call so tests (and users) can redirect them with
CODEX_HOME / CODEX_ACCOUNTS_DIR at any point.
"""

import os


def codex_dir() -> str:
    """Codex's data directory.

    CODEX_HOME is Codex's own variable, not one this tool invented, so
    honouring it keeps `cx` pointed at whatever `codex` itself would use.
    """
    override = os.environ.get("CODEX_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".codex")


def auth_path() -> str:
    """The one file that is per-account."""
    return os.path.join(codex_dir(), "auth.json")


def config_path() -> str:
    """Codex's config.toml. Shared by every account - never written here."""
    return os.path.join(codex_dir(), "config.toml")


def sessions_dir() -> str:
    """Shared session history. Never written here; listed by `doctor`."""
    return os.path.join(codex_dir(), "sessions")


def store_dir() -> str:
    """Where this tool keeps saved accounts."""
    override = os.environ.get("CODEX_ACCOUNTS_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".codex-accounts")


def account_path(name: str) -> str:
    return os.path.join(store_dir(), name + ".json")


def auth_backup_path() -> str:
    """Copy of the live auth.json, taken before every switch."""
    return os.path.join(store_dir(), ".auth.json.bak")


def legacy_manager_dir() -> str:
    """Where the v1 `codex-accounts` script kept its profiles."""
    override = os.environ.get("CODEX_ACCOUNT_MANAGER_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(codex_dir(), "account-manager")
