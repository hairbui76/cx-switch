"""The account store: saving, loading and applying accounts.

An account is a single JSON file holding a whole auth.json plus the identity
read out of it. Nothing else is captured, which is what makes sessions,
config.toml and history shared rather than per-account.
"""

import os
import re
import shutil
from datetime import datetime, timezone

from . import shims
from .credentials import identity, read_auth, user_key, write_auth
from .jsonio import read_json, write_json
from .paths import (account_path, auth_backup_path, auth_path, store_dir)
from .term import CliError, dim, warn

STORE_VERSION = 1


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip()).strip("-.")
    return slug.lower() or "account"


def unique_name(base: str) -> str:
    existing = set(list_accounts())
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


# --------------------------------------------------------------------------
# reading the store
# --------------------------------------------------------------------------

def list_accounts():
    d = store_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        entry[:-5] for entry in os.listdir(d)
        if entry.endswith(".json") and not entry.startswith(".")
    )


def load_account(name: str):
    data = read_json(account_path(name))
    if data is None:
        raise CliError(
            f"account '{name}' not found. "
            f"Save it with: {shims.command_name()} save {name}"
        )
    if data.get("version") != STORE_VERSION:
        raise CliError(
            f"account '{name}' was written by an incompatible version "
            f"(store v{data.get('version')})"
        )
    return data


def account_summary(data) -> str:
    email = data.get("email") or "?"
    bits = [b for b in (data.get("planType"),
                        data.get("organizationName")) if b]
    return f"{email}{dim('  ' + ' / '.join(bits)) if bits else ''}"


# --------------------------------------------------------------------------
# identifying the live account
# --------------------------------------------------------------------------

def current_user_key():
    """Key of the account logged in right now, read from auth.json."""
    return user_key(read_auth())


def find_by_user_key(key):
    if not key:
        return None
    for name in list_accounts():
        try:
            data = load_account(name)
        except CliError:
            continue
        if data.get("userKey") == key:
            return name
    return None


def current_account_name():
    return find_by_user_key(current_user_key())


# --------------------------------------------------------------------------
# writing the store
# --------------------------------------------------------------------------

def build_account(name: str, auth):
    """Assemble an account record from a whole auth.json."""
    ident = identity(auth)
    return {
        "version": STORE_VERSION,
        "name": name,
        "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "userKey": user_key(auth),
        "userId": ident["userId"],
        "accountId": ident["accountId"],
        "email": ident["email"],
        "planType": ident["planType"],
        "organizationName": ident["organizationName"],
        "authMode": ident["authMode"],
        "auth": auth,
    }


def save_account(data) -> None:
    write_json(account_path(data["name"]), data, private=True)


def snapshot_current(name: str):
    """Capture the live auth.json into account `name`."""
    auth = read_auth()
    if not auth:
        raise CliError(
            f"no credentials found - run `codex login` first "
            f"(looked in {auth_path()})"
        )
    if not user_key(auth):
        raise CliError(
            f"{auth_path()} has no usable login in it - run `codex login`")
    data = build_account(name, auth)
    save_account(data)
    return data


def backup_live_auth() -> None:
    src = auth_path()
    if not os.path.exists(src):
        return
    os.makedirs(store_dir(), exist_ok=True)
    try:
        shutil.copy2(src, auth_backup_path())
    except OSError as exc:
        warn(f"could not back up auth.json: {exc}")


def apply_account(data) -> None:
    """Make `data` the live account.

    One file write. Codex holds no identity anywhere else - not in
    config.toml, not in the sqlite state - so there is nothing else to swap
    and nothing of the previous account left behind.
    """
    backup_live_auth()
    write_auth(data["auth"])
