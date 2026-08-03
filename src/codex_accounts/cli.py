"""Argument parsing and process entry point."""

import argparse
import os
import sys

from . import VERSION, commands, shims, updater
from .paths import store_dir
from .term import CliError, enable_debug, err

# Anything not in here is treated as an account name, so that
# `cx work` means `cx use work`.
KNOWN_ARGS = {
    "add", "import", "save", "use", "next", "list", "ls", "status",
    "remove", "rm", "usage", "migrate", "doctor", "update",
    "version", "setup", "run", "bind", "unbind", "bindings", "env", "sync",
    "help", "-h", "--help", "--version",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog=shims.command_name(),
        description="Switch between multiple Codex accounts. Sessions, "
                    "config and history are shared by every account.",
        epilog=f"Shorthand: `{shims.command_name()} <name>` is the same "
               f"as `{shims.command_name()} use <name>`.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {VERSION}")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="trace every HTTP request on stderr "
                             "(or set CODEX_SWITCH_DEBUG=1)")
    parser.add_argument("--codex-binary",
                        default=os.environ.get("CODEX_BINARY", "codex"),
                        help="name or path of the Codex CLI (default: codex)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("add",
                       help="log in as a new account without disturbing the "
                            "current one")
    p.add_argument("name", nargs="?",
                   help="account name (default: from email)")
    p.add_argument("--activate", action="store_true",
                   help="switch to it once it is added")
    p.set_defaults(func=commands.cmd_add)

    p = sub.add_parser("import",
                       help="add an account from an auth.json file instead "
                            "of logging in")
    p.add_argument("file", help="path to an auth.json, or - for stdin")
    p.add_argument("name", nargs="?",
                   help="account name (default: from email)")
    p.add_argument("--activate", action="store_true",
                   help="switch to it once it is imported")
    p.add_argument("--verify", action="store_true",
                   help="check the credentials against the API before "
                        "trusting them")
    p.add_argument("--force", action="store_true",
                   help="replace an account that already exists")
    p.set_defaults(func=commands.cmd_import)

    p = sub.add_parser("save", help="save the current login as an account")
    p.add_argument("name", nargs="?",
                   help="account name (default: from email)")
    p.set_defaults(func=commands.cmd_save)

    p = sub.add_parser("use", help="switch to an account")
    p.add_argument("name")
    p.set_defaults(func=commands.cmd_switch)

    p = sub.add_parser("next", help="switch to the next account (round-robin)")
    p.set_defaults(func=commands.cmd_next)

    p = sub.add_parser("run", help="start Codex on this directory's account")
    p.add_argument("-a", "--account",
                   help="ignore the binding and use this account")
    p.add_argument("argv", nargs="*", metavar="...",
                   help="arguments passed straight to codex")
    p.set_defaults(func=commands.cmd_run)

    p = sub.add_parser("bind", help="run this directory under a given account")
    p.add_argument("name", nargs="?",
                   help="account name (default: the one logged in now)")
    p.add_argument("--path", help="directory to bind (default: cwd)")
    p.set_defaults(func=commands.cmd_bind)

    p = sub.add_parser("unbind", help="drop this directory's binding")
    p.add_argument("--path", help="directory to unbind (default: cwd)")
    p.set_defaults(func=commands.cmd_unbind)

    p = sub.add_parser("bindings", help="list directory -> account bindings")
    p.set_defaults(func=commands.cmd_bindings)

    p = sub.add_parser("env",
                       help="print env vars putting a shell on this "
                            "directory's account")
    p.add_argument("-a", "--account", help="use this account instead")
    p.add_argument("--format", choices=("posix", "powershell", "cmd"),
                   help="shell syntax to emit (default: guessed)")
    p.set_defaults(func=commands.cmd_env)

    p = sub.add_parser("sync",
                       help="fold tokens refreshed inside profiles back into "
                            "the store")
    p.set_defaults(func=commands.cmd_sync)

    for alias in ("list", "ls"):
        p = sub.add_parser(alias, help="list saved accounts")
        p.set_defaults(func=commands.cmd_list)

    p = sub.add_parser("status", help="show the account currently logged in")
    p.set_defaults(func=commands.cmd_status)

    for alias in ("remove", "rm"):
        p = sub.add_parser(alias, help="delete a saved account")
        p.add_argument("name")
        p.add_argument("--force", action="store_true",
                       help="allow removing the active account")
        p.set_defaults(func=commands.cmd_remove)

    p = sub.add_parser("usage",
                       help="show usage for every account (no switching)")
    p.add_argument("name", nargs="?", help="limit to one account")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=commands.cmd_usage)

    p = sub.add_parser("migrate",
                       help="import profiles from the v1 codex-accounts "
                            "script")
    p.set_defaults(func=commands.cmd_migrate)

    p = sub.add_parser("doctor",
                       help="diagnose paths, credentials and API access")
    p.set_defaults(func=commands.cmd_doctor)

    p = sub.add_parser("update", help="update to the newest published build")
    p.add_argument("--check", action="store_true",
                   help="report whether an update exists, install nothing")
    p.add_argument("--rollback", action="store_true",
                   help="go back to the previously installed build")
    p.add_argument("--channel", choices=updater.CHANNELS,
                   help="main tracks every push, stable tracks releases "
                        "(default: whatever was installed)")
    p.add_argument("--force", action="store_true",
                   help="reinstall even when already up to date")
    p.set_defaults(func=commands.cmd_update)

    p = sub.add_parser("version",
                       help="show the installed build, channel and paths")
    p.set_defaults(func=commands.cmd_version)

    p = sub.add_parser("setup", help="rebuild the launcher and PATH entry")
    p.add_argument("--bootstrap", action="store_true",
                   help=argparse.SUPPRESS)  # used by install.sh / install.ps1
    p.add_argument("--channel", choices=updater.CHANNELS,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=commands.cmd_setup)

    return parser


def _split_run(argv, start):
    """Split `run`'s own arguments from the ones meant for Codex.

    Done by hand rather than with argparse.REMAINDER, which matches a leading
    option against *our* parser first and so would reject `cx run --model
    gpt-5.1-codex` before Codex ever saw it.
    """
    index = start
    while index < len(argv):
        token = argv[index]
        if token in ("-a", "--account"):
            index += 2
            continue
        # `cx run --help` is a question about `cx run`. Codex's own help is
        # still reachable, as `cx run -- --help`.
        if (token in ("-h", "--help")
                or token.startswith(("-a=", "--account="))):
            index += 1
            continue
        break
    return argv[:index], argv[index:]


def _first_command(argv):
    """Index of the subcommand, skipping the global flags in front of it.

    `--codex-binary` takes a value, and that value is a path - so it has to be
    stepped over rather than mistaken for the subcommand.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--codex-binary":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Insert the implicit `use`, skipping the global flags so that
    # `cx --debug work` still resolves to an account name.
    first = _first_command(argv)
    if first is not None and argv[first] not in KNOWN_ARGS:
        argv.insert(first, "use")

    tail = []
    if first is not None and argv[first] == "run":
        argv, tail = _split_run(argv, first + 1)

    parser = build_parser()
    args = parser.parse_args(argv)
    if tail:
        args.argv = tail
    if args.debug:
        enable_debug()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    os.makedirs(store_dir(), exist_ok=True)
    try:
        return args.func(args)
    except CliError as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
