# v1: `codex-accounts.py`

The single-file script this project grew out of. Kept for reference only — it
is not installed, not tested, and receives no fixes.

It stored one directory per account under
`~/.codex/account-manager/profiles/`, each holding its own `auth.json`, and
copied the chosen one over `~/.codex/auth.json` on switch. `cx migrate` reads
that layout and imports it, so there is no need to run this script again.

Why it was replaced rather than extended:

- **Linux only.** It locks with `fcntl`, which does not exist on Windows, so
  the module fails to import there at all.
- **Usage came from a side channel.** Quota was written into a sqlite database
  by a Telegram notification hook and read back out; accounts with no recent
  hook run showed nothing. `cx usage` asks the API directly instead.
- **Accounts were keyed on the workspace.** Profiles were addressed by a
  manually assigned id, and the active account was tracked in `active.json`
  rather than derived from the credentials, so nothing detected two logins in
  the same ChatGPT team sharing a `tokens.account_id`.

The good idea it contributed is `cx add`: running `codex login` against a
throwaway `CODEX_HOME` so a new account can be added without disturbing the
one already logged in.
