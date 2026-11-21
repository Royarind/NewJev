"""
One-click connections: Slack, Microsoft 365 (Teams / calendar / OneDrive / SharePoint) and Gmail.

The user clicks Connect, signs in in their browser, picks the workspace / account, and is done. This is standard
OAuth with PKCE and a redirect back to NewJev on this PC (http://localhost:<port>/oauth/<provider>/callback), so no
secrets are pasted and no app has to be created by the user.

The PUBLISHER of NewJev registers one app per provider once and puts the public client ids into
app/config/connectors.json (or .env). See README "Connectors (publisher setup)".
Tokens are stored per user in data/connections/ and refreshed automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CONNECTORS_FILE = Path(__file__).parent / "config" / "connectors.json"

SLACK_USER_SCOPES = ("chat:write channels:read groups:read im:read im:write mpim:read channels:history "
                     "groups:history im:history mpim:history users:read users:read.email")
MS_SCOPES = ("offline_access User.Read Chat.ReadWrite ChannelMessage.Send ChannelMessage.Read.All Team.ReadBasic.All "
             "Channel.ReadBasic.All Calendars.ReadWrite Files.ReadWrite.All Sites.Read.All")
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"

_pending: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


class ConnectError(Exception):
    pass


# ------------------------------------------------------------------ config
def client_config() -> dict[str, str]:
    """Client ids: .env wins, then the publisher's app/config/connectors.json."""
    from .config import get_settings
    s = get_settings()
    try:
        shipped = json.loads(CONNECTORS_FILE.read_text(encoding="utf-8"))
    except Exception:
        shipped = {}
    pick = lambda env, key: (env or str(shipped.get(key) or "")).strip()  # noqa: E731
    return {
        "slack_client_id": pick(getattr(s, "slack_client_id", ""), "slack_client_id"),
        "microsoft_client_id": pick(s.graph_client_id, "microsoft_client_id"),
        "microsoft_tenant": pick(s.graph_tenant if s.graph_tenant != "organizations" else "", "microsoft_tenant")
        or "organizations",
        "google_client_id": pick(getattr(s, "google_client_id", ""), "google_client_id"),
        "google_client_secret": pick(getattr(s, "google_client_secret", ""), "google_client_secret"),
    }


def _dir() -> Path:
    from .config import get_settings
    return Path(get_settings().connections_path)


def token_path(provider: str) -> Path:
    if provider == "microsoft":
        from .config import get_settings
        return Path(get_settings().graph_token_path)       # the Graph client reads it from here
    return _dir() / f"{provider}.json"


def load_token(provider: str) -> dict[str, Any] | None:
    try:
        return json.loads(token_path(provider).read_text(encoding="utf-8"))
    except Exception:
        return None


def save_token(provider: str, tok: dict[str, Any]) -> None:
    p = token_path(provider)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=1), encoding="utf-8")
    tmp.replace(p)


def disconnect(provider: str) -> bool:
    p = token_path(provider)
    if p.exists():
        p.unlink()
        return True
    return False


# ------------------------------------------------------------------- oauth
def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def redirect_uri(provider: str, port: int) -> str:
    host = "127.0.0.1" if provider == "google" else "localhost"    # what each provider accepts for desktop apps
    return f"http://{host}:{port}/oauth/{provider}/callback"


def start(provider: str, port: int) -> str:
    """Authorize URL to open in the browser."""
    cfg = client_config()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    ru = redirect_uri(provider, port)
    if provider == "slack":
        if not cfg["slack_client_id"]:
            raise ConnectError("Slack is not set up in this NewJev build (no Slack client id)")
        q = {"client_id": cfg["slack_client_id"], "user_scope": SLACK_USER_SCOPES, "redirect_uri": ru,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        url = "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(q)
    elif provider == "microsoft":
        if not cfg["microsoft_client_id"]:
            raise ConnectError("Microsoft 365 is not set up in this NewJev build (no Microsoft client id)")
        q = {"client_id": cfg["microsoft_client_id"], "response_type": "code", "redirect_uri": ru,
             "response_mode": "query", "scope": MS_SCOPES, "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", "prompt": "select_account"}
        url = (f"https://login.microsoftonline.com/{cfg['microsoft_tenant']}/oauth2/v2.0/authorize?"
               + urllib.parse.urlencode(q))
    elif provider == "google":
        if not cfg["google_client_id"]:
            raise ConnectError("Gmail is not set up in this NewJev build (no Google client id)")
        q = {"client_id": cfg["google_client_id"], "response_type": "code", "redirect_uri": ru, "scope": GOOGLE_SCOPES,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             "access_type": "offline", "prompt": "consent select_account"}
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)
    else:
        raise ConnectError(f"unknown provider {provider!r}")
    with _lock:
        for k in [k for k, v in _pending.items() if v["t"] < time.time() - 900]:
            _pending.pop(k, None)
        _pending[state] = {"provider": provider, "verifier": verifier, "redirect_uri": ru, "t": time.time()}
    return url


def finish(provider: str, code: str, state: str, http: httpx.Client | None = None) -> str:
    """Exchange the code for tokens. Returns a human description of what got connected."""
    with _lock:
        p = _pending.pop(state, None)
    if p is None or p["provider"] != provider or p["t"] < time.time() - 900:
        raise ConnectError("this sign-in link expired or was not started by NewJev — click Connect again")
    cfg = client_config()
    http = http or httpx.Client(timeout=30)
    if provider == "slack":
        r = http.post("https://slack.com/api/oauth.v2.access", data={
            "client_id": cfg["slack_client_id"], "code": code, "code_verifier": p["verifier"],
            "redirect_uri": p["redirect_uri"]}).json()
        if not r.get("ok"):
            raise ConnectError(f"Slack said: {r.get('error')}")
        u = r.get("authed_user") or {}
        tok = {"access_token": u.get("access_token"), "refresh_token": u.get("refresh_token"),
               "expires_at": time.time() + int(u["expires_in"]) - 120 if u.get("expires_in") else None,
               "team": (r.get("team") or {}).get("name", ""), "team_id": (r.get("team") or {}).get("id", ""),
               "user_id": u.get("id", "")}
        if not tok["access_token"]:
            raise ConnectError("Slack returned no user token")
        save_token("slack", tok)
        return f"Slack workspace {tok['team'] or tok['team_id']}"
    if provider == "microsoft":
        r = http.post(f"https://login.microsoftonline.com/{cfg['microsoft_tenant']}/oauth2/v2.0/token", data={
            "client_id": cfg["microsoft_client_id"], "grant_type": "authorization_code", "code": code,
            "redirect_uri": p["redirect_uri"], "code_verifier": p["verifier"], "scope": MS_SCOPES})
        body = r.json()
        if r.status_code != 200:
            raise ConnectError(body.get("error_description", body.get("error", "sign-in failed"))[:300])
        body["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 120
        try:
            me = http.get("https://graph.microsoft.com/v1.0/me",
                          headers={"Authorization": f"Bearer {body['access_token']}"}).json()
            body["account"] = me.get("mail") or me.get("userPrincipalName") or me.get("displayName", "")
        except Exception:
            body["account"] = ""
        save_token("microsoft", body)
        return f"Microsoft 365 account {body['account']}".strip()
    if provider == "google":
        r = http.post("https://oauth2.googleapis.com/token", data={
            "client_id": cfg["google_client_id"], "client_secret": cfg["google_client_secret"], "code": code,
            "code_verifier": p["verifier"], "grant_type": "authorization_code", "redirect_uri": p["redirect_uri"]})
        body = r.json()
        if r.status_code != 200:
            raise ConnectError(body.get("error_description", body.get("error", "sign-in failed"))[:300])
        body["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 120
        try:
            info = http.get("https://www.googleapis.com/oauth2/v3/userinfo",
                            headers={"Authorization": f"Bearer {body['access_token']}"}).json()
            body["account"] = info.get("email", "")
        except Exception:
            body["account"] = ""
        save_token("google", body)
        return f"Gmail account {body['account']}".strip()
    raise ConnectError(f"unknown provider {provider!r}")


def access_token(provider: str, http: httpx.Client | None = None) -> str:
    """A valid access token, refreshed when needed (Slack token rotation, Google refresh tokens)."""
    tok = load_token(provider)
    if not tok or not tok.get("access_token"):
        raise ConnectError(f"{provider} is not connected — open Connections and click Connect")
    if not tok.get("expires_at") or tok["expires_at"] > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise ConnectError(f"the {provider} sign-in expired — open Connections and connect again")
    cfg = client_config()
    http = http or httpx.Client(timeout=30)
    if provider == "slack":
        r = http.post("https://slack.com/api/oauth.v2.access", data={
            "client_id": cfg["slack_client_id"], "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"]}).json()
        if not r.get("ok"):
            raise ConnectError(f"Slack sign-in expired ({r.get('error')}) — connect again")
        u = r.get("authed_user") or r
        tok.update(access_token=u.get("access_token"), refresh_token=u.get("refresh_token", tok["refresh_token"]),
                   expires_at=time.time() + int(u.get("expires_in", 43200)) - 120)
    elif provider == "google":
        r = http.post("https://oauth2.googleapis.com/token", data={
            "client_id": cfg["google_client_id"], "client_secret": cfg["google_client_secret"],
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        if r.status_code != 200:
            raise ConnectError("Google sign-in expired — connect Gmail again")
        b = r.json()
        tok.update(access_token=b["access_token"], expires_at=time.time() + int(b.get("expires_in", 3600)) - 120)
    else:
        raise ConnectError(f"cannot refresh {provider}")
    save_token(provider, tok)
    return tok["access_token"]


# ------------------------------------------------------------------ status
def status() -> list[dict[str, Any]]:
    cfg = client_config()
    out = []
    try:
        from .shortcuts.mail import outlook_available
        outlook = outlook_available()
    except Exception:
        outlook = False
    out.append({"id": "outlook", "name": "Outlook (desktop)", "what": "email, calendar", "connected": outlook,
                "account": "this PC's Outlook" if outlook else "", "available": True, "automatic": True,
                "note": "" if outlook else "Install / open classic Outlook and sign in to use it."})
    for pid, name, what, key in (("slack", "Slack", "messages", "slack_client_id"),
                                 ("microsoft", "Microsoft 365", "Teams, calendar, OneDrive, SharePoint",
                                  "microsoft_client_id"),
                                 ("google", "Gmail", "email", "google_client_id")):
        tok = load_token(pid) or {}
        out.append({"id": pid, "name": name, "what": what, "available": bool(cfg[key]),
                    "connected": bool(tok.get("access_token")),
                    "account": tok.get("team") or tok.get("account") or "",
                    "note": "" if cfg[key] else "Not set up in this build (the publisher adds the client id)."})
    return out
