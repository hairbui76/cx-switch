"""Version tracking and self-update straight from GitHub.

A managed install is a set of *versioned* trees plus a one-line `current`
pointer, rather than one tree that gets overwritten:

    ~/.codex-switch/
      versions/0.1.0-a1b2c3d/{bin,src,VERSION}
      versions/0.0.9-9f8e7d6/
      current          -> "0.1.0-a1b2c3d"
      manifest.json    -> repo, channel, sha, previous version

Two reasons for the indirection. Replacing files underneath a running
interpreter fails outright on Windows, and a release that will not start needs
a way back - `update --rollback` is then just a pointer rewrite.

Nothing here needs git: only HTTPS and the standard library.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

from . import VERSION, shims
from .jsonio import read_json, write_json, write_text
from .term import (CliError, bold, debug, dim, green, info, ok, warn, yellow)

DEFAULT_REPO = "hairbui76/codex-switch"

# `main` tracks every push, so a one-line fix is available the moment it lands.
# `stable` tracks the newest published release, for people who only want those.
CHANNELS = ("main", "stable")
DEFAULT_CHANNEL = "main"

# Enough history to roll back through a bad release without hoarding trees.
KEEP_VERSIONS = 3


class HttpError(CliError):
    """A GitHub request failed. Carries the status so 404 can be special."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

def tree_root() -> str:
    """Directory holding the bin/ and src/ of the copy running right now."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_home() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "codex-switch")
    return os.path.join(os.path.expanduser("~"), ".codex-switch")


def install_root() -> str:
    """Root of the managed install, derived from where this code is running.

    Deriving it beats trusting a convention: the same Python may be reached
    from Git Bash or PowerShell, which disagree about where a per-user
    directory belongs.
    """
    override = os.environ.get("CODEX_SWITCH_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    parent = os.path.dirname(tree_root())
    if os.path.basename(parent) == "versions":
        return os.path.dirname(parent)
    return default_home()


def is_managed() -> bool:
    """False when running from a git checkout or an unpacked tarball."""
    return os.path.basename(os.path.dirname(tree_root())) == "versions"


def versions_dir(root=None) -> str:
    return os.path.join(root or install_root(), "versions")


def pointer_path(root=None) -> str:
    return os.path.join(root or install_root(), "current")


def manifest_path(root=None) -> str:
    return os.path.join(root or install_root(), "manifest.json")


def read_manifest(root=None) -> dict:
    return read_json(manifest_path(root), {}) or {}


def read_pointer(root=None):
    try:
        with open(pointer_path(root), "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def repo() -> str:
    """Which GitHub repo to update from.

    Resolved per call rather than frozen at import time. An install made from a
    fork records that fork in its manifest and has to keep updating from it,
    even though the code it just installed defaults to upstream.
    """
    override = os.environ.get("CODEX_SWITCH_REPO")
    if override:
        return override
    return read_manifest().get("repo") or DEFAULT_REPO


def install_url() -> str:
    return f"https://raw.githubusercontent.com/{DEFAULT_REPO}/main/install.sh"


def installed_versions(root=None):
    """Version directory names, newest install first."""
    path = versions_dir(root)
    try:
        names = [n for n in os.listdir(path)
                 if os.path.isdir(os.path.join(path, n))
                 and not n.startswith(".")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(path, n)),
               reverse=True)
    return names


# --------------------------------------------------------------------------
# talking to GitHub
# --------------------------------------------------------------------------

def _http(url: str, timeout: int = 30,
          accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": f"codex-switch/{VERSION}", "Accept": accept}
    # Only for the API: raw.githubusercontent.com and codeload are unmetered,
    # and sending a token to them buys nothing.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"

    debug(f"GET  {url}")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            debug(f"  -> {response.status} {len(body)}B")
            return body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        if exc.code == 403 and "rate limit" in detail.lower():
            raise HttpError("GitHub rate limit reached - retry in a while, "
                            "or set GITHUB_TOKEN to raise the limit", 403)
        if exc.code == 404:
            raise HttpError(f"not found on GitHub: {url}", 404)
        raise HttpError(f"GitHub returned HTTP {exc.code} for {url}"
                        f"{' - ' + detail[:160] if detail else ''}", exc.code)
    except urllib.error.URLError as exc:
        raise HttpError(f"cannot reach GitHub - {exc.reason}")


def _remote_version(sha: str) -> str:
    """The VERSION file at a commit. Pinned by sha to dodge CDN staleness."""
    url = f"https://raw.githubusercontent.com/{repo()}/{sha}/VERSION"
    try:
        text = _http(url, accept="text/plain").decode("utf-8")
    except HttpError as exc:
        if exc.status == 404:
            return "0.0.0"  # a commit from before VERSION existed
        raise
    return text.strip() or "0.0.0"


def resolve_remote(channel: str):
    """(ref, sha, version) for the tip of a channel."""
    if channel not in CHANNELS:
        raise CliError(f"unknown channel '{channel}' - "
                       f"pick one of: {', '.join(CHANNELS)}")

    name = repo()
    if channel == "stable":
        # /releases/latest rather than the tag list: GitHub defines it as the
        # newest non-draft, non-prerelease release, whereas the order tags come
        # back in is not guaranteed to be chronological.
        try:
            release = json.loads(_http(
                f"https://api.github.com/repos/{name}/releases/latest"))
        except HttpError as exc:
            if exc.status != 404:
                raise
            raise CliError(
                f"{name} has published no release yet - use "
                f"`{shims.command_name()} update --channel main`")
        ref = release["tag_name"]
    else:
        ref = "main"

    commit = json.loads(
        _http(f"https://api.github.com/repos/{name}/commits/{ref}"))
    return ref, commit["sha"], _remote_version(commit["sha"])


def build_name(version: str, sha: str) -> str:
    """Directory label for a build. The sha makes untagged pushes distinct."""
    return f"{version}-{sha[:7]}"


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------

def _safe_parts(name: str):
    """Archive path split into components, or None for entries to skip."""
    parts = [p for p in name.split("/")[1:] if p]  # drop <repo>-<ref>/
    if not parts:
        return None
    for part in parts:
        # Paths are vetted by hand because tarfile's `filter=` argument only
        # arrived in 3.12 and this tool supports 3.7.
        if part in (".", "..") or "\\" in part or ":" in part:
            raise CliError(f"refusing unsafe path in archive: {name}")
    return parts


def _extract(blob: bytes, dest: str) -> None:
    """Unpack a GitHub tarball into `dest`, dropping its top-level dir."""
    os.makedirs(dest, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                # The repo has no links; refuse to follow any that appear.
                if member.issym() or member.islnk():
                    continue
                parts = _safe_parts(member.name)
                if parts is None:
                    continue

                target = os.path.join(dest, *parts)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isfile():
                    continue

                os.makedirs(os.path.dirname(target), exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with open(target, "wb") as fh:
                    shutil.copyfileobj(source, fh)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, EOFError, OSError) as exc:
        # A cut-off transfer surfaces here as a gzip EOF rather than an HTTP
        # error, and must still read as a plain message, not a traceback.
        raise CliError(f"the download is not a readable archive - {exc}")

    _make_executable(dest)


def _make_executable(tree: str) -> None:
    """Force the exec bit on the launchers.

    Not inherited from the archive on purpose: a contributor committing from
    Windows has core.filemode=false, so a new script can reach GitHub as mode
    644 and would arrive here unrunnable.
    """
    bin_dir = os.path.join(tree, "bin")
    if not os.path.isdir(bin_dir):
        raise CliError("downloaded tree has no bin/ directory")
    for name in os.listdir(bin_dir):
        if not name.endswith((".sh", ".py")):
            continue
        path = os.path.join(bin_dir, name)
        try:
            os.chmod(path, os.stat(path).st_mode | 0o111)
        except OSError:
            pass


def _verify(tree: str) -> str:
    """Run the freshly unpacked copy's own --version.

    A download that cannot start is worse than no update at all, and this is
    the cheapest check that catches a truncated or half-extracted tree.
    """
    launcher = os.path.join(tree, "bin", "codex-accounts.py")
    if not os.path.isfile(launcher):
        raise CliError("downloaded tree has no bin/codex-accounts.py")

    try:
        proc = subprocess.run(
            [sys.executable, launcher, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError(f"could not run the downloaded version: {exc}")

    output = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise CliError(f"the downloaded version fails to run: {output[:300]}")
    return output


def fetch_build(sha: str, dest: str) -> None:
    """Download the tree at `sha` into `dest`, verified before it lands."""
    url = f"https://codeload.github.com/{repo()}/tar.gz/{sha}"
    info(f"downloading {dim(url)}")
    blob = _http(url, timeout=120, accept="application/octet-stream")

    staging = dest + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _extract(blob, staging)
        _verify(staging)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# activation
# --------------------------------------------------------------------------

def activate(name: str, root=None) -> None:
    root = root or install_root()
    tree = os.path.join(versions_dir(root), name)
    if not os.path.isdir(tree):
        raise CliError(f"version '{name}' is not installed")
    write_text(pointer_path(root), name + "\n")


def prune(root=None, keep=KEEP_VERSIONS) -> None:
    """Drop old trees, never touching the active one or its fallback."""
    root = root or install_root()
    manifest = read_manifest(root)
    protected = {read_pointer(root), manifest.get("previous")}

    for index, name in enumerate(installed_versions(root)):
        if index < keep or name in protected:
            continue
        shutil.rmtree(os.path.join(versions_dir(root), name),
                      ignore_errors=True)
        debug(f"pruned old version {name}")


def _write_manifest(root, channel, ref, sha, version, previous, shim_dir):
    write_json(manifest_path(root), {
        "repo": repo(),
        "channel": channel,
        "ref": ref,
        "sha": sha,
        "version": version,
        "build": build_name(version, sha),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "previous": previous,
        "shim_dir": shim_dir,
        "command": shims.installed_name(),
    })


def _install_build(root, channel, ref, sha, version, previous=None):
    """Download, activate and re-link a build. Returns its directory name."""
    name = build_name(version, sha)
    fetch_build(sha, os.path.join(versions_dir(root), name))
    activate(name, root)
    shim_dir = shims.write_shims(root, shims.installed_name())
    _write_manifest(root, channel, ref, sha, version, previous, shim_dir)
    prune(root)
    return name, shim_dir


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _refuse_checkout(action: str) -> int:
    warn(f"cannot {action}: this is a source checkout, not a managed install")
    print(f"     tree: {tree_root()}")
    print()
    print("  To keep working here:  git pull")
    print("  To switch to a managed install instead:")
    print(f"     curl -fsSL {install_url()} | sh")
    return 1


def show_version() -> int:
    print(f"{bold(shims.command_name())} {VERSION}")

    if not is_managed():
        print(f"  {'source'.ljust(9)} {tree_root()}  {dim('(git checkout)')}")
        print()
        print(dim("  updates come from `git pull` in this directory"))
        return 0

    root = install_root()
    manifest = read_manifest(root)
    build = read_pointer(root) or dim("unset")
    channel = manifest.get("channel", DEFAULT_CHANNEL)

    print(f"  {'build'.ljust(9)} {build}  {dim('channel ' + channel)}")
    if manifest.get("installed_at"):
        print(f"  {'installed'.ljust(9)} {manifest['installed_at']}")
    print(f"  {'root'.ljust(9)} {root}")
    print(f"  {'shims'.ljust(9)} {manifest.get('shim_dir') or dim('unknown')}")
    if manifest.get("previous"):
        print(f"  {'rollback'.ljust(9)} {manifest['previous']}")
    print()
    print(dim("  check for a newer build with: "
              f"{shims.command_name()} update --check"))
    return 0


def rollback() -> int:
    if not is_managed():
        return _refuse_checkout("roll back")

    root = install_root()
    manifest = read_manifest(root)
    previous = manifest.get("previous")
    if not previous:
        raise CliError("no previous version recorded - nothing to go back to")
    if not os.path.isdir(os.path.join(versions_dir(root), previous)):
        raise CliError(f"previous version {previous} is no longer on disk")

    current = read_pointer(root)
    activate(previous, root)
    shims.write_shims(root, shims.installed_name())
    manifest["previous"] = current
    manifest["build"] = previous
    write_json(manifest_path(root), manifest)
    ok(f"rolled back to {bold(previous)}")
    info(f"re-run `{shims.command_name()} update` when a fix lands")
    return 0


def run(check=False, do_rollback=False, channel=None, force=False) -> int:
    if do_rollback:
        return rollback()
    if not is_managed():
        return _refuse_checkout("self-update")

    root = install_root()
    manifest = read_manifest(root)
    channel = channel or manifest.get("channel") or DEFAULT_CHANNEL
    current = read_pointer(root)

    info(f"channel {bold(channel)}  installed {current or dim('unknown')}")
    ref, sha, version = resolve_remote(channel)
    latest = build_name(version, sha)

    reinstall = latest == current

    if check:
        if reinstall:
            ok(f"already up to date ({latest})")
        else:
            warn(f"update available: {bold(latest)}")
            print(dim(f"     run `{shims.command_name()} update` "
                      "to install it"))
        return 0
    if reinstall and not force:
        ok(f"already up to date ({latest})")
        return 0

    info(f"{'reinstalling' if reinstall else 'updating to'} {bold(latest)}")
    # A forced reinstall of the active build must not overwrite the rollback
    # target with itself, or there is nothing left to roll back to.
    previous = manifest.get("previous") if reinstall else current
    _install_build(root, channel, ref, sha, version, previous=previous)

    if reinstall:
        ok(f"reinstalled {green(latest)}")
        return 0

    ok(f"updated {dim(current or '?')} -> {green(latest)}")
    info("shims resolve the new version immediately - no shell restart needed")
    if current:
        print(dim(f"     roll back with: {shims.command_name()} "
                  "update --rollback"))
    return 0


def setup(bootstrap=False, channel=None) -> int:
    """First install (--bootstrap), or repair the shims of an existing one."""
    if bootstrap:
        return _first_install(channel or DEFAULT_CHANNEL)

    if not is_managed():
        return _refuse_checkout("re-link shims")

    root = install_root()
    if not read_pointer(root):
        raise CliError(f"no active version in {root} - re-run the installer")

    name = shims.installed_name()
    shim_dir = shims.write_shims(root, name)
    ok(f"`{bold(name)}` rebuilt in {shim_dir}")
    shims.ensure_on_path(shim_dir, name)

    manifest = read_manifest(root)
    manifest["shim_dir"] = shim_dir
    manifest["command"] = name
    write_json(manifest_path(root), manifest)
    return 0


def _first_install(channel: str) -> int:
    root = install_root()
    current = read_pointer(root)

    info(f"installing {bold(repo())} into {root}")
    ref, sha, version = resolve_remote(channel)

    # Re-running the installer over the build already active must not record
    # that build as its own rollback target, or the previous one is lost and
    # `update --rollback` has nowhere to go.
    previous = current
    if current == build_name(version, sha):
        previous = read_manifest(root).get("previous")

    name, shim_dir = _install_build(
        root, channel, ref, sha, version, previous=previous)

    print()
    ok(f"installed {bold(name)}  {dim('channel ' + channel)}")
    shims.ensure_on_path(shim_dir, shims.installed_name())

    print()
    print(bold("Activate it now:"))
    for line in shims.activation_hint():
        print(f"  {line}")

    print()
    print(bold("Commands:"))
    cmd = shims.command_name()
    for usage, blurb in (
        (f"{cmd} add", "Log in as a new account"),
        (f"{cmd} save <name>", "Save the account now logged in"),
        (f"{cmd} <name>", "Switch to an account"),
        (f"{cmd} list", "List saved accounts"),
        (f"{cmd} status", "Show the current account"),
        (f"{cmd} next", "Switch to the next account"),
        (f"{cmd} usage", "Usage for every account"),
        (f"{cmd} update", "Update to the newest build"),
        (f"{cmd} doctor", "Diagnose setup problems"),
    ):
        print(f"  {usage.ljust(26)}{blurb}")

    print()
    if current:
        print(dim(f"  replaced {current}"))
    else:
        print(dim(f"  coming from the v1 script? run: {cmd} migrate"))
    return 0
