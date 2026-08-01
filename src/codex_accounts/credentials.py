"""Reading ~/.codex/auth.json and pulling an identity out of it.

Codex keeps the whole login in this one file - there is no keychain and no
second file holding "who am I", which is what makes switching a single write.

    {
      "auth_mode": "chatgpt",
      "OPENAI_API_KEY": null,
      "tokens": {"id_token": …, "access_token": …,
                 "refresh_token": …, "account_id": …},
      "last_refresh": "2026-08-01T04:41:30.573360300Z"
    }
"""

import base64
import binascii
import hashlib
import json
import os

from .jsonio import read_json, write_json
from .paths import auth_path, codex_dir

# The claim namespace OpenAI puts its own fields under, in both the id_token
# and the access_token.
AUTH_CLAIM = "https://api.openai.com/auth"


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------

def read_auth():
    """The live auth.json as a dict, or None if not logged in."""
    return read_json(auth_path())


def write_auth(auth) -> None:
    os.makedirs(codex_dir(), exist_ok=True)
    write_json(auth_path(), auth, private=True)


def tokens(auth):
    """The `tokens` sub-object, or {} if absent (API-key logins have none)."""
    if isinstance(auth, dict):
        blk = auth.get("tokens")
        if isinstance(blk, dict):
            return blk
    return {}


# --------------------------------------------------------------------------
# JWTs
# --------------------------------------------------------------------------

def decode_jwt(token):
    """The claims of a JWT, without verifying the signature.

    Verification would need the issuer's public keys and buys nothing here:
    the token came out of a 0600 file on this machine, and every use of it is
    checked by the server anyway. This only reads what is already ours.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def access_expires_at(auth):
    """Epoch seconds at which the access token expires, or None.

    Read from the token's own `exp` claim rather than from a stored field:
    Codex refreshes auth.json behind our back, so the token is the only thing
    guaranteed to still be describing itself accurately.
    """
    claims = decode_jwt(tokens(auth).get("access_token")) or {}
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def identity(auth):
    """Who this auth.json belongs to.

    Returns a dict with userId / accountId / email / planType /
    organizationName / authMode. Any of them may be None.
    """
    auth = auth or {}
    mode = auth.get("auth_mode") or ("apikey" if auth.get("OPENAI_API_KEY")
                                     else None)
    blk = tokens(auth)
    claims = decode_jwt(blk.get("id_token")) or {}
    scoped = claims.get(AUTH_CLAIM) or {}

    org = None
    for entry in scoped.get("organizations") or []:
        if isinstance(entry, dict) and entry.get("title"):
            org = entry["title"]
            if entry.get("is_default"):
                break

    return {
        "authMode": mode,
        # chatgpt_user_id is the person. account_id is the *workspace*, which
        # every member of a team shares - see user_key() below.
        "userId": scoped.get("chatgpt_user_id") or claims.get("sub"),
        "accountId": scoped.get("chatgpt_account_id") or blk.get("account_id"),
        "email": claims.get("email"),
        "planType": scoped.get("chatgpt_plan_type"),
        "organizationName": org,
    }


def user_key(auth):
    """The stable key identifying an account. None if it cannot be determined.

    Deliberately *not* `tokens.account_id`: that is the workspace id, and two
    members of the same team share it. Keying on it would make them
    indistinguishable, so a switch would auto-save over the wrong profile and
    destroy the other one's credentials.

    API-key logins carry no user at all, so they are keyed by a digest of the
    key itself - enough to tell two of them apart without storing it twice.
    """
    ident = identity(auth)
    if ident["userId"]:
        return ident["userId"]

    api_key = (auth or {}).get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        return f"apikey:{digest}"
    return None
