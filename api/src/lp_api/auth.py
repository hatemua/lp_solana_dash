"""Accounts (email + password, session cookie) and an OAuth 2.1 authorization server for the MCP.

OAuth: authorization code + PKCE (S256 only), public clients, dynamic client registration (RFC 7591), metadata
(RFC 8414, RFC 9728), refresh tokens rotated on use. One scope: mcp:read. Secrets are stored as SHA-256 hashes only.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Settings

log = logging.getLogger("lp_api.auth")

SCOPE = "mcp:read"
SESSION_COOKIE = "lp_session"
SESSION_DAYS = 30
CODE_TTL_S = 300
ACCESS_TTL_S = 3600
REFRESH_TTL_DAYS = 30
MIN_PASSWORD = 10

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id bigserial PRIMARY KEY, email text NOT NULL UNIQUE, password_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), last_login_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS user_sessions (
        token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id text PRIMARY KEY, client_name text, redirect_uris jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash text PRIMARY KEY, client_id text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
        user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE, redirect_uri text NOT NULL,
        code_challenge text NOT NULL, scope text NOT NULL, expires_at timestamptz NOT NULL,
        used boolean NOT NULL DEFAULT false)""",
    """CREATE TABLE IF NOT EXISTS oauth_tokens (
        token_hash text PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
        client_id text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
        user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE, scope text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
        revoked boolean NOT NULL DEFAULT false)""",
    "CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens (user_id, client_id)",
]


# ---------------------------------------------------------------------------------------------- pure helpers
def hash_password(password: str, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


# verified against when the email is unknown, so a login takes the same time either way
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier or ""):
        return False
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(calc, challenge)


def redirect_uri_ok(uri: str) -> bool:
    """https anywhere, or http only to a loopback address (native apps, e.g. Claude Code); never a fragment."""
    if not isinstance(uri, str) or len(uri) > 500:
        return False
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(email: str) -> str | None:
    e = (email or "").strip().lower()
    return e if len(e) <= 254 and EMAIL_RE.match(e) else None


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if len(password) > 200:
        return "password is too long"
    return None


def csrf_for(session_hash: str) -> str:
    return digest("csrf:" + session_hash)[:32]


def with_query(uri: str, params: dict[str, str]) -> str:
    return uri + ("&" if "?" in uri else "?") + urlencode(params)


# ---------------------------------------------------------------------------------------------- routes
class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


CONSENT_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorize: LP Dash</title>
<style>
:root{{--bg:#0b0e14;--card:#121722;--line:#232b3a;--fg:#e2e8f0;--mut:#8a94a8;--acc:#7aa2ff}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;display:grid;
place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;max-width:420px;
width:100%}}
h1{{font-size:18px;margin:0 0 12px}} .mut{{color:var(--mut);font-size:13px}} ul{{padding-left:18px}}
.row{{display:flex;gap:8px;margin-top:20px}} button{{flex:1;padding:10px;border-radius:8px;border:1px solid
var(--line);background:transparent;color:var(--fg);font:inherit;cursor:pointer}}
button.ok{{background:var(--acc);border-color:var(--acc);color:#0b0e14;font-weight:600}}
</style></head><body><form class="card" method="post" action="/oauth/authorize">
<h1>Allow <b>{client}</b> to use LP Dash?</h1>
<p class="mut">Signed in as {email}. Redirects to {host}.</p>
<ul><li>Read pool data, charts, bins and LP signals</li>
<li>Read-only: it cannot trade, sign or move funds</li></ul>
{hidden}
<div class="row"><button name="decision" value="deny">Deny</button>
<button class="ok" name="decision" value="allow">Allow</button></div></form></body></html>"""

ERROR_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Authorization error</title></head>
<body style="font:15px system-ui;padding:24px"><h1 style="font-size:18px">Authorization error</h1>
<p>{msg}</p></body></html>"""


def router(cfg: Settings, state: dict[str, Any]) -> APIRouter:
    r = APIRouter()
    issuer = cfg.public_api_url.rstrip("/")
    resource = issuer + "/mcp"

    async def q(sql: str, **params: Any) -> list[dict[str, Any]]:
        engine: AsyncEngine = state["engine"]
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(x._mapping) for x in res] if res.returns_rows else []

    def set_session(resp: Response, token: str) -> None:
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=cfg.cookie_secure, samesite="lax", path="/")

    async def start_session(resp: Response, user_id: int) -> None:
        token = new_secret()
        await q("INSERT INTO user_sessions (token_hash, user_id, expires_at) "
                "VALUES (:h, :u, now() + make_interval(days => :d))", h=digest(token), u=user_id, d=SESSION_DAYS)
        set_session(resp, token)

    async def session_user(request: Request) -> tuple[dict[str, Any], str] | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        h = digest(token)
        found = await q("SELECT u.id, u.email, u.created_at FROM user_sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = :h AND s.expires_at > now()", h=h)
        return (found[0], h) if found else None

    async def bearer_user(request: Request) -> dict[str, Any] | None:
        m = re.match(r"^Bearer\s+(\S+)$", request.headers.get("authorization", ""), re.I)
        if not m:
            return None
        found = await q("SELECT u.id, u.email, u.created_at, t.scope, t.client_id, t.expires_at "
                        "FROM oauth_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = :h "
                        "AND t.kind = 'access' AND NOT t.revoked AND t.expires_at > now()", h=digest(m.group(1)))
        return found[0] if found else None

    def public_user(u: dict[str, Any]) -> dict[str, Any]:
        return {"id": u["id"], "email": u["email"], "created_at": u["created_at"].isoformat()}

    # ------------------------------------------------------------------ accounts
    @r.post("/v1/auth/signup")
    async def signup(body: Credentials) -> Response:
        if not cfg.signup_open:
            raise HTTPException(403, "sign-up is closed")
        email = normalize_email(body.email)
        if not email:
            raise HTTPException(400, "enter a valid email address")
        if problem := password_problem(body.password):
            raise HTTPException(400, problem)
        made = await q("INSERT INTO users (email, password_hash, last_login_at) VALUES (:e, :p, now()) "
                       "ON CONFLICT (email) DO NOTHING RETURNING id, email, created_at",
                       e=email, p=hash_password(body.password))
        if not made:
            raise HTTPException(409, "an account with this email already exists; log in instead")
        resp = JSONResponse({"user": public_user(made[0])}, status_code=201)
        await start_session(resp, made[0]["id"])
        return resp

    @r.post("/v1/auth/login")
    async def login(body: Credentials) -> Response:
        email = normalize_email(body.email) or ""
        found = await q("SELECT id, email, created_at, password_hash FROM users WHERE email = :e", e=email)
        ok = verify_password(body.password, found[0]["password_hash"] if found else _DUMMY_HASH)
        if not (found and ok):
            raise HTTPException(401, "wrong email or password")
        await q("UPDATE users SET last_login_at = now() WHERE id = :u", u=found[0]["id"])
        resp = JSONResponse({"user": public_user(found[0])})
        await start_session(resp, found[0]["id"])
        return resp

    @r.post("/v1/auth/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await q("DELETE FROM user_sessions WHERE token_hash = :h", h=digest(token))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/", secure=cfg.cookie_secure, httponly=True, samesite="lax")
        return resp

    @r.get("/v1/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        """The signed-in user (session cookie) or the owner of an OAuth access token (used by the MCP server)."""
        if tok := await bearer_user(request):
            return {"user": public_user(tok), "via": "oauth", "scope": tok["scope"], "client_id": tok["client_id"],
                    "expires_at": tok["expires_at"].isoformat()}
        if s := await session_user(request):
            return {"user": public_user(s[0]), "via": "session"}
        raise HTTPException(401, "not signed in")

    @r.get("/v1/auth/connections")
    async def connections(request: Request) -> dict[str, Any]:
        """Apps (MCP clients) the user has authorized, with an active refresh token."""
        s = await session_user(request)
        if not s:
            raise HTTPException(401, "not signed in")
        data = await q("SELECT c.client_id, c.client_name, min(t.created_at) AS since, max(t.created_at) AS last_used "
                       "FROM oauth_tokens t JOIN oauth_clients c ON c.client_id = t.client_id WHERE t.user_id = :u "
                       "AND NOT t.revoked AND t.expires_at > now() GROUP BY c.client_id, c.client_name "
                       "ORDER BY last_used DESC", u=s[0]["id"])
        return {"connections": [{**d, "since": d["since"].isoformat(), "last_used": d["last_used"].isoformat()}
                                for d in data]}

    @r.post("/v1/auth/connections/{client_id}/revoke")
    async def revoke_connection(client_id: str, request: Request) -> dict[str, Any]:
        s = await session_user(request)
        if not s:
            raise HTTPException(401, "not signed in")
        await q("UPDATE oauth_tokens SET revoked = true WHERE user_id = :u AND client_id = :c",
                u=s[0]["id"], c=client_id[:80])
        return {"ok": True}

    # ------------------------------------------------------------------ OAuth metadata
    @r.get("/.well-known/oauth-authorization-server")
    async def as_metadata() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }

    @r.get("/.well-known/oauth-protected-resource")
    @r.get("/.well-known/oauth-protected-resource/mcp")
    async def pr_metadata() -> dict[str, Any]:
        return {"resource": resource, "authorization_servers": [issuer], "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"], "resource_name": "LP Solana Dash MCP (read-only)"}

    # ------------------------------------------------------------------ dynamic client registration
    @r.post("/oauth/register")
    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_client_metadata", "error_description": "JSON body expected"}, 400)
        uris = body.get("redirect_uris")
        if not (isinstance(uris, list) and 0 < len(uris) <= 5 and all(redirect_uri_ok(u) for u in uris)):
            return JSONResponse({"error": "invalid_redirect_uri",
                                 "error_description": "1-5 redirect URIs, https or http://localhost"}, 400)
        if body.get("token_endpoint_auth_method", "none") != "none":
            return JSONResponse({"error": "invalid_client_metadata",
                                 "error_description": "only public clients (token_endpoint_auth_method=none)"}, 400)
        name = str(body.get("client_name") or "MCP client")[:80]
        client_id = "mcp_" + secrets.token_urlsafe(18)
        await q("INSERT INTO oauth_clients (client_id, client_name, redirect_uris) VALUES (:c, :n, CAST(:u AS jsonb))",
                c=client_id, n=name, u=json.dumps(uris))
        return JSONResponse({"client_id": client_id, "client_id_issued_at": int(time.time()),
                             "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": "none",
                             "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                             "scope": SCOPE}, status_code=201)

    # ------------------------------------------------------------------ authorize
    AUTHZ_KEYS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
                  "scope", "resource")

    async def check_authz(p: dict[str, str]) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """-> (client, error_for_page, error_for_redirect). Page errors: the redirect URI is not trusted yet."""
        client = None
        if p.get("client_id"):
            found = await q("SELECT client_id, client_name, redirect_uris FROM oauth_clients WHERE client_id = :c",
                            c=p["client_id"][:80])
            client = found[0] if found else None
        if not client:
            return None, "Unknown client. Remove and re-add the connector in your MCP client.", None
        if p.get("redirect_uri") not in client["redirect_uris"]:
            return None, "This redirect URI is not registered for the client.", None
        if p.get("response_type") != "code":
            return client, None, "unsupported_response_type"
        if p.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_\-]{43}",
                                                                          p.get("code_challenge", "")):
            return client, None, "invalid_request"
        if p.get("scope") and set(p["scope"].split()) - {SCOPE}:
            return client, None, "invalid_scope"
        if p.get("resource") and p["resource"].rstrip("/") not in (resource, issuer):
            return client, None, "invalid_target"
        return client, None, None

    def error_redirect(p: dict[str, str], error: str) -> RedirectResponse:
        params = {"error": error, "iss": issuer}
        if p.get("state"):
            params["state"] = p["state"]
        return RedirectResponse(with_query(p["redirect_uri"], params), status_code=302)

    def page(content: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(content, status_code=status, headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: "
                                       "http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
            "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @r.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        p = {k: v[:600] for k, v in request.query_params.items() if k in AUTHZ_KEYS}
        client, page_err, redir_err = await check_authz(p)
        if page_err:
            return page(ERROR_HTML.format(msg=html.escape(page_err)), 400)
        if redir_err:
            return error_redirect(p, redir_err)
        s = await session_user(request)
        if not s:
            here = f"{issuer}/oauth/authorize?{urlencode(p)}"
            return RedirectResponse(f"{cfg.web_url.rstrip('/')}/login?next={quote(here, safe='')}", status_code=302)
        user, sh = s
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in p.items())
        hidden += f'<input type="hidden" name="csrf" value="{csrf_for(sh)}">'
        assert client is not None
        return page(CONSENT_HTML.format(client=html.escape(client["client_name"] or client["client_id"]),
                                        email=html.escape(user["email"]),
                                        host=html.escape(urlsplit(p["redirect_uri"]).hostname or ""),
                                        hidden=hidden))

    @r.post("/oauth/authorize")
    async def authorize_decision(request: Request) -> Response:
        form = dict(parse_qsl((await request.body()).decode(errors="replace")[:8000]))
        p = {k: v[:600] for k, v in form.items() if k in AUTHZ_KEYS}
        client, page_err, redir_err = await check_authz(p)
        if page_err:
            return page(ERROR_HTML.format(msg=html.escape(page_err)), 400)
        if redir_err:
            return error_redirect(p, redir_err)
        s = await session_user(request)
        if not s or not hmac.compare_digest(form.get("csrf", ""), csrf_for(s[1])):
            return page(ERROR_HTML.format(msg="Your session expired. Start the connection again."), 400)
        if form.get("decision") != "allow":
            return error_redirect(p, "access_denied")
        code = new_secret()
        await q("INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, scope, "
                "expires_at) VALUES (:h, :c, :u, :r, :cc, :s, now() + make_interval(secs => :t))",
                h=digest(code), c=p["client_id"], u=s[0]["id"], r=p["redirect_uri"], cc=p["code_challenge"],
                s=SCOPE, t=CODE_TTL_S)
        log.info("oauth grant user=%s client=%s", s[0]["id"], p["client_id"])
        params = {"code": code, "iss": issuer}
        if p.get("state"):
            params["state"] = p["state"]
        return RedirectResponse(with_query(p["redirect_uri"], params), status_code=302)

    # ------------------------------------------------------------------ token
    def token_error(error: str, desc: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                            headers={"Cache-Control": "no-store"})

    async def issue(client_id: str, user_id: int) -> JSONResponse:
        access, refresh = new_secret(), new_secret()
        await q("INSERT INTO oauth_tokens (token_hash, kind, client_id, user_id, scope, expires_at) VALUES "
                "(:a, 'access', :c, :u, :s, now() + make_interval(secs => :ta)), "
                "(:r, 'refresh', :c, :u, :s, now() + make_interval(days => :tr))",
                a=digest(access), r=digest(refresh), c=client_id, u=user_id, s=SCOPE, ta=ACCESS_TTL_S,
                tr=REFRESH_TTL_DAYS)
        return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                             "refresh_token": refresh, "scope": SCOPE},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @r.post("/oauth/token")
    async def token(request: Request) -> Response:
        f = dict(parse_qsl((await request.body()).decode(errors="replace")[:8000]))
        client_id = f.get("client_id", "")[:80]
        if f.get("grant_type") == "authorization_code":
            got = await q("UPDATE oauth_codes SET used = true WHERE code_hash = :h AND NOT used AND expires_at > now() "
                          "RETURNING client_id, user_id, redirect_uri, code_challenge", h=digest(f.get("code", "")))
            if not got:
                return token_error("invalid_grant", "code is invalid, expired or already used")
            c = got[0]
            if c["client_id"] != client_id or c["redirect_uri"] != f.get("redirect_uri"):
                return token_error("invalid_grant", "client_id or redirect_uri does not match the code")
            if not pkce_ok(f.get("code_verifier", ""), c["code_challenge"]):
                return token_error("invalid_grant", "PKCE verification failed")
            return await issue(c["client_id"], c["user_id"])
        if f.get("grant_type") == "refresh_token":
            got = await q("UPDATE oauth_tokens SET revoked = true WHERE token_hash = :h AND kind = 'refresh' "
                          "AND NOT revoked AND expires_at > now() RETURNING client_id, user_id",
                          h=digest(f.get("refresh_token", "")))
            if not got or got[0]["client_id"] != client_id:
                return token_error("invalid_grant", "refresh token is invalid, expired or revoked")
            return await issue(got[0]["client_id"], got[0]["user_id"])
        return token_error("unsupported_grant_type", "use authorization_code or refresh_token")

    @r.post("/oauth/revoke")
    async def revoke(request: Request) -> Response:
        f = dict(parse_qsl((await request.body()).decode(errors="replace")[:8000]))
        if f.get("token"):
            await q("UPDATE oauth_tokens SET revoked = true WHERE token_hash = :h", h=digest(f["token"]))
        return Response(status_code=200)

    return r


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for stmt in SCHEMA:
            await conn.execute(text(stmt))
