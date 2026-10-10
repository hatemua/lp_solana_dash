"""Accounts and the full OAuth flow (register -> login -> consent -> code -> token -> /me -> refresh) on a real DB."""

import base64
import hashlib
import os
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = [pytest.mark.db, pytest.mark.skipif(not (os.environ.get("DATABASE_URL") and os.environ.get("REDIS_URL")),
                                                 reason="needs DATABASE_URL and REDIS_URL")]

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(scope="module")
def client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from lp_api.config import Settings
    from lp_api.main import create_app

    cfg = Settings(database_url=os.environ["DATABASE_URL"], redis_url=os.environ["REDIS_URL"],
                   indexer_health_url="http://127.0.0.1:9/none", rate_limit_per_min=1000,
                   auth_rate_limit_per_min=1000, cookie_secure=False, public_api_url="http://testserver",
                   web_url="https://web.test")
    with TestClient(create_app(cfg), base_url="http://testserver") as c:
        yield c


def test_signup_login_logout(client) -> None:  # type: ignore[no-untyped-def]
    email = f"u{secrets.token_hex(4)}@example.com"
    assert client.post("/v1/auth/signup", json={"email": email, "password": "short"}).status_code == 400
    r = client.post("/v1/auth/signup", json={"email": email.upper(), "password": "a long password"})
    assert r.status_code == 201 and r.json()["user"]["email"] == email
    assert client.get("/v1/auth/me").json()["via"] == "session"
    assert client.post("/v1/auth/signup", json={"email": email, "password": "a long password"}).status_code == 409
    client.post("/v1/auth/logout")
    assert client.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/login", json={"email": email, "password": "wrong password!"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": email, "password": "a long password"}).status_code == 200
    assert client.get("/v1/auth/me").json()["user"]["email"] == email


def test_oauth_flow(client) -> None:  # type: ignore[no-untyped-def]
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == "http://testserver/mcp"

    assert client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    reg = client.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"})
    assert reg.status_code == 201
    cid = reg.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": cid, "redirect_uri": CALLBACK, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "st8", "scope": "mcp:read"}

    # unregistered redirect: error page, never a redirect
    bad = client.get("/oauth/authorize", params={**params, "redirect_uri": "https://evil.example/cb"},
                     follow_redirects=False)
    assert bad.status_code == 400

    # logged out: sent to the web login with next=
    client.post("/v1/auth/logout")
    r = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://web.test/login?next=")

    email = f"o{secrets.token_hex(4)}@example.com"
    client.post("/v1/auth/signup", json={"email": email, "password": "a long password"})
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Claude" in page.text and "cannot trade" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text)
    assert csrf

    form = {**params, "csrf": csrf.group(1), "decision": "allow"}
    assert client.post("/oauth/authorize", data={**form, "csrf": "0" * 32}).status_code == 400
    r = client.post("/oauth/authorize", data=form, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(CALLBACK)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["st8"] and q["iss"] == ["http://testserver"]
    code = q["code"][0]

    tok = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": cid}
    assert client.post("/oauth/token", data={**tok, "code_verifier": "x" * 43}).status_code == 400
    # the failed attempt burned the code (single use), so run a fresh authorization
    r = client.post("/oauth/authorize", data=form, follow_redirects=False)
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    t = client.post("/oauth/token", data={**tok, "code": code, "code_verifier": verifier})
    assert t.status_code == 200, t.text
    access, refresh = t.json()["access_token"], t.json()["refresh_token"]
    assert client.post("/oauth/token", data={**tok, "code": code, "code_verifier": verifier}).status_code == 400

    client.cookies.clear()
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).json()
    assert me["via"] == "oauth" and me["user"]["email"] == email and me["scope"] == "mcp:read"

    rt = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid})
    assert rt.status_code == 200 and rt.json()["access_token"] != access
    again = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                              "client_id": cid})
    assert again.status_code == 400                                       # rotated: the old refresh token is dead

    client.post("/oauth/revoke", data={"token": rt.json()["access_token"]})
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {rt.json()['access_token']}"}).status_code \
        == 401
