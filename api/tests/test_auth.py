from lp_api import auth as A


def test_password_hash_roundtrip() -> None:
    h = A.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert A.verify_password("correct horse battery", h)
    assert not A.verify_password("correct horse batterY", h)
    assert not A.verify_password("x", "garbage")
    assert A.hash_password("same password") != A.hash_password("same password")   # salted


def test_pkce_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert A.pkce_ok(verifier, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    assert not A.pkce_ok(verifier[:-1] + "l", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    assert not A.pkce_ok("short", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


def test_redirect_uris() -> None:
    assert A.redirect_uri_ok("https://claude.ai/api/mcp/auth_callback")
    assert A.redirect_uri_ok("http://localhost:33418/callback")
    assert A.redirect_uri_ok("http://127.0.0.1:5000/cb")
    assert not A.redirect_uri_ok("http://evil.example/cb")
    assert not A.redirect_uri_ok("https://claude.ai/cb#frag")
    assert not A.redirect_uri_ok("javascript:alert(1)")
    assert not A.redirect_uri_ok("https://" + "a" * 600)


def test_email_and_password_rules() -> None:
    assert A.normalize_email("  Me@Example.COM ") == "me@example.com"
    assert A.normalize_email("nope") is None
    assert A.normalize_email("a@b") is None
    assert A.password_problem("short") and A.password_problem("x" * 201)
    assert A.password_problem("long enough pw") is None


def test_with_query_and_secrets() -> None:
    assert A.with_query("https://a.b/cb", {"code": "x y"}) == "https://a.b/cb?code=x+y"
    assert A.with_query("https://a.b/cb?k=1", {"code": "c"}) == "https://a.b/cb?k=1&code=c"
    assert A.new_secret() != A.new_secret() and len(A.digest("x")) == 64
