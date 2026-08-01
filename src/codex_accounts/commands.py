"""One function per subcommand. All of them return a process exit code."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import migrate, shims, updater
from .api import (OAUTH_CLIENT_ID, TOKEN_URL, USAGE_URL, ApiError, fmt_credits,
                  fmt_window, http_json, usage_for, windows)
from .credentials import (access_expires_at, identity, normalize_auth,
                          read_auth, user_key)
from .jsonio import read_json
from .paths import (account_path, auth_path, codex_dir, sessions_dir,
                    store_dir)
from .store import (account_summary, apply_account, build_account,
                    current_account_name, current_user_key, find_by_user_key,
                    list_accounts, load_account, save_account, slugify,
                    snapshot_current, unique_name)
from .term import (CliError, bold, dim, green, info, ok, pad, red, warn,
                   yellow)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _live_identity():
    return identity(read_auth())


def _name_from_email(email, fallback="account"):
    return slugify(email.split("@")[0]) if email else fallback


def _column(values, header) -> int:
    """Width of a table column, never narrower than its own header."""
    return max([len(header)] + [len(v) for v in values])


def autosave_current():
    """Refresh the stored copy of whatever account is live right now.

    Codex rotates tokens behind our back, so the stored copy would go stale
    without this. An account that was never saved is kept under a name derived
    from its email rather than being discarded.
    """
    key = current_user_key()
    if not key:
        return None

    name = find_by_user_key(key)
    if name:
        try:
            snapshot_current(name)
            info(f"auto-saved current account {bold(name)}")
        except CliError as exc:
            warn(f"could not auto-save current account: {exc}")
        return name

    email = _live_identity()["email"] or ""
    derived = unique_name(_name_from_email(email, "unsaved"))
    try:
        snapshot_current(derived)
        warn(f"current login was not saved - stored it as {bold(derived)}")
    except CliError:
        warn("current login is not saved and has no credentials to store")
        return None
    return derived


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def cmd_save(args):
    name = args.name
    if not name:
        email = _live_identity()["email"]
        if not email:
            raise CliError(
                "not logged in - run `codex login`, or pass a name explicitly")
        name = _name_from_email(email)
        info(f"no name given, using {bold(name)} (from {email})")

    data = snapshot_current(name)
    ok(f"saved {bold(name)}  {account_summary(data)}")
    return 0


def _read_source(source):
    """(text, label) for a path or `-` meaning stdin."""
    if source == "-":
        return sys.stdin.read(), "stdin"
    path = os.path.expanduser(source)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(), path
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}")


def _report_token(auth) -> None:
    """Say whether the credentials just taken on are usable yet."""
    expires = access_expires_at(auth)
    if expires is None:
        return
    left = expires - time.time()
    if left > 0:
        info(f"access token valid for {int(left // 3600)}h")
    else:
        warn("the access token in this file has expired - it will be "
             "refreshed on first use, provided the refresh token is live")


def cmd_import(args):
    """Take on an account from an auth.json instead of logging in.

    For credentials that arrived some other way: copied off another machine,
    handed over by a teammate, or produced by a login that happened elsewhere.
    Nothing is written to the live auth.json unless --activate is passed.
    """
    raw, label = _read_source(args.file)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CliError(f"{label} is not valid JSON: {exc}")

    auth = normalize_auth(parsed)
    key = user_key(auth)
    if not key:
        raise CliError(f"{label} carries no recognisable login - it has "
                       "neither a usable token nor an API key")

    ident = identity(auth)
    existing = find_by_user_key(key)

    if existing and not args.force:
        raise CliError(
            f"that login is already saved as '{existing}' - pass --force to "
            "replace its credentials with this file")
    if existing:
        # Replacing a known login: keep the name it is already filed under, or
        # `cx use <old name>` would silently go on using the stale copy.
        if args.name and args.name != existing:
            warn(f"keeping the existing name {bold(existing)} - "
                 f"rename it afterwards if you want {args.name}")
        name = existing
    else:
        name = args.name or unique_name(_name_from_email(ident["email"]))
        clash = name in list_accounts()
        if clash and not args.force:
            raise CliError(
                f"account '{name}' already exists for a different login - "
                "pick another name, or pass --force to overwrite it")

    data = build_account(name, auth)
    save_account(data)
    ok(f"imported {bold(name)}  {account_summary(data)}  "
       f"{dim('from ' + label)}")
    _report_token(auth)

    if args.verify:
        info("checking the credentials against the API")
        usage = usage_for(data)
        ok(f"verified - {usage.get('email') or '?'} "
           f"{dim(usage.get('plan_type') or '')}")

    if args.activate:
        autosave_current()
        apply_account(data)
        ok(f"switched to {bold(name)}")
    else:
        info(f"switch to it with: {shims.command_name()} {name}")
    return 0


def cmd_add(args):
    """Log in as another account without disturbing the current one.

    `codex login` is run against a throwaway CODEX_HOME, so the new tokens
    land in a temporary directory instead of overwriting the live auth.json.
    That makes adding an account safe even mid-session.
    """
    binary = shutil.which(args.codex_binary)
    if not binary:
        raise CliError(f"`{args.codex_binary}` not found on PATH - "
                       "install Codex, or pass --codex-binary")

    workdir = tempfile.mkdtemp(prefix="codex-switch-login-")
    try:
        env = os.environ.copy()
        env["CODEX_HOME"] = workdir

        info(f"running {bold(args.codex_binary + ' login')} against a "
             "temporary CODEX_HOME")
        print(dim("     your current login is not touched by this"))
        try:
            result = subprocess.run([binary, "login"], env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CliError(f"could not run `{args.codex_binary} login`: {exc}")
        if result.returncode != 0:
            raise CliError(
                f"`codex login` exited with code {result.returncode}")

        auth = read_json(os.path.join(workdir, "auth.json"))
        if not auth:
            raise CliError(
                "login finished but no auth.json appeared in the temporary "
                "CODEX_HOME - this Codex build may store credentials "
                "elsewhere. Log in normally and use "
                f"`{shims.command_name()} save` instead."
            )

        key = user_key(auth)
        if not key:
            raise CliError("the new auth.json carries no recognisable login")

        existing = find_by_user_key(key)
        if existing:
            raise CliError(f"that account is already saved as '{existing}'")

        ident = identity(auth)
        name = args.name or unique_name(_name_from_email(ident["email"]))
        data = build_account(name, auth)
        save_account(data)
    finally:
        # The temp dir holds a complete set of credentials - never leave it.
        shutil.rmtree(workdir, ignore_errors=True)

    ok(f"added {bold(name)}  {account_summary(data)}")
    if args.activate:
        autosave_current()
        apply_account(data)
        ok(f"switched to {bold(name)}")
    else:
        info(f"switch to it with: {shims.command_name()} {name}")
    return 0


# --------------------------------------------------------------------------
# switching
# --------------------------------------------------------------------------

def cmd_switch(args):
    target = load_account(args.name)
    if target.get("userKey") and target["userKey"] == current_user_key():
        ok(f"already on {bold(args.name)}  {account_summary(target)}")
        return 0

    autosave_current()
    apply_account(target)
    ok(f"switched to {bold(args.name)}  {account_summary(target)}")
    info("sessions, config.toml and history are shared - nothing else to sync")
    return 0


def cmd_next(args):
    names = list_accounts()
    if not names:
        raise CliError("no accounts saved yet. Run: "
                       f"{shims.command_name()} add")
    if len(names) == 1:
        ok(f"only one account ({bold(names[0])})")
        return 0

    current = current_account_name()
    index = names.index(current) if current in names else -1
    target = names[(index + 1) % len(names)]

    args.name = target
    code = cmd_switch(args)
    print(dim(f"     position {names.index(target) + 1}/{len(names)}"))
    return code


# --------------------------------------------------------------------------
# inspecting
# --------------------------------------------------------------------------

def cmd_list(args):
    names = list_accounts()
    if not names:
        warn(f"no accounts saved yet. Run: {shims.command_name()} add")
        return 0

    current = current_account_name()
    width = _column(names, "ACCOUNT")
    print(bold(f"  {'ACCOUNT'.ljust(width)}  EMAIL"))

    for name in names:
        try:
            summary = account_summary(load_account(name))
        except CliError as exc:
            summary = red(str(exc))
        marker = green("*") if name == current else " "
        label = bold(name) if name == current else name
        print(f"{marker} {pad(label, width)}  {summary}")

    if current is None:
        print()
        warn("the account logged in right now matches no saved account")
        print(dim(f"     save it with: {shims.command_name()} save"))
    return 0


def cmd_status(args):
    auth = read_auth()
    if not auth:
        warn(f"not logged in - no {auth_path()}")
        return 1

    ident = identity(auth)
    name = find_by_user_key(user_key(auth))
    label = bold(name) if name else yellow("(unsaved)")
    bits = [b for b in (ident["planType"], ident["organizationName"]) if b]
    print(f"{green('[ok]')} current: {label}  {ident['email'] or '?'}"
          f"{dim('  ' + ' / '.join(bits)) if bits else ''}")

    expires = access_expires_at(auth)
    if expires:
        left = expires - time.time()
        state = green("valid") if left > 0 else red("expired")
        if left > 0:
            hours = int(left // 3600)
            remaining = dim(f", {hours}h left" if hours
                            else f", {int(left // 60)}m left")
        else:
            remaining = ""
        mode = dim("  mode: " + ident["authMode"]) if ident["authMode"] else ""
        print(f"     token: {state}{remaining}{mode}")
    return 0


def cmd_usage(args):
    names = [args.name] if args.name else list_accounts()
    if not names:
        raise CliError("no accounts saved yet. Run: "
                       f"{shims.command_name()} add")

    rows = []
    for name in names:
        try:
            data = load_account(name)
            rows.append((name, data, usage_for(data), None))
        except CliError as exc:
            # ApiError already explains which call failed and why.
            rows.append((name, None, None, str(exc)))

    current = current_account_name()
    width = _column(names, "ACCOUNT")
    plan_width = _column([(d or {}).get("planType") or "?"
                          for _, d, _, _ in rows], "PLAN")
    print(bold(f"  {'ACCOUNT'.ljust(width)}  {'PLAN'.ljust(plan_width)}  "
               f"{'PRIMARY'.ljust(28)}  SECONDARY"))

    for name, data, usage, error in rows:
        marker = green("*") if name == current else " "
        if error:
            print(f"{marker} {name.ljust(width)}  {red(error)}")
            continue
        primary, secondary = windows(usage)
        plan = (usage.get("plan_type") or data.get("planType") or "?")
        print(f"{marker} {name.ljust(width)}  {plan.ljust(plan_width)}  "
              f"{pad(fmt_window(primary), 28)}  {fmt_window(secondary)}")
        if args.verbose:
            print(dim(f"    {usage.get('email') or account_summary(data)}"
                      f"   credits: ") + fmt_credits(usage))
    return 0


# --------------------------------------------------------------------------
# managing
# --------------------------------------------------------------------------

def cmd_remove(args):
    path = account_path(args.name)
    if not os.path.exists(path):
        raise CliError(f"account '{args.name}' not found")
    if current_account_name() == args.name and not args.force:
        raise CliError(
            f"'{args.name}' is the account you are logged in as. "
            "Switch away first, or pass --force"
        )

    os.remove(path)
    ok(f"removed {bold(args.name)}")
    return 0


def cmd_migrate(args):
    return migrate.run()


def cmd_update(args):
    return updater.run(check=args.check, do_rollback=args.rollback,
                       channel=args.channel, force=args.force)


def cmd_version(args):
    return updater.show_version()


def cmd_setup(args):
    return updater.setup(bootstrap=args.bootstrap, channel=args.channel)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args):
    print(bold("install"))
    if updater.is_managed():
        manifest = updater.read_manifest()
        spare = updater.installed_versions()[1:]
        print(f"  kind:    managed  {dim(updater.install_root())}")
        print(f"  build:   {updater.read_pointer() or yellow('unset')}"
              f"  {dim('channel ' + manifest.get('channel', '?'))}")
        print(f"  shims:   {manifest.get('shim_dir') or yellow('unknown')}")
        print(f"  spare:   {', '.join(spare) or dim('none')}")
    else:
        print(f"  kind:    source checkout  {dim(updater.tree_root())}")
        print(dim("           update it with `git pull`"))

    print()
    print(bold("paths"))
    for label, path in (
        ("codex home", codex_dir()),
        ("auth.json", auth_path()),
        ("sessions", sessions_dir()),
        ("store", store_dir()),
    ):
        state = green("found") if os.path.exists(path) else yellow("missing")
        print(f"  {label.ljust(11)} {path}  [{state}]")
    found = shutil.which(args.codex_binary) or "not found"
    print(dim(f"  codex binary {found}"))

    print()
    print(bold("credentials"))
    auth = read_auth()
    if not auth:
        print(f"  {red('none')} - run `codex login`")
    else:
        ident = identity(auth)
        print(f"  mode:    {ident['authMode'] or dim('unknown')}")
        print(f"  user:    {ident['email'] or dim('unknown')}  "
              f"{dim(ident['userId'] or '')}")
        print(f"  plan:    {ident['planType'] or dim('unknown')}")
        expires = access_expires_at(auth)
        if expires:
            left = (expires - time.time()) / 3600
            state = green(f"{left:.1f}h left") if left > 0 else red("expired")
            print(f"  token:   {state}")

    print()
    print(bold("accounts"))
    names = list_accounts()
    print(f"  {len(names)} saved: {', '.join(names) or dim('none')}")
    print(f"  current: {current_account_name() or yellow('unsaved')}")
    if migrate.has_legacy_profiles():
        print(yellow(f"  v1 profiles found - run "
                     f"`{shims.command_name()} migrate`"))

    print()
    print(bold("api"))
    if not auth:
        print(f"  usage  {yellow('skipped')} - not logged in")
    else:
        try:
            from .credentials import tokens
            fetch = tokens(auth)
            http_json(USAGE_URL, token=fetch.get("access_token"),
                      account_id=fetch.get("account_id"), stage="usage")
            print(f"  usage  {green('reachable')}  {USAGE_URL}")
        except CliError as exc:
            print(f"  usage  {red('failed')}  {exc}")

    # Probe with a deliberately invalid grant: a 400/401 proves the endpoint is
    # there, which is what distinguishes "expired token" from "wrong URL".
    try:
        http_json(TOKEN_URL, stage="refresh", payload={
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "probe-invalid",
        })
        print(f"  token  {green('reachable')}  {TOKEN_URL}")
    except ApiError as exc:
        reachable = exc.status in (400, 401)
        state = green("reachable") if reachable else red("failed")
        note = "" if reachable else f"  {exc}"
        print(f"  token  {state}  {TOKEN_URL}{note}")

    print()
    print(dim("  re-run any command with --debug to trace every request"))
    return 0
