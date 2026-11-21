"""
Slack via the Web API with a token (SLACK_TOKEN): a user token (xoxp-) posts as you, a bot token (xoxb-) posts as
the app. Needed scopes: chat:write, channels:read, groups:read, im:read, im:write, users:read, users:read.email,
channels:history, groups:history, im:history.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://slack.com/api/"


class SlackError(Exception):
    pass


class Slack:
    def __init__(self, token: str = "", http: httpx.Client | None = None, token_fn=None) -> None:
        self.token = token
        self._token_fn = token_fn          # connected account: refreshed token on demand
        self._http = http or httpx.Client(timeout=20)

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        token = self._token_fn() if self._token_fn else self.token
        r = self._http.post(API + method, headers={"Authorization": f"Bearer {token}"}, data=params)
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"Slack {method}: {data.get('error', r.status_code)}")
        return data

    def _channel_id(self, target: str) -> str:
        t = target.strip()
        if t.startswith(("C", "G", "D")) and t[1:].isalnum() and t.isupper():
            return t
        if t.startswith("#"):
            name = t[1:].lower()
            cursor = ""
            for _ in range(10):
                d = self._call("conversations.list", types="public_channel,private_channel", limit=500,
                               exclude_archived="true", cursor=cursor)
                for c in d.get("channels", []):
                    if c.get("name", "").lower() == name:
                        return c["id"]
                cursor = (d.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor:
                    break
            raise SlackError(f"channel {t} not found (or the app is not in it)")
        # a person: by email or by name -> open a DM
        uid = None
        if "@" in t and not t.startswith("@"):
            uid = self._call("users.lookupByEmail", email=t)["user"]["id"]
        else:
            name = t.lstrip("@").lower()
            for u in self._call("users.list", limit=1000).get("members", []):
                prof = u.get("profile", {})
                if name in (u.get("name", "").lower(), prof.get("display_name", "").lower(),
                            prof.get("real_name", "").lower()):
                    uid = u["id"]
                    break
        if not uid:
            raise SlackError(f"nobody called {t!r} in this Slack workspace")
        return self._call("conversations.open", users=uid)["channel"]["id"]

    def send(self, to: str, text: str) -> str:
        self._call("chat.postMessage", channel=self._channel_id(to), text=text)
        return f"Sent a Slack message to {to}"

    def read(self, to: str, limit: int = 10) -> list[dict[str, str]]:
        d = self._call("conversations.history", channel=self._channel_id(to), limit=max(1, min(limit, 50)))
        return [{"from": m.get("user", m.get("username", "?")), "date": m.get("ts", ""),
                 "text": " ".join(str(m.get("text", "")).split())[:400]} for m in d.get("messages", [])]
