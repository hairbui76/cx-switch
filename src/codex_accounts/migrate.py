"""Importing profiles written by the v1 `codex-accounts` script.

v1 kept one directory per account under ~/.codex/account-manager/profiles/,
each with its own auth.json, and a registry listing them:

    {"version": 1, "accounts": {"account-01": {"id", "name", "email",
                                               "auth_file", ...}}}

Importing copies each auth.json into this tool's own store. The v1 tree is
left alone, so both can coexist until the old one is deleted by hand.
"""

import os

from .credentials import user_key
from .jsonio import read_json
from .paths import legacy_manager_dir
from .store import (build_account, find_by_user_key, list_accounts,
                    save_account, slugify, unique_name)
from .term import CliError, bold, dim, info, ok, warn


def legacy_registry_path() -> str:
    return os.path.join(legacy_manager_dir(), "profiles.json")


def read_legacy_profiles():
    """The v1 accounts dict, or {} when there is nothing to import."""
    raw = read_json(legacy_registry_path(), None)
    if not isinstance(raw, dict):
        return {}
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    return {str(k): v for k, v in accounts.items() if isinstance(v, dict)}


def has_legacy_profiles() -> bool:
    return bool(read_legacy_profiles())


def run() -> int:
    profiles = read_legacy_profiles()
    if not profiles:
        info(f"nothing to import - no v1 profiles at "
             f"{dim(legacy_registry_path())}")
        return 0

    info(f"found {len(profiles)} v1 profile(s) in "
         f"{dim(legacy_manager_dir())}")
    imported = skipped = failed = 0

    for account_id in sorted(profiles):
        profile = profiles[account_id]
        label = profile.get("name") or account_id

        auth_file = profile.get("auth_file")
        if not isinstance(auth_file, str) or not auth_file:
            warn(f"{label}: no auth_file recorded, skipping")
            failed += 1
            continue

        auth_file = os.path.expanduser(auth_file)
        try:
            auth = read_json(auth_file, None)
        except CliError as exc:
            warn(f"{label}: {exc}")
            failed += 1
            continue
        if not auth:
            warn(f"{label}: {auth_file} is missing or empty, skipping")
            failed += 1
            continue

        key = user_key(auth)
        if not key:
            warn(f"{label}: no recognisable login in {auth_file}, skipping")
            failed += 1
            continue

        existing = find_by_user_key(key)
        if existing:
            info(f"{label}: already imported as {bold(existing)}")
            skipped += 1
            continue

        # v1 ids look like `account-01`; prefer the display name or the email,
        # falling back to the id only when neither is usable.
        base = slugify(profile.get("name")
                       or (profile.get("email") or "").split("@")[0]
                       or account_id)
        name = unique_name(base)
        save_account(build_account(name, auth))
        ok(f"imported {bold(name)}  {profile.get('email') or dim('?')}")
        imported += 1

    print()
    ok(f"{imported} imported, {skipped} already present, {failed} skipped")
    if imported:
        info(f"{len(list_accounts())} account(s) now in the store")
        print(dim("     the v1 tree is untouched - delete it by hand once "
                  "you have checked every account works"))
    return 0 if not failed else 1
