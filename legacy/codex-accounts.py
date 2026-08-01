#!/usr/bin/env python3
"""Manual multi-account manager for Codex CLI on Ubuntu.

This tool keeps Codex sessions in one shared CODEX_HOME (default: ~/.codex)
while storing one auth.json per account. Switching is always explicit/manual.

Commands:
  codex-accounts add
  codex-accounts list
  codex-accounts status
  codex-accounts switch [ACCOUNT]
  codex-accounts current
  codex-accounts run [ACCOUNT] [-- CODEX_ARGS...]
  codex-accounts resume SESSION_ID [--account ACCOUNT] [-- PROMPT...]
  codex-accounts rename ACCOUNT NEW_NAME
  codex-accounts remove ACCOUNT
  codex-accounts telegram
  codex-accounts doctor

The Telegram hook can identify the active account through:
  ~/.codex/account-manager/active.json
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

APP_NAME = "codex-accounts"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def shared_codex_home() -> Path:
    return Path(
        os.environ.get("CODEX_SHARED_HOME", Path.home() / ".codex")
    ).expanduser().resolve()


def manager_home() -> Path:
    default = shared_codex_home() / "account-manager"
    return Path(
        os.environ.get("CODEX_ACCOUNT_MANAGER_HOME", default)
    ).expanduser().resolve()


def profiles_file() -> Path:
    return manager_home() / "profiles.json"


def profiles_dir() -> Path:
    return manager_home() / "profiles"


def active_file() -> Path:
    return manager_home() / "active.json"


def lock_file() -> Path:
    return manager_home() / "switch.lock"


def usage_db_path() -> Path:
    default = shared_codex_home() / "account-usage.db"
    return Path(os.environ.get("CODEX_USAGE_DB", default)).expanduser().resolve()


def hook_path() -> Path:
    default = shared_codex_home() / "hooks" / "telegram_notify.py"
    return Path(os.environ.get("CODEX_TELEGRAM_HOOK", default)).expanduser().resolve()


def shared_auth_path() -> Path:
    return shared_codex_home() / "auth.json"


def ensure_layout() -> None:
    shared_codex_home().mkdir(parents=True, exist_ok=True)
    manager_home().mkdir(parents=True, exist_ok=True, mode=0o700)
    profiles_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(manager_home(), 0o700)
        os.chmod(profiles_dir(), 0o700)
    except OSError:
        pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Không đọc được JSON {path}: {error}") from error


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_copy(source: Path, destination: Path, mode: int = 0o600) -> None:
    if not source.is_file():
        raise RuntimeError(f"Không tìm thấy file credential: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temp_path = Path(temp_name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_profiles() -> dict[str, dict[str, Any]]:
    ensure_layout()
    raw = read_json(profiles_file(), {"version": 1, "accounts": {}})
    if not isinstance(raw, dict):
        raise RuntimeError(f"Registry không hợp lệ: {profiles_file()}")
    accounts = raw.get("accounts", {})
    if not isinstance(accounts, dict):
        raise RuntimeError(f"Registry accounts không hợp lệ: {profiles_file()}")
    return {
        str(account_id): dict(value)
        for account_id, value in accounts.items()
        if isinstance(value, dict)
    }


def save_profiles(accounts: dict[str, dict[str, Any]]) -> None:
    ordered = dict(sorted(accounts.items(), key=lambda item: item[0].casefold()))
    atomic_write_json(
        profiles_file(),
        {"version": 1, "accounts": ordered, "updated_at": now_iso()},
    )


def load_active() -> dict[str, Any] | None:
    value = read_json(active_file(), None)
    return value if isinstance(value, dict) else None


def profile_auth_path(profile: dict[str, Any]) -> Path:
    raw = profile.get("auth_file")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("Profile thiếu auth_file")
    return Path(raw).expanduser().resolve()


def validate_account_id(account_id: str) -> str:
    account_id = account_id.strip()
    if not ID_PATTERN.fullmatch(account_id):
        raise ValueError(
            "Account ID chỉ gồm chữ, số, dấu '.', '_' hoặc '-', tối đa 64 ký tự."
        )
    return account_id


def validate_email(email: str | None) -> str | None:
    if email is None:
        return None
    email = email.strip()
    if not email:
        return None
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError(f"Email không hợp lệ: {email}")
    return email


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None


def find_string_by_key(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        direct = value.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for child in value.values():
            result = find_string_by_key(child, key)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_string_by_key(child, key)
            if result:
                return result
    return None


def extract_email_from_auth(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    direct = find_string_by_key(data, "email")
    if direct:
        return direct

    id_token = find_string_by_key(data, "id_token")
    if not id_token:
        return None
    claims = decode_jwt_claims(id_token)
    return find_string_by_key(claims, "email") if claims else None


def next_account_id(accounts: dict[str, dict[str, Any]]) -> str:
    for index in range(1, 1000):
        candidate = f"account-{index:02d}"
        if candidate not in accounts:
            return candidate
    raise RuntimeError("Không thể tạo account ID tự động")


@contextmanager
def account_lock(blocking: bool = True) -> Iterator[None]:
    ensure_layout()
    descriptor = os.open(lock_file(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as error:
            raise RuntimeError(
                "Đang có Codex/account manager khác sử dụng credential chung. "
                "Hãy đóng phiên đó trước khi switch."
            ) from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def snapshot_active_auth(accounts: dict[str, dict[str, Any]]) -> None:
    """Save the shared auth back to the currently active profile.

    This also recovers refreshed credentials after a raw `codex` command that was
    launched after `codex-accounts switch`.
    """
    active = load_active()
    shared_auth = shared_auth_path()
    if not active or not shared_auth.is_file():
        return

    account_id = active.get("account_id")
    if not isinstance(account_id, str) or account_id not in accounts:
        return

    destination = profile_auth_path(accounts[account_id])
    atomic_copy(shared_auth, destination)


def seed_notification_database(profile: dict[str, Any]) -> None:
    path = usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_usage (
                account_id TEXT PRIMARY KEY,
                account_name TEXT NOT NULL,
                account_email TEXT,
                codex_home TEXT,
                primary_used_percent REAL,
                secondary_used_percent REAL,
                primary_reset_at TEXT,
                secondary_reset_at TEXT,
                primary_window_minutes REAL,
                secondary_window_minutes REAL,
                context_used_percent REAL,
                session_id TEXT,
                machine TEXT,
                updated_at TEXT NOT NULL,
                last_limit_at TEXT
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(account_usage)")
        }
        if "account_email" not in columns:
            connection.execute(
                "ALTER TABLE account_usage ADD COLUMN account_email TEXT"
            )
        connection.execute(
            """
            INSERT INTO account_usage (
                account_id, account_name, account_email, codex_home,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                account_name = excluded.account_name,
                account_email = COALESCE(excluded.account_email, account_usage.account_email),
                codex_home = excluded.codex_home
            """,
            (
                profile["id"],
                profile["name"],
                profile.get("email"),
                str(shared_codex_home()),
                now_iso(),
            ),
        )


def remove_from_notification_database(account_id: str) -> None:
    path = usage_db_path()
    if not path.exists():
        return
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute(
            "DELETE FROM account_usage WHERE account_id = ?", (account_id,)
        )


def activate_profile_locked(
    account_id: str,
    accounts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if account_id not in accounts:
        raise KeyError(f"Không tìm thấy tài khoản: {account_id}")

    snapshot_active_auth(accounts)
    profile = accounts[account_id]
    auth_file = profile_auth_path(profile)
    atomic_copy(auth_file, shared_auth_path())

    active = {
        "account_id": account_id,
        "account_name": profile["name"],
        "account_email": profile.get("email"),
        "profile_auth_file": str(auth_file),
        "shared_codex_home": str(shared_codex_home()),
        "activated_at": now_iso(),
    }
    atomic_write_json(active_file(), active)
    seed_notification_database(profile)
    return profile


def activate_profile(account_id: str) -> dict[str, Any]:
    accounts = load_profiles()
    with account_lock():
        return activate_profile_locked(account_id, accounts)


def select_account(
    accounts: dict[str, dict[str, Any]],
    prompt: str = "Chọn tài khoản",
) -> str:
    if not accounts:
        raise RuntimeError("Chưa có tài khoản. Chạy: codex-accounts add")

    active = load_active() or {}
    ids = list(sorted(accounts, key=lambda key: accounts[key]["name"].casefold()))
    print(prompt)
    for index, account_id in enumerate(ids, start=1):
        profile = accounts[account_id]
        marker = " *" if active.get("account_id") == account_id else ""
        email = f" <{profile['email']}>" if profile.get("email") else ""
        print(f"  {index}. {profile['name']} [{account_id}]{email}{marker}")

    while True:
        raw = input("Nhập số hoặc account ID (q để hủy): ").strip()
        if raw.casefold() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(ids):
                return ids[index - 1]
        if raw in accounts:
            return raw
        print("Lựa chọn không hợp lệ.")


def resolve_account_argument(account_id: str | None) -> str:
    accounts = load_profiles()
    if account_id:
        if account_id not in accounts:
            raise KeyError(f"Không tìm thấy tài khoản: {account_id}")
        return account_id
    return select_account(accounts)


def command_add(args: argparse.Namespace) -> int:
    ensure_layout()
    accounts = load_profiles()
    account_id = validate_account_id(args.id or next_account_id(accounts))
    if account_id in accounts:
        raise RuntimeError(f"Account ID đã tồn tại: {account_id}")

    codex_binary = shutil.which(args.codex_binary)
    if not codex_binary:
        raise RuntimeError(f"Không tìm thấy lệnh Codex: {args.codex_binary}")

    profile_dir = profiles_dir() / account_id
    auth_file = profile_dir / "auth.json"
    if profile_dir.exists() and any(profile_dir.iterdir()):
        raise RuntimeError(f"Thư mục profile đã có dữ liệu: {profile_dir}")
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Force file-based credential storage so each account has a portable auth.json.
    login_config = profile_dir / "config.toml"
    login_config.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
    os.chmod(login_config, 0o600)

    print(f"Đang thêm tài khoản mới: {account_id}")
    print("Trình duyệt/terminal sẽ yêu cầu bạn đăng nhập đúng tài khoản ChatGPT.")

    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(profile_dir)
    environment.pop("CODEX_ACCOUNT_ID", None)
    environment.pop("CODEX_ACCOUNT_NAME", None)
    environment.pop("CODEX_ACCOUNT_EMAIL", None)

    command = [codex_binary, "login"]
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"codex login thất bại, exit code {result.returncode}")
    if not auth_file.is_file():
        raise RuntimeError(
            "Đăng nhập hoàn tất nhưng không thấy auth.json. "
            "Hãy kiểm tra cli_auth_credentials_store hoặc phiên bản Codex."
        )
    os.chmod(auth_file, 0o600)

    email = validate_email(args.email) or extract_email_from_auth(auth_file)
    if not email and sys.stdin.isatty():
        entered = input("Không tự đọc được email. Nhập email tài khoản: ").strip()
        email = validate_email(entered)

    name = (args.name or email or account_id).strip()
    profile = {
        "id": account_id,
        "name": name,
        "email": email,
        "auth_file": str(auth_file),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    accounts[account_id] = profile
    save_profiles(accounts)
    seed_notification_database(profile)

    print(f"Đã thêm: {name} [{account_id}]")
    if email:
        print(f"Email: {email}")
    print("Tài khoản đã xuất hiện trong notification status với quota 'chưa đọc được'.")

    if args.activate:
        with account_lock():
            activate_profile_locked(account_id, accounts)
        print("Đã đặt làm tài khoản hiện tại.")
    else:
        print(f"Switch bằng: {APP_NAME} switch {account_id}")
    return 0


def read_usage_rows() -> dict[str, dict[str, Any]]:
    path = usage_db_path()
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM account_usage").fetchall()
        return {str(row["account_id"]): dict(row) for row in rows}
    except sqlite3.Error:
        return {}


def number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def account_icon(row: dict[str, Any] | None) -> str:
    if not row:
        return "⚪"
    values = [
        value
        for value in (
            number(row.get("primary_used_percent")),
            number(row.get("secondary_used_percent")),
        )
        if value is not None
    ]
    if not values:
        return "⚪"
    maximum = max(values)
    if maximum >= 95:
        return "🔴"
    if maximum >= 80:
        return "🟠"
    return "🟢"


def quota_summary(row: dict[str, Any] | None) -> str:
    if not row:
        return "chưa có usage"
    primary = number(row.get("primary_used_percent"))
    secondary = number(row.get("secondary_used_percent"))
    parts: list[str] = []
    if primary is not None:
        parts.append(f"chính {primary:.1f}% dùng/{100-primary:.1f}% còn")
    if secondary is not None:
        parts.append(f"phụ {secondary:.1f}% dùng/{100-secondary:.1f}% còn")
    return " · ".join(parts) if parts else "quota chưa đọc được"


def command_list(_args: argparse.Namespace) -> int:
    accounts = load_profiles()
    if not accounts:
        print("Chưa có tài khoản. Chạy: codex-accounts add")
        return 0
    active = load_active() or {}
    usage = read_usage_rows()
    print(f"Tài khoản Codex ({len(accounts)}):")
    for account_id in sorted(accounts, key=lambda key: accounts[key]["name"].casefold()):
        profile = accounts[account_id]
        marker = " ← hiện tại" if active.get("account_id") == account_id else ""
        email = profile.get("email") or "chưa rõ email"
        print(
            f"{account_icon(usage.get(account_id))} {profile['name']} [{account_id}]"
            f"{marker}\n   {email}\n   {quota_summary(usage.get(account_id))}"
        )
    return 0


def command_status(_args: argparse.Namespace) -> int:
    hook = hook_path()
    if hook.is_file():
        result = subprocess.run([sys.executable, str(hook), "--status"], check=False)
        return result.returncode
    print(f"Không tìm thấy hook: {hook}")
    return command_list(_args)


def command_current(_args: argparse.Namespace) -> int:
    active = load_active()
    if not active:
        print("Chưa chọn tài khoản hiện tại.")
        return 1
    print(f"{active.get('account_name')} [{active.get('account_id')}]")
    if active.get("account_email"):
        print(active["account_email"])
    print(f"Activated: {active.get('activated_at', 'unknown')}")
    return 0


def command_switch(args: argparse.Namespace) -> int:
    account_id = resolve_account_argument(args.account)
    profile = activate_profile(account_id)
    print(f"Đã switch sang: {profile['name']} [{account_id}]")
    if profile.get("email"):
        print(f"Email: {profile['email']}")
    print("Bây giờ có thể chạy trực tiếp:")
    print("  codex")
    print("  codex resume <SESSION_ID>")
    return 0


def codex_environment(profile: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(shared_codex_home())
    environment["CODEX_ACCOUNT_ID"] = str(profile["id"])
    environment["CODEX_ACCOUNT_NAME"] = str(profile["name"])
    if profile.get("email"):
        environment["CODEX_ACCOUNT_EMAIL"] = str(profile["email"])
    else:
        environment.pop("CODEX_ACCOUNT_EMAIL", None)
    environment["CODEX_USAGE_DB"] = str(usage_db_path())
    return environment


def run_codex_locked(
    account_id: str,
    codex_args: Sequence[str],
    codex_binary: str = "codex",
) -> int:
    binary = shutil.which(codex_binary)
    if not binary:
        raise RuntimeError(f"Không tìm thấy lệnh Codex: {codex_binary}")

    accounts = load_profiles()
    with account_lock():
        profile = activate_profile_locked(account_id, accounts)
        print(
            f"Đang dùng {profile['name']} [{account_id}]"
            + (f" <{profile['email']}>" if profile.get("email") else ""),
            file=sys.stderr,
        )
        try:
            completed = subprocess.run(
                [binary, *codex_args],
                env=codex_environment(profile),
                check=False,
            )
            return completed.returncode
        finally:
            # Persist refreshed tokens back into the account profile.
            snapshot_active_auth(accounts)


def strip_separator(values: list[str]) -> list[str]:
    return values[1:] if values and values[0] == "--" else values


def command_run(args: argparse.Namespace) -> int:
    account_id = resolve_account_argument(args.account)
    return run_codex_locked(
        account_id,
        strip_separator(args.codex_args),
        args.codex_binary,
    )


def command_resume(args: argparse.Namespace) -> int:
    account_id = resolve_account_argument(args.account)
    codex_args = ["resume", args.session_id, *strip_separator(args.prompt)]
    return run_codex_locked(account_id, codex_args, args.codex_binary)


def command_rename(args: argparse.Namespace) -> int:
    accounts = load_profiles()
    if args.account not in accounts:
        raise KeyError(f"Không tìm thấy tài khoản: {args.account}")
    new_name = args.new_name.strip()
    if not new_name:
        raise ValueError("Tên mới không được để trống")
    accounts[args.account]["name"] = new_name
    accounts[args.account]["updated_at"] = now_iso()
    save_profiles(accounts)
    seed_notification_database(accounts[args.account])

    active = load_active()
    if active and active.get("account_id") == args.account:
        active["account_name"] = new_name
        atomic_write_json(active_file(), active)
    print(f"Đã đổi tên {args.account} thành: {new_name}")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    accounts = load_profiles()
    if args.account not in accounts:
        raise KeyError(f"Không tìm thấy tài khoản: {args.account}")

    active = load_active()
    is_active = bool(active and active.get("account_id") == args.account)
    if is_active and not args.force:
        raise RuntimeError(
            "Không xóa tài khoản đang active. Switch sang tài khoản khác trước, "
            "hoặc dùng --force để logout credential chung."
        )

    profile = accounts[args.account]
    if not args.yes and sys.stdin.isatty():
        answer = input(
            f"Xóa {profile['name']} [{args.account}] khỏi manager? [y/N]: "
        ).strip().casefold()
        if answer not in {"y", "yes"}:
            print("Đã hủy.")
            return 0

    with account_lock():
        if is_active:
            try:
                shared_auth_path().unlink(missing_ok=True)
                active_file().unlink(missing_ok=True)
            except OSError as error:
                raise RuntimeError(f"Không xóa được active credential: {error}") from error
        profile_dir = profile_auth_path(profile).parent
        shutil.rmtree(profile_dir, ignore_errors=False)
        del accounts[args.account]
        save_profiles(accounts)
        remove_from_notification_database(args.account)

    print(f"Đã xóa tài khoản: {args.account}")
    return 0


def command_telegram(_args: argparse.Namespace) -> int:
    hook = hook_path()
    if not hook.is_file():
        raise RuntimeError(f"Không tìm thấy Telegram hook: {hook}")
    return subprocess.run(
        [sys.executable, str(hook), "--send-status"], check=False
    ).returncode


def command_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    binary = shutil.which(args.codex_binary)
    checks.append(("Codex binary", binary is not None, binary or "không tìm thấy"))
    checks.append(
        ("Shared CODEX_HOME", shared_codex_home().is_dir(), str(shared_codex_home()))
    )
    checks.append(("Profiles registry", profiles_file().is_file(), str(profiles_file())))
    checks.append(("Active metadata", active_file().is_file(), str(active_file())))
    checks.append(("Telegram hook", hook_path().is_file(), str(hook_path())))
    checks.append(("Usage database", usage_db_path().is_file(), str(usage_db_path())))

    config_path = shared_codex_home() / "config.toml"
    config_text = ""
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        pass
    hook_configured = "telegram_notify.py" in config_text or "notify" in config_text
    checks.append(("Hook trong config.toml", hook_configured, str(config_path)))

    failed = False
    for label, ok, detail in checks:
        print(f"{'OK' if ok else 'WARN':4}  {label}: {detail}")
        failed = failed or not ok

    try:
        accounts = load_profiles()
        for account_id, profile in accounts.items():
            auth_ok = profile_auth_path(profile).is_file()
            print(
                f"{'OK' if auth_ok else 'FAIL':4}  Auth {account_id}: "
                f"{profile_auth_path(profile)}"
            )
            failed = failed or not auth_ok
    except Exception as error:
        print(f"FAIL  Registry: {error}")
        failed = True

    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            "Quản lý nhiều tài khoản Codex với switch thủ công và session chung."
        ),
    )
    parser.add_argument(
        "--codex-binary",
        default=os.environ.get("CODEX_BINARY", "codex"),
        help="Tên hoặc đường dẫn Codex binary (mặc định: codex)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="Đăng nhập và thêm tài khoản mới")
    add.add_argument("--id", help="ID ổn định, ví dụ account-01")
    add.add_argument("--name", help="Tên hiển thị")
    add.add_argument("--email", help="Email nếu không đọc được từ auth token")
    add.add_argument(
        "--activate", action="store_true", help="Switch sang account sau khi thêm"
    )
    add.set_defaults(handler=command_add)

    list_command = subparsers.add_parser("list", aliases=["ls"], help="Liệt kê account")
    list_command.set_defaults(handler=command_list)

    status = subparsers.add_parser("status", help="Xem status theo Telegram hook")
    status.set_defaults(handler=command_status)

    current = subparsers.add_parser("current", help="Xem account hiện tại")
    current.set_defaults(handler=command_current)

    switch = subparsers.add_parser("switch", aliases=["use"], help="Switch thủ công")
    switch.add_argument("account", nargs="?", help="Bỏ trống để chọn bằng menu")
    switch.set_defaults(handler=command_switch)

    run = subparsers.add_parser("run", help="Chọn account rồi chạy Codex")
    run.add_argument("account", nargs="?", help="Bỏ trống để chọn bằng menu")
    run.add_argument("codex_args", nargs=argparse.REMAINDER)
    run.set_defaults(handler=command_run)

    resume = subparsers.add_parser("resume", help="Resume session bằng account đã chọn")
    resume.add_argument("session_id")
    resume.add_argument("--account", "-a", help="Bỏ trống để chọn bằng menu")
    resume.add_argument("prompt", nargs="*")
    resume.set_defaults(handler=command_resume)

    rename = subparsers.add_parser("rename", help="Đổi tên account")
    rename.add_argument("account")
    rename.add_argument("new_name")
    rename.set_defaults(handler=command_rename)

    remove = subparsers.add_parser("remove", aliases=["rm"], help="Xóa account")
    remove.add_argument("account")
    remove.add_argument("--yes", "-y", action="store_true")
    remove.add_argument("--force", action="store_true")
    remove.set_defaults(handler=command_remove)

    telegram = subparsers.add_parser("telegram", help="Gửi status lên Telegram")
    telegram.set_defaults(handler=command_telegram)

    doctor = subparsers.add_parser("doctor", help="Kiểm tra setup")
    doctor.set_defaults(handler=command_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\nĐã hủy.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, KeyError, OSError, sqlite3.Error) as error:
        message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
        print(f"Lỗi: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())