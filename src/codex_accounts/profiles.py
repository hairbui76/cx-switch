"""Per-directory accounts: several Codex windows, different logins.

`cx use` swaps the one live login, so every window on the machine follows it.
A profile applies the same idea to a single process instead of the whole
machine: a second CODEX_HOME holding *only* the file that says who is logged in
- `auth.json` - with everything else in ~/.codex linked back to the originals.
Pointing `CODEX_HOME` at it gives one Codex process its own identity while it
still reads and writes the same sessions, config.toml, history and skills as
every other window.

Profiles are keyed by account rather than by directory, so two repositories
bound to the same account share one profile - and therefore one set of tokens,
which is exactly what two windows on the same login do today.
"""

import os
import shutil
import signal
import subprocess
import sys

from . import shims
from .credentials import access_expires_at, identity, user_key
from .jsonio import read_json, write_json
from .paths import codex_dir, profile_dir, profiles_dir
from .store import build_account, load_account, save_account
from .term import CliError, dim, warn

# Everything in ~/.codex is linked into a profile except these: the file that
# says who is logged in, a login this tool set aside, the cache of what a plan
# is entitled to, and scratch directories. Anything a future Codex release adds
# is therefore shared by default, which is the promise the rest of the tool
# makes.
PRIVATE_ENTRIES = frozenset((
    "auth.json",
    "auth.json.bak",
    "models_cache.json",
    "tmp",
    ".tmp",
))

# SQLite refuses to be reached through two paths at once: each directory gets
# its own `-wal`, and two write-ahead logs over one database is how a database
# gets corrupted. A symlink is fine - SQLite resolves it and puts the WAL next
# to the real file - but a hardlink is not, so on a Windows without symlinks
# these stay private to the profile rather than risk the shared copy.
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _is_temp(name: str) -> bool:
    """True for the half-written files Codex leaves behind mid-write."""
    return ".tmp" in name


def _is_sqlite(name: str) -> bool:
    base = name
    for suffix in SQLITE_SIDECARS:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base.endswith(SQLITE_SUFFIXES)


def _within(child: str, parent: str) -> bool:
    child = os.path.normcase(os.path.abspath(child))
    parent = os.path.normcase(os.path.abspath(parent))
    return child == parent or child.startswith(parent + os.sep)


def shared_root() -> str:
    """The ~/.codex everything is shared from.

    Refuses to answer inside a window `cx run` started: CODEX_HOME points at a
    profile there, and building a profile from a profile would link a directory
    into itself.
    """
    root = codex_dir()
    if _within(root, profiles_dir()):
        raise CliError(
            f"CODEX_HOME points at a profile ({root}) - this shell is already "
            "running on one. Run this from a shell where CODEX_HOME is unset."
        )
    return root


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------

def _is_link(path: str) -> bool:
    """True for a symlink or a Windows directory junction."""
    if os.path.islink(path):
        return True
    try:
        # Junctions are not symlinks and islink() says so, but readlink() has
        # answered for them since 3.8.
        os.readlink(path)
        return True
    except (OSError, ValueError, NotImplementedError):
        return False


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        pass
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _drop_link(path: str) -> None:
    """Remove a link without touching whatever it points at."""
    try:
        os.unlink(path)
    except OSError:
        # A directory symlink or junction on Windows refuses unlink; rmdir
        # removes the link itself and leaves the target alone.
        os.rmdir(path)


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return -1.0


def _symlink(src: str, dst: str, is_dir: bool) -> bool:
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def _junction(src: str, dst: str) -> bool:
    """A directory link that needs no privilege on Windows."""
    try:
        out = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                             capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _link_dir(src: str, dst: str, label: str) -> None:
    if os.path.lexists(dst):
        if _same(src, dst):
            return
        if not _is_link(dst):
            warn(f"{label}: the profile has a real directory here, "
                 "leaving it unshared")
            return
        _drop_link(dst)

    # Symlinks on Windows need Developer Mode or an elevated shell; junctions
    # need no privilege at all.
    if _symlink(src, dst, True):
        return
    if os.name == "nt" and _junction(src, dst):
        return
    warn(f"could not link {label} into the profile - "
         "that directory will not be shared")


def _link_file(src: str, dst: str, label: str):
    """Link one file into a profile. Returns a note when it could not be."""
    if os.path.lexists(dst):
        if _same(src, dst):
            return None
        # A hardlink survives appends but not the write-temp-then-rename that
        # Codex uses, so the two copies drift apart the first time the file is
        # rewritten. Keep whichever was written last and link again, which
        # turns every launch and every exit into a repair.
        if _mtime(dst) > _mtime(src):
            try:
                shutil.copy2(dst, src)
            except OSError as exc:
                warn(f"{label}: could not fold the profile's copy back "
                     f"into {src}: {exc}")
                return None
        try:
            _drop_link(dst) if _is_link(dst) else os.remove(dst)
        except OSError as exc:
            warn(f"{label}: could not replace the profile's copy: {exc}")
            return None

    if _symlink(src, dst, False):
        return None
    if _is_sqlite(label):
        # Never hardlinked and never copied: a stale copy of a database is
        # worse than one this profile filled in itself.
        return label
    try:
        os.link(src, dst)
        return None
    except OSError:
        pass
    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        warn(f"could not share {label}: {exc}")
        return None
    warn(f"{label}: copied instead of linked - "
         "edits inside this profile stay in the profile")
    return None


def _shareable(root: str):
    """Names in ~/.codex a profile should link back, in a stable order."""
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        raise CliError(f"cannot read {root}: {exc}")
    return [entry for entry in entries
            if entry not in PRIVATE_ENTRIES and not _is_temp(entry)]


def _note_unshared(profile: str, skipped) -> None:
    """Report what could not be shared - but only when the answer changes.

    Linking runs on every launch and every exit, and on a Windows without
    symlinks the same databases are skipped every time. Saying so once is
    useful; saying it four times a day is noise. `cx doctor` has the standing
    answer.
    """
    marker = os.path.join(profile, ".unshared")
    try:
        before = read_json(marker, [])
    except CliError:
        before = None
    if skipped == before:
        return

    try:
        write_json(marker, skipped)
    except OSError:
        pass
    if skipped:
        warn("kept per-account, this platform cannot symlink a database: "
             f"{', '.join(skipped)}")
        print(dim("     enable Developer Mode to share them too"))


def link_shared(profile: str) -> None:
    """(Re)point everything shareable in `profile` at ~/.codex.

    Run on every launch and again on exit, so an entry Codex replaced with a
    file of its own is repaired rather than quietly forked.
    """
    root = shared_root()
    skipped = []

    for entry in _shareable(root):
        src = os.path.join(root, entry)
        dst = os.path.join(profile, entry)
        if os.path.isdir(src):
            _link_dir(src, dst, entry)
        elif os.path.isfile(src):
            note = _link_file(src, dst, entry)
            if note:
                skipped.append(note)

    _note_unshared(profile, skipped)


def link_report(profile: str):
    """(shared, unshared) entry names, for `cx doctor`.

    Everything private is filtered out before the comparison, so anything left
    that is missing from the profile - a database this platform could not
    symlink - is genuinely not shared and is reported as such.
    """
    shared, unshared = [], []
    try:
        root = shared_root()
        entries = _shareable(root)
    except CliError:
        return shared, unshared

    for entry in entries:
        src = os.path.join(root, entry)
        dst = os.path.join(profile, entry)
        linked = os.path.lexists(dst) and _same(src, dst)
        (shared if linked else unshared).append(entry)
    return shared, unshared


# --------------------------------------------------------------------------
# the profile itself
# --------------------------------------------------------------------------

def list_profiles():
    root = profiles_dir()
    if not os.path.isdir(root):
        return []
    return sorted(entry for entry in os.listdir(root)
                  if os.path.isdir(os.path.join(root, entry)))


def auth_path_for(name: str) -> str:
    return os.path.join(profile_dir(name), "auth.json")


def _stray_path(name: str) -> str:
    return os.path.join(profile_dir(name), "auth.json.bak")


def profile_auth(name):
    """auth.json as last written *inside* a profile, or None.

    An account only ever launched through `cx run` refreshes its tokens in
    there, so this copy can be newer than the store's.
    """
    if not name:
        return None
    return read_json(auth_path_for(name))


def _expiry(auth) -> float:
    return access_expires_at(auth) or 0.0


def _describe(auth) -> str:
    return identity(auth)["email"] or "unknown login"


def _write_auth(name: str, data) -> None:
    """Install the stored credentials, unless the profile's are better.

    Two cases where they are. A window killed without running its exit hook
    leaves rotated tokens in the profile that the store never saw, and refresh
    tokens rotate - handing Codex the store's older copy would hand it a token
    the server has already retired. And someone may have run `codex login` in
    that window as a different account entirely, which is not this account's to
    overwrite silently.
    """
    target = auth_path_for(name)
    live = read_json(target)
    stored = data.get("auth")

    if live and user_key(live):
        if user_key(live) != data.get("userKey"):
            # Set it aside rather than destroy it: it is a whole working login,
            # and `cx import` can still take it on under a name of its own.
            spare = _stray_path(name)
            try:
                write_json(spare, live, private=True)
                warn(f"this profile was logged in as {_describe(live)}, not "
                     f"{name} - kept that login at {spare}")
                print(dim(f"     save it with: {shims.command_name()} import "
                          f"{spare} <name>"))
            except OSError as exc:
                raise CliError(
                    f"this profile is logged in as {_describe(live)}, not "
                    f"{name}, and its credentials could not be set aside "
                    f"({exc}) - move {target} away by hand first")
        elif _expiry(live) > _expiry(stored):
            data["auth"] = live
            save_account(build_account(name, live))
            return

    if not stored:
        # Nothing to install - whatever the profile has is all there is.
        return
    write_json(target, stored, private=True)


def ensure_profile(data) -> str:
    """Create or refresh the CODEX_HOME for account `data`."""
    name = data["name"]
    path = profile_dir(name)
    os.makedirs(path, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    link_shared(path)
    _write_auth(name, data)
    return path


def sync_back(name: str) -> bool:
    """Fold what an instance wrote in its profile back into the store.

    Tokens rotate while Codex runs, so without this the store would go stale
    for any account only ever used through `cx run` - and `cx usage` would be
    reporting on a refresh token that no longer works.
    """
    live = profile_auth(name)
    if not live or not user_key(live):
        # Codex blanks this file when a login expires past repair. Saving that
        # over the store would destroy the refresh token `cx usage` and a later
        # `codex login` can still be recovered from, so keep ours.
        if live is not None:
            warn(f"{name}: no usable login left in its profile - kept the "
                 "stored copy; run `codex login` in that window to sign in "
                 "again")
        link_shared(profile_dir(name))
        return False

    try:
        stored = load_account(name)
    except CliError:
        warn(f"{name}: a profile with no saved account. Take it on with: "
             f"{shims.command_name()} import {auth_path_for(name)} {name}")
        return False

    if user_key(live) != stored.get("userKey"):
        # Left for `cx run` to set aside on the next launch; overwriting the
        # account here would file one login's tokens under another's name.
        warn(f"{name}: its profile is logged in as {_describe(live)} - not "
             f"saved over {name}")
        print(dim(f"     save it with: {shims.command_name()} import "
                  f"{auth_path_for(name)} <name>"))
        return False

    save_account(build_account(name, live))
    link_shared(profile_dir(name))
    return True


def rename_profile(old: str, new: str) -> bool:
    """Move a profile to a new account name.

    The links inside it are absolute, so moving the directory leaves them
    pointing exactly where they did.
    """
    source = profile_dir(old)
    if not os.path.isdir(source):
        return False

    target = profile_dir(new)
    if os.path.isdir(target):
        # An orphan from an earlier rename or removal. Unlink it properly
        # rather than let os.rename fail on a directory that is not empty.
        remove_profile(new)

    try:
        os.rename(source, target)
    except OSError as exc:
        warn(f"could not move the profile to {target}: {exc}")
        return False
    return True


def remove_profile(name: str) -> bool:
    """Delete a profile. Only the links are lost; ~/.codex is untouched."""
    path = profile_dir(name)
    if not os.path.isdir(path):
        return False
    # Every shared entry is a link, so a plain rmtree could walk into ~/.codex
    # and delete the real sessions. Unlink the links first.
    for entry in sorted(os.listdir(path)):
        target = os.path.join(path, entry)
        if _is_link(target):
            try:
                _drop_link(target)
            except OSError as exc:
                warn(f"could not unlink {target}: {exc}")
                return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.isdir(path)


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

def codex_executable(binary=None) -> str:
    name = binary or os.environ.get("CODEX_BINARY") or "codex"
    found = shutil.which(name)
    if not found:
        raise CliError(
            f"`{name}` not found on PATH - install Codex, or point "
            "CODEX_BINARY at its executable"
        )
    return found


def _swallow_interrupt() -> None:
    """Stop Ctrl-C from killing this wrapper: the key belongs to Codex.

    A no-op handler rather than SIG_IGN, because SIG_IGN is inherited through
    exec and would leave Codex itself deaf to Ctrl-C.
    """
    def ignore(signum, frame):
        pass

    try:
        signal.signal(signal.SIGINT, ignore)
    except (ValueError, OSError, AttributeError):
        pass


def launch(data, argv, binary=None) -> int:
    """Run Codex against `data`'s profile and wait for it to exit."""
    path = ensure_profile(data)
    executable = codex_executable(binary)

    env = dict(os.environ)
    env["CODEX_HOME"] = path
    env["CODEX_SWITCH_ACCOUNT"] = data["name"]

    _swallow_interrupt()
    # Say which account this is *before* Codex takes over the terminal; a
    # redirected stdout is block-buffered and would otherwise print it last.
    sys.stdout.flush()
    try:
        proc = subprocess.Popen([executable] + list(argv), env=env)
    except OSError as exc:
        raise CliError(f"could not start {executable}: {exc}")

    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            # Windows raises this in the parent as well; the child is handling
            # the key, so keep waiting for it.
            continue

    sync_back(data["name"])
    return code


def env_exports(data, shell: str):
    """Lines that put a shell (or an IDE, or direnv) on this account.

    The escape hatch for anything that launches `codex` itself instead of going
    through `cx run`: VS Code's integrated terminal, a wrapper script, a
    long-lived tmux pane.

    `plain` emits bare `KEY=VALUE` for consumers that read the pairs
    themselves rather than eval'ing a shell: agent-of-empires'
    `host_hooks.before_session`, `docker --env-file`, systemd
    `EnvironmentFile`. No quoting is applied, because nothing downstream
    unquotes it -- a quote would land in the value.
    """
    path = ensure_profile(data)
    if shell == "posix" and os.name == "nt":
        # A backslash is an escape in most POSIX shells; Codex reads either
        # separator, so hand the forward-slash form to bash and zsh.
        path = path.replace("\\", "/")
    pairs = (("CODEX_HOME", path),
             ("CODEX_SWITCH_ACCOUNT", data["name"]))

    if shell == "plain":
        return [f"{key}={value}" for key, value in pairs]
    if shell == "powershell":
        return [f'$env:{key} = "{value}"' for key, value in pairs]
    if shell == "cmd":
        return [f'set "{key}={value}"' for key, value in pairs]
    return [f'export {key}="{value}"' for key, value in pairs]
