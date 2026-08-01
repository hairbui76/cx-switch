"""Talking to OpenAI's OAuth and Codex usage endpoints.

Usage is read straight from the API with each account's stored token, so
reporting on every account needs no switching at all.

Both URLs were confirmed against a live account:

    GET  https://chatgpt.com/backend-api/wham/usage
    POST https://auth.openai.com/oauth/token

`/backend-api/codex/usage` is an alias serving the same payload.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .credentials import (access_expires_at, read_auth, tokens, write_auth)
from .store import current_user_key, save_account
from .term import CliError, debug, dim, green, red, yellow

# Taken from the `aud` claim of a Codex id_token.
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# Codex identifies itself with these on every call; the usage endpoint is
# reachable without them but there is no reason to look like anything else.
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "codex_cli_rs/0.0.0"

# Refresh a little early so a token cannot expire mid-request.
EXPIRY_MARGIN_S = 60


class ApiError(CliError):
    """An HTTP call failed. Carries enough context to say *which* one."""

    def __init__(self, message, stage=None, url=None, status=None, body=None):
        super().__init__(message)
        self.stage = stage
        self.url = url
        self.status = status
        self.body = body


def _detail(body: str):
    """(message, code) pulled out of whichever error shape came back.

    OpenAI answers with {"error": {"message", "code"}} from the auth host and
    a bare {"detail": …} from the ChatGPT backend.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.strip()[:120], None
    if not isinstance(parsed, dict):
        return body.strip()[:120], None

    error = parsed.get("error")
    if isinstance(error, dict):
        return error.get("message") or "", error.get("code")
    if isinstance(error, str):
        return error, parsed.get("error_description")
    if isinstance(parsed.get("detail"), str):
        return parsed["detail"], None
    return "", None


def _explain(stage: str, status: int, body: str) -> str:
    """Turn an HTTP failure into something the user can act on."""
    message, code = _detail(body)

    if code == "refresh_token_invalidated" or (
            stage == "refresh" and status in (400, 401)):
        return "session ended - run `codex login` on this account"
    if stage == "usage" and status == 401:
        return "token rejected - log in again on this account"
    if stage == "usage" and status == 403:
        return "usage forbidden for this account"
    if status == 429:
        return "rate limited - try again shortly"

    return f"{stage}: HTTP {status}{' - ' + message if message else ''}"


def http_json(url: str, token=None, account_id=None, payload=None,
              timeout: int = 20, stage: str = "request"):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "originator": ORIGINATOR,
    }
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if account_id:
        headers["chatgpt-account-id"] = account_id
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    debug(f"{'POST' if body else 'GET '} {url}  ({stage})")
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            debug(f"  -> {resp.status} {len(text)}B")
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="ignore")
        debug(f"  -> {exc.code} {text[:400]}")
        raise ApiError(_explain(stage, exc.code, text),
                       stage=stage, url=url, status=exc.code, body=text)
    except urllib.error.URLError as exc:
        debug(f"  -> network error: {exc.reason}")
        raise ApiError(f"{stage}: network error - {exc.reason}",
                       stage=stage, url=url)


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def refresh_tokens(blk):
    """Exchange a refresh token for a new one. Returns a new tokens dict."""
    refresh = blk.get("refresh_token")
    if not refresh:
        raise CliError("no refresh token stored - run `codex login`")

    data = http_json(TOKEN_URL, stage="refresh", payload={
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": "openid profile email",
    })

    new = dict(blk)
    new["access_token"] = data["access_token"]
    if data.get("id_token"):
        new["id_token"] = data["id_token"]
    # Whether OpenAI rotates the refresh token is not documented, so take a
    # new one whenever it appears. Dropping a rotated token would leave the
    # stored copy unable to refresh ever again.
    if data.get("refresh_token"):
        new["refresh_token"] = data["refresh_token"]
    return new


def _fresh(auth) -> bool:
    expires_at = access_expires_at(auth)
    if expires_at is None:
        return False
    return expires_at > time.time() + EXPIRY_MARGIN_S


def _utc_stamp() -> str:
    """The shape Codex writes into `last_refresh`."""
    return (datetime.now(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="microseconds") + "Z")


def token_for(data):
    """A usable access token, refreshed and persisted if it has expired."""
    is_current = data.get("userKey") == current_user_key()

    # Codex refreshes the live auth.json on its own, so for the active account
    # it is at least as fresh as our stored copy - and often newer.
    if is_current:
        live = read_auth()
        if _fresh(live):
            debug(f"{data.get('name')}: using live auth.json")
            return tokens(live)["access_token"]

    auth = data.get("auth") or {}
    blk = tokens(auth)
    if not blk.get("access_token"):
        raise CliError("no access token stored - run `codex login`")
    if _fresh(auth):
        debug(f"{data.get('name')}: stored token still valid")
        return blk["access_token"]

    debug(f"{data.get('name')}: token expired, refreshing")
    auth = dict(auth)
    auth["tokens"] = refresh_tokens(blk)
    auth["last_refresh"] = _utc_stamp()
    data["auth"] = auth
    save_account(data)
    if is_current:
        write_auth(auth)
    return auth["tokens"]["access_token"]


def fetch_usage(token, account_id=None):
    return http_json(USAGE_URL, token=token, account_id=account_id,
                     stage="usage")


def usage_for(data):
    """Usage for a saved account, refreshing its token if need be."""
    token = token_for(data)
    account_id = tokens(data.get("auth") or {}).get("account_id")
    return fetch_usage(token, account_id)


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def windows(usage):
    """(primary, secondary) rate-limit windows, either of which may be None."""
    limit = (usage or {}).get("rate_limit") or {}
    return limit.get("primary_window"), limit.get("secondary_window")


def fmt_span(seconds) -> str:
    """604800 -> '7d'. Labels a window by how long it is."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def fmt_reset(epoch) -> str:
    """'Wed 09:16 (6d23h)' for an epoch-second reset timestamp.

    Codex returns an integer here, unlike Anthropic's ISO-8601 string.
    """
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return ""
    when = datetime.fromtimestamp(epoch).astimezone()
    minutes = int((when - datetime.now().astimezone()).total_seconds() // 60)
    if minutes < 0:
        return "now"
    if minutes < 60:
        rel = f"{minutes}m"
    elif minutes < 60 * 24:
        rel = f"{minutes // 60}h{minutes % 60:02d}m"
    else:
        rel = f"{minutes // 1440}d{(minutes % 1440) // 60}h"
    return f"{when.strftime('%a %H:%M')} ({rel})"


def fmt_pct(window) -> str:
    """A colour-coded utilisation percentage."""
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return dim("  -%")
    pct = int(round(float(window["used_percent"])))
    text = f"{pct:3d}%"
    if pct >= 90:
        return red(text)
    if pct >= 70:
        return yellow(text)
    return green(text)


def fmt_window(window) -> str:
    """'  3% (7d) Wed 09:16 (6d23h)', or a dim dash when the plan has none."""
    if not isinstance(window, dict):
        return dim("-")
    span = dim(f"({fmt_span(window.get('limit_window_seconds'))})")
    reset = fmt_reset(window.get("reset_at"))
    return f"{fmt_pct(window)} {span} {dim(reset)}".rstrip()


def fmt_credits(usage) -> str:
    credits = (usage or {}).get("credits") or {}
    if credits.get("unlimited"):
        return green("unlimited")
    balance = credits.get("balance")
    if balance is not None:
        return str(balance)
    return dim("-") if not credits.get("has_credits") else "yes"
