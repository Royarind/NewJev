"""
Microsoft 365 via Microsoft Graph: Teams chat / channel messages, calendar, OneDrive / SharePoint files.

Setup (once): register an app in Entra ID (Azure portal) as a public client ("Allow public client flows" = Yes),
put its Application (client) ID in GRAPH_CLIENT_ID, then run `python tools\\connect_microsoft.py` and sign in with
the code shown. Tokens are kept in GRAPH_TOKEN_PATH and refreshed automatically. Only plain HTTPS (httpx) is used.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ("offline_access User.Read Chat.ReadWrite ChannelMessage.Send ChannelMessage.Read.All Team.ReadBasic.All "
          "Channel.ReadBasic.All Calendars.ReadWrite Files.ReadWrite.All Sites.Read.All")


class GraphError(Exception):
    pass


class Graph:
    def __init__(self, client_id: str, tenant: str = "organizations", token_path: str = "data/graph_token.json",
                 http: httpx.Client | None = None) -> None:
        self.client_id, self.tenant, self.token_path = client_id, tenant or "organizations", Path(token_path)
        self._http = http or httpx.Client(timeout=30)

    # ------------------------------------------------------------- auth
    @property
    def _auth(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0"

    def connected(self) -> bool:
        return bool(self.client_id) and self.token_path.exists()

    def device_login(self, show: Callable[[str], None] = print, timeout_s: int = 900) -> None:
        r = self._http.post(f"{self._auth}/devicecode", data={"client_id": self.client_id, "scope": SCOPES})
        if r.status_code != 200:
            raise GraphError(f"device code request failed: {r.text[:300]}")
        d = r.json()
        show(d.get("message") or f"Open {d['verification_uri']} and enter {d['user_code']}")
        deadline = time.time() + min(timeout_s, int(d.get("expires_in", 900)))
        while time.time() < deadline:
            time.sleep(int(d.get("interval", 5)))
            t = self._http.post(f"{self._auth}/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": self.client_id,
                "device_code": d["device_code"]})
            body = t.json()
            if t.status_code == 200:
                self._store(body)
                return
            if body.get("error") not in ("authorization_pending", "slow_down"):
                raise GraphError(body.get("error_description", body.get("error", "sign-in failed"))[:300])
        raise GraphError("sign-in timed out")

    def _store(self, body: dict[str, Any]) -> None:
        body["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 120
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(body), encoding="utf-8")

    def _token(self) -> str:
        if not self.connected():
            raise GraphError("Microsoft 365 is not connected — open Connections in NewJev and click Connect")
        tok = json.loads(self.token_path.read_text(encoding="utf-8"))
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        r = self._http.post(f"{self._auth}/token", data={"grant_type": "refresh_token", "client_id": self.client_id,
                                                         "refresh_token": tok.get("refresh_token", ""),
                                                         "scope": SCOPES})
        if r.status_code != 200:
            raise GraphError("Microsoft sign-in expired — open Connections in NewJev and connect again")
        body = r.json()
        body.setdefault("refresh_token", tok.get("refresh_token"))
        self._store(body)
        return body["access_token"]

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        url = path if path.startswith("http") else GRAPH + path
        r = self._http.request(method, url, headers={"Authorization": f"Bearer {self._token()}",
                                                     **kw.pop("headers", {})}, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except Exception:
                msg = r.text
            raise GraphError(f"Graph {method} {path.split('?')[0]} -> {r.status_code}: {str(msg)[:300]}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else r.content

    # ------------------------------------------------------------ teams
    def _find_chat(self, who: str) -> str:
        chats = self._req("GET", "/me/chats?$expand=members&$top=50").get("value", [])
        w = who.lower()
        for c in chats:
            names = [str(m.get("displayName", "")) + " " + str(m.get("email", "")) for m in c.get("members", [])]
            if (c.get("topic") or "").lower() == w or any(w in n.lower() for n in names):
                return c["id"]
        raise GraphError(f"no Teams chat with {who!r} found (start one in Teams first)")

    def _find_channel(self, team: str, channel: str) -> tuple[str, str]:
        teams = self._req("GET", "/me/joinedTeams").get("value", [])
        t = next((x for x in teams if x.get("displayName", "").lower() == team.lower()), None)
        if t is None:
            raise GraphError(f"team {team!r} not found; your teams: {', '.join(x['displayName'] for x in teams)}")
        chans = self._req("GET", f"/teams/{t['id']}/channels").get("value", [])
        c = next((x for x in chans if x.get("displayName", "").lower() == channel.lower()), None)
        if c is None:
            raise GraphError(f"channel {channel!r} not found in {team}")
        return t["id"], c["id"]

    def teams_send(self, to: str = "", text: str = "", team: str = "", channel: str = "") -> str:
        body = {"body": {"contentType": "text", "content": text}}
        if team and channel:
            tid, cid = self._find_channel(team, channel)
            self._req("POST", f"/teams/{tid}/channels/{cid}/messages", json=body)
            return f"Posted to Teams {team} / {channel}"
        self._req("POST", f"/chats/{self._find_chat(to)}/messages", json=body)
        return f"Sent a Teams message to {to}"

    def teams_read(self, to: str = "", team: str = "", channel: str = "", limit: int = 10) -> list[dict[str, str]]:
        if team and channel:
            tid, cid = self._find_channel(team, channel)
            items = self._req("GET", f"/teams/{tid}/channels/{cid}/messages?$top={limit}").get("value", [])
        else:
            items = self._req("GET", f"/chats/{self._find_chat(to)}/messages?$top={limit}").get("value", [])
        return [{"from": ((m.get("from") or {}).get("user") or {}).get("displayName", "?"),
                 "date": m.get("createdDateTime", ""), "text": _strip_html((m.get("body") or {}).get("content", ""))}
                for m in items if m.get("messageType") == "message"]

    # --------------------------------------------------------- calendar
    def calendar_read(self, start: dt.datetime, end: dt.datetime) -> list[dict[str, str]]:
        q = f"/me/calendarView?startDateTime={start.isoformat()}&endDateTime={end.isoformat()}&$top=50&$orderby=start/dateTime"
        out = []
        for e in self._req("GET", q, headers={"Prefer": 'outlook.timezone="UTC"'}).get("value", []):
            out.append({"subject": e.get("subject", ""), "start": e["start"]["dateTime"][:16],
                        "end": e["end"]["dateTime"][:16], "location": (e.get("location") or {}).get("displayName", ""),
                        "attendees": ", ".join(a["emailAddress"]["address"] for a in e.get("attendees", []))})
        return out

    def calendar_create(self, subject: str, start: dt.datetime, end: dt.datetime, attendees: list[str],
                        location: str = "", body: str = "", online: bool = False) -> str:
        tz = time.strftime("%Z") or "UTC"
        ev = {"subject": subject, "body": {"contentType": "text", "content": body},
              "start": {"dateTime": start.isoformat(), "timeZone": tz},
              "end": {"dateTime": end.isoformat(), "timeZone": tz},
              "location": {"displayName": location},
              "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees]}
        if online:
            ev.update(isOnlineMeeting=True, onlineMeetingProvider="teamsForBusiness")
        self._req("POST", "/me/events", json=ev)
        return f"Created '{subject}' {start:%Y-%m-%d %H:%M}" + (f" and invited {', '.join(attendees)}" if attendees else "")

    # ------------------------------------------------------------ files
    def files_search(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        items = self._req("GET", f"/me/drive/root/search(q='{query.replace(chr(39), '')}')?$top={limit}").get("value", [])
        try:   # SharePoint / Teams files the user can see
            hits = self._req("POST", "/search/query", json={"requests": [{
                "entityTypes": ["driveItem"], "query": {"queryString": query}, "size": limit}]})
            for c in hits.get("value", [{}])[0].get("hitsContainers", []):
                for h in c.get("hits", []):
                    items.append(h.get("resource", {}))
        except GraphError:
            pass
        seen, out = set(), []
        for it in items:
            if not it.get("id") or it["id"] in seen:
                continue
            seen.add(it["id"])
            out.append({"id": it["id"], "name": it.get("name", ""), "url": it.get("webUrl", ""),
                        "drive": (it.get("parentReference") or {}).get("driveId", ""),
                        "modified": it.get("lastModifiedDateTime", "")[:16]})
        return out[:limit]

    def files_download(self, item: dict[str, str], folder: Path) -> Path:
        dst = folder / item["name"]
        if dst.exists():
            raise GraphError(f"'{dst}' already exists — nothing was overwritten")
        path = f"/drives/{item['drive']}/items/{item['id']}/content" if item.get("drive") else \
            f"/me/drive/items/{item['id']}/content"
        dst.write_bytes(self._req("GET", path, follow_redirects=True))
        return dst

    def files_upload(self, src: Path, folder: str = "") -> str:
        if src.stat().st_size > 4 * 1024 * 1024:
            raise GraphError("uploads over 4 MB are not supported here — use the OneDrive folder on this PC")
        target = "/".join(p for p in (folder.strip("/"), src.name) if p)
        res = self._req("PUT", f"/me/drive/root:/{target}:/content?@microsoft.graph.conflictBehavior=fail",
                        content=src.read_bytes())
        return f"Uploaded {src.name} to OneDrive/{target} ({res.get('webUrl', '') if isinstance(res, dict) else ''})"


def _strip_html(text: str) -> str:
    import re
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())[:400]
