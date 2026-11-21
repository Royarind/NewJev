"""One-click connections: PKCE start, code exchange, token refresh, status."""

import json
import time
import urllib.parse

import httpx

from app import connections as C


def _cfg(tmp_path, **ids):
    f = tmp_path / "connectors.json"
    f.write_text(json.dumps({"slack_client_id": "S123", "microsoft_client_id": "M456", "microsoft_tenant": "organizations",
                             "google_client_id": "G789", "google_client_secret": "gsecret", **ids}))
    C.CONNECTORS_FILE = f


def _q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_slack_connect_flow_with_pkce(tmp_path):
    _cfg(tmp_path)
    url = C.start("slack", 8766)
    q = _q(url)
    assert url.startswith("https://slack.com/oauth/v2/authorize") and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://localhost:8766/oauth/slack/callback" and "chat:write" in q["user_scope"]
    sent = {}

    def handler(req):
        sent.update(dict(urllib.parse.parse_qsl(req.content.decode())))
        return httpx.Response(200, json={"ok": True, "team": {"id": "T1", "name": "Rezolve"},
                                         "authed_user": {"id": "U1", "access_token": "xoxe.xoxp-1",
                                                         "refresh_token": "xoxe-1-r", "expires_in": 43200}})

    what = C.finish("slack", "CODE", q["state"], http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "Rezolve" in what and sent["code_verifier"] and "client_secret" not in sent
    assert C.load_token("slack")["access_token"] == "xoxe.xoxp-1"
    # the same state cannot be used twice
    try:
        C.finish("slack", "CODE", q["state"])
        raise AssertionError
    except C.ConnectError:
        pass
    # expired access token is refreshed (token rotation)
    tok = C.load_token("slack")
    tok["expires_at"] = time.time() - 1
    C.save_token("slack", tok)

    def refresh(req):
        assert "grant_type=refresh_token" in req.content.decode()
        return httpx.Response(200, json={"ok": True, "access_token": "xoxe.xoxp-2", "refresh_token": "xoxe-1-r2",
                                         "expires_in": 43200})

    assert C.access_token("slack", http=httpx.Client(transport=httpx.MockTransport(refresh))) == "xoxe.xoxp-2"
    st = {c["id"]: c for c in C.status()}
    assert st["slack"]["connected"] and st["slack"]["account"] == "Rezolve"
    assert C.disconnect("slack") and not C.load_token("slack")


def test_microsoft_and_google_flows(tmp_path):
    _cfg(tmp_path)
    q = _q(C.start("microsoft", 8766))
    assert "Calendars.ReadWrite" in q["scope"] and q["prompt"] == "select_account"

    def ms(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "ms-a", "refresh_token": "ms-r", "expires_in": 3600})
        return httpx.Response(200, json={"mail": "arindam@rezolve.com"})

    assert "arindam@rezolve.com" in C.finish("microsoft", "c", q["state"], http=httpx.Client(transport=httpx.MockTransport(ms)))
    from app.shortcuts.msgraph import Graph
    from app.config import get_settings
    g = Graph("M456", "organizations", get_settings().graph_token_path)
    assert g.connected() and g._token() == "ms-a"

    url = C.start("google", 8766)
    q = _q(url)
    assert q["redirect_uri"].startswith("http://127.0.0.1:8766/") and q["access_type"] == "offline"

    def gg(req):
        if "oauth2.googleapis.com" in str(req.url):
            body = dict(urllib.parse.parse_qsl(req.content.decode()))
            assert body["client_secret"] == "gsecret" and body["code_verifier"]
            return httpx.Response(200, json={"access_token": "g-a", "refresh_token": "g-r", "expires_in": 3600})
        return httpx.Response(200, json={"email": "me@gmail.com"})

    assert "me@gmail.com" in C.finish("google", "c", q["state"], http=httpx.Client(transport=httpx.MockTransport(gg)))
    from app.shortcuts.mail import gmail_available
    assert gmail_available("", "")


def test_not_set_up_builds_explain_themselves(tmp_path):
    _cfg(tmp_path, slack_client_id="")
    try:
        C.start("slack", 8766)
        raise AssertionError
    except C.ConnectError as exc:
        assert "not set up" in str(exc)
    st = {c["id"]: c for c in C.status()}
    assert st["slack"]["available"] is False and "publisher" in st["slack"]["note"]
