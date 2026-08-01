# Codex Multi-Account Switcher

Switch between multiple Codex accounts on **Windows, macOS and Linux** — with
sessions, config and history shared across every account.

```text
$ cx usage
  ACCOUNT  PLAN  PRIMARY                     SECONDARY
* work     team    5% (7d) Sat 11:36 (6d23h)  -
  personal plus   62% (5h) Fri 18:02 (2h11m)  31% (7d) Mon 06:00 (5d4h)
```

## How it works

Only **`~/.codex/auth.json`** is per-account. Switching writes that one file.

Everything else in `CODEX_HOME` stays where it is and is therefore **shared by
every account automatically**:

```text
~/.codex/sessions/          <- your sessions, on any account
~/.codex/history.jsonl
~/.codex/config.toml        <- model, personality, trusted projects
~/.codex/skills/  plugins/  memories/  rules/
~/.codex/state_*.sqlite     <- everything else Codex keeps
```

There is nothing to sync. Codex holds no account identity outside `auth.json`
— not in `config.toml`, not in the sqlite state — so a switch cannot leave the
previous account's data behind, and it cannot roll your sessions back.

### Accounts are keyed by user, not by workspace

`auth.json` carries a `tokens.account_id`, and it is tempting to treat that as
the account identity. It is not: **it identifies the ChatGPT workspace**, and
every member of a team shares it. Two colleagues on the same team have
identical `account_id`s.

This tool keys accounts on `chatgpt_user_id` from the id_token instead, so
those two logins stay distinct. Getting this wrong is not cosmetic — the
auto-save that runs before every switch would write one account's tokens over
the other's profile.

API-key logins carry no user at all, so they are keyed by a digest of the key.

## Requirements

- Python 3.7+ (standard library only — nothing to `pip install`)
- Codex CLI
- `curl` (or `wget`) and `tar` to run the installer — git is *not* required

## Installation

### macOS / Linux / Git Bash / WSL

```bash
curl -fsSL https://raw.githubusercontent.com/hairbui76/cx-switch/main/install.sh | sh
```

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/hairbui76/cx-switch/main/install.ps1 | iex
```

Then open a new terminal, or `source ~/.bashrc` — the installer tells you which.

No git required. The installer downloads a pinned build over HTTPS, puts a real
`cx` executable on your `PATH`, and records where it came from so the tool can
update itself later.

It is an **executable, not a shell alias**, so it also works from cron, from
scripts, and over `ssh host cx usage`.

To install under a different name:

```bash
CODEX_SWITCH_NAME=cxs curl -fsSL https://raw.githubusercontent.com/hairbui76/cx-switch/main/install.sh | sh
```

Every message the tool prints then refers to `cxs`, because the launcher tells
the CLI which name it was invoked under.

## Usage

### Add an account

```bash
cx add                # runs `codex login` against a throwaway CODEX_HOME
cx add work           # ...and stores the result under a name you pick
cx add work --activate
```

`cx add` does **not** disturb the account you are currently logged in as: the
login runs with `CODEX_HOME` pointed at a temporary directory, so the new
tokens never touch your live `auth.json`.

If you would rather log in the normal way:

```bash
codex login           # log in as usual
cx save work          # store that login as "work"
cx save               # or let it name the account from your email
```

### Switch

```bash
cx work
cx personal
cx next               # round-robin to the next account
```

The account you are leaving is auto-saved first, so rotated tokens are never
lost. If you were logged in as an account that had never been saved, it gets
stored under a name derived from your email rather than being thrown away.

### Inspect

```bash
cx list               # all saved accounts, current one marked with *
cx status             # current account + token expiry
cx usage              # usage for every account
cx usage work         # ...or just one
```

`cx usage` reads `GET https://chatgpt.com/backend-api/wham/usage` with each
account's stored token. It does **not** switch accounts to collect the numbers,
and it refreshes an expired access token automatically.

The window each percentage covers is shown next to it — `(7d)` for a weekly
limit, `(5h)` for a session one. Plans with only one window show `-` in the
second column; that is normal, not an error.

### Manage

```bash
cx remove old-account
cx doctor             # diagnose install, paths, credentials, API access
cx update             # install the newest build
cx migrate            # import profiles from the v1 codex-accounts script
```

## Updating

```bash
cx update             # fetch and install the newest build
cx update --check     # only report whether one exists
cx update --rollback  # go back to the build you had before
cx version            # what is installed, from where
```

Nothing to pull, nothing to re-source: the launcher resolves the active build
at call time, so an update takes effect on the very next command.

### Channels

| Channel | Tracks | Use it when |
| --- | --- | --- |
| `main` (default) | every push to `main` | you want small fixes the moment they land |
| `stable` | the newest published GitHub release | you only want reviewed releases |

```bash
cx update --channel stable    # switch channel and update
```

The channel you pick is remembered, so later `cx update` runs stay on it.

A build is identified as `<version>-<short sha>`, e.g. `0.1.0-a1b2c3d`. The sha
is what makes two pushes that share a version number distinguishable, so a fix
pushed without a version bump is still detected as an update.

### What an install looks like on disk

```text
~/.codex-switch/
  versions/0.1.0-a1b2c3d/    the build in use
  versions/0.0.9-9f8e7d6/    kept so --rollback has somewhere to go
  current                    one line: the active build's directory name
  manifest.json              repo, channel, sha, previous build
~/.local/bin/cx              launcher, reads `current` on every call
```

Updating writes a new directory and then rewrites one line in `current`.
Nothing is overwritten in place, so an interrupted update cannot leave a
half-replaced install behind, and a broken build is one pointer rewrite away
from being undone.

On Windows the root is `%LOCALAPPDATA%\codex-switch` and the launcher goes in
`%LOCALAPPDATA%\codex-switch\bin`, which the installer adds to your user
`PATH`. A `cx.cmd` is written alongside the extensionless `cx`, so cmd.exe,
PowerShell and Git Bash all find a working launcher.

### If the command goes missing

```bash
cx setup    # rebuild the launcher and the PATH entry
```

## Coming from the v1 `codex-accounts` script

The v1 script kept one directory per account under
`~/.codex/account-manager/profiles/`. To import them:

```bash
cx migrate
```

Each profile's `auth.json` is read and rewritten into this tool's own store.
Accounts already present are skipped, so re-running is safe. The v1 tree is
left untouched — delete it by hand once every account has been verified.

Beyond the storage layout, the differences are:

| v1 script | this tool |
| --- | --- |
| Linux only (`fcntl`) | Windows, macOS, Linux |
| Telegram hook + sqlite usage DB | usage read straight from the API |
| keyed on the workspace `account_id` | keyed on `chatgpt_user_id` |
| one 875-line file | a package, stdlib only |

## Troubleshooting

There is no log file. Add `--debug` to any command (or set
`CODEX_SWITCH_DEBUG=1`) to trace every HTTP request on stderr:

```bash
cx usage --debug
```

```text
[db] work: token expired, refreshing
[db] POST https://auth.openai.com/oauth/token  (refresh)
[db]   -> 401 {"error": {"code": "refresh_token_invalidated", ...}}
```

`cx doctor` checks both endpoints the tool depends on, so a broken URL is
distinguishable from a broken token.

| `cx usage` says | Meaning |
| --- | --- |
| `session ended - run codex login` | The stored refresh token is dead. Switch to that account and log in again. |
| `token rejected` | The access token was refused and could not be refreshed. Log in again. |
| `rate limited` | Too many requests. Wait and retry. |
| `usage: HTTP 404` | The API moved. Check `doctor`, then open an issue. |
| `-%` in a column | The plan has no limit in that window. Normal. |

## Notes

- Accounts live in `~/.codex-accounts/<name>.json`, written with `0600`
  permissions. **They contain OAuth tokens — do not commit or share them.**
- `~/.codex/auth.json` is backed up to `~/.codex-accounts/.auth.json.bak`
  before every switch.
- Quit Codex before switching. A running instance — the desktop app in
  particular — may rewrite `auth.json` when it exits and restore the previous
  account.
- Accounts and the install are independent. Reinstalling, updating or rolling
  back never touches `~/.codex-accounts/`.

### Environment variables

| Variable | Effect |
| --- | --- |
| `CODEX_HOME` | Codex's own data directory (Codex reads this too) |
| `CODEX_ACCOUNTS_DIR` | where saved accounts are stored |
| `CODEX_BINARY` | name or path of the Codex CLI, used by `cx add` |
| `CODEX_SWITCH_PYTHON` | interpreter to use |
| `CODEX_SWITCH_HOME` | install root (default `~/.codex-switch`) |
| `CODEX_SWITCH_NAME` | what to call the command (default `cx`) |
| `CODEX_SWITCH_BIN` | directory the launcher goes into |
| `CODEX_SWITCH_CHANNEL` | channel for a fresh install: `main` or `stable` |
| `CODEX_SWITCH_REPO` | `owner/name` to install and update from, e.g. a fork |
| `GITHUB_TOKEN` | raises the GitHub API rate limit for update checks |
| `CODEX_SWITCH_DEBUG` | trace every HTTP request |

## Working on the tool itself

```bash
git clone https://github.com/hairbui76/cx-switch
cd cx-switch
./init.sh          # or .\init.ps1 on PowerShell
```

This points your shell at the working tree, so an edit takes effect
immediately and updates come from `git pull`. `cx update` deliberately refuses
to run here — it would have to overwrite your checkout — and tells you so.

The package can also be run directly:

```bash
PYTHONPATH=src python -m codex_accounts list
```

If PowerShell blocks `init.ps1`, allow local scripts for your user once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Layout

```text
VERSION                     the published version, read by the updater
release-please-config.json  release automation: bumps VERSION, tags, releases
.github/workflows/          release-please
install.sh  install.ps1     curl installer: managed, self-updating copy
init.sh     init.ps1        dev installer: point a shell at this checkout
bin/                        entry points only - no logic
  codex-switch.sh   .ps1      find a usable Python, hand off to the launcher
  codex-accounts.py           launcher: puts src/ on sys.path, calls the CLI
src/codex_accounts/         all logic, stdlib only
  cli.py                      argument parsing, entry point
  commands.py                 one function per subcommand
  store.py                    save / load / apply accounts
  credentials.py              auth.json, JWT claims, identity
  api.py                      usage endpoint + token refresh
  updater.py                  version resolution, download, activate, rollback
  shims.py                    the launcher and PATH wiring
  migrate.py                  importing v1 profiles
  paths.py                    where everything lives
  jsonio.py                   atomic file reads and writes
  term.py                     colours and status lines
legacy/                     the v1 single-file script, kept for reference
```

## Releasing

Versioning is automated by
[release-please](https://github.com/googleapis/release-please). You never edit
`VERSION` or tag by hand. Write
[Conventional Commits](https://www.conventionalcommits.org/) on `main`:

| Commit prefix | Effect on the next release |
| --- | --- |
| `fix: ...` | patch bump — `0.1.0` → `0.1.1` |
| `feat: ...` | minor bump — `0.1.0` → `0.2.0` |
| `feat!: ...` or a `BREAKING CHANGE:` footer | major bump — `0.1.0` → `1.0.0` |
| `docs:`, `refactor:`, `perf:`, `build:` | listed in the changelog, no bump |
| `chore:`, `ci:`, `test:`, `style:` | no bump, hidden from the changelog |

On every push to `main`, the workflow opens or updates a single **release PR**
that bumps `VERSION` and writes `CHANGELOG.md`. Merging that PR tags the commit
`vX.Y.Z` and publishes a GitHub release.

### One-time repo setup

The workflow needs the repository to let Actions open pull requests:

> Settings → Actions → General → Workflow permissions →
> **Allow GitHub Actions to create and approve pull requests**

Without it the job fails with `GitHub Actions is not permitted to create or
approve pull requests`.

### Why a broken push is not a broken install

Clients verify a download by running its `--version` before activating it, and
only then rewrite the `current` pointer. A push that fails to import therefore
makes `cx update` fail with a message and leave the working build in place; it
cannot brick an install.
