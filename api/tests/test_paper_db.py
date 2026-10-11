"""Demo accounts and bots through the API on a real DB: limits, money accounting, ownership, cross-site refusal."""

import os
import secrets

import pytest

pytestmark = [pytest.mark.db, pytest.mark.skipif(not (os.environ.get("DATABASE_URL") and os.environ.get("REDIS_URL")),
                                                 reason="needs DATABASE_URL and REDIS_URL")]


def make_client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from lp_api.config import Settings
    from lp_api.main import create_app

    cfg = Settings(database_url=os.environ["DATABASE_URL"], redis_url=os.environ["REDIS_URL"],
                   indexer_health_url="http://127.0.0.1:9/none", rate_limit_per_min=1000,
                   auth_rate_limit_per_min=1000, cookie_secure=False, public_api_url="http://testserver",
                   web_url="https://web.test")
    return TestClient(create_app(cfg), base_url="http://testserver")


def signup(c) -> None:  # type: ignore[no-untyped-def]
    r = c.post("/v1/auth/signup", json={"email": f"p{secrets.token_hex(4)}@example.com", "password": "a long password"})
    assert r.status_code == 201


def test_account_bots_and_limits() -> None:
    with make_client() as c, make_client() as other:
        assert c.get("/v1/paper/account").status_code == 401
        signup(c)
        assert c.get("/v1/paper/account").json()["account"] is None
        assert c.post("/v1/paper/account", json={"start_usd": 50_000}).status_code == 422      # max $10k
        a = c.post("/v1/paper/account", json={"start_usd": 5_000}).json()["account"]
        assert a["cash_usd"] == 5_000 and a["equity_usd"] == 5_000
        assert c.post("/v1/paper/account", json={"start_usd": 5_000}).status_code == 409

        bot = {"name": "dips", "strategy": "tpl:dip_reversal", "budget_usd": 1_000, "position_usd": 100}
        assert c.post("/v1/paper/bots", json=bot, headers={"origin": "https://evil.example"}).status_code == 403
        b = c.post("/v1/paper/bots", json=bot).json()
        assert b["status"] == "running" and b["cash_usd"] == 1_000 and b["strategy_name"] == "Dip reversal"
        acc = c.get("/v1/paper/account").json()
        assert acc["account"]["cash_usd"] == 4_000 and acc["account"]["equity_usd"] == 5_000
        assert c.post("/v1/paper/bots", json={**bot, "budget_usd": 4_500}).status_code == 400      # not enough cash
        assert c.post("/v1/paper/bots", json={**bot, "position_usd": 2_000}).status_code == 400    # > budget
        assert c.post("/v1/paper/bots", json={**bot, "strategy": "tpl:nope"}).status_code == 400

        bad = {"name": "x", "params": {"exit": {"trail_trigger_pct": 5}}}
        assert c.post("/v1/paper/strategies", json=bad).status_code == 422
        mine = {"name": "cats only", "params": {"entry": {"narratives": ["cat"], "liquidity_min": 80_000},
                                                "exit": {"stop_loss_pct": 7, "max_hours": 4}}}
        sid = c.post("/v1/paper/strategies", json=mine).json()["id"]
        b2 = c.post("/v1/paper/bots", json={**bot, "name": "cats", "strategy": f"my:{sid}"}).json()
        assert b2["params"]["entry"]["narratives"] == ["cat"] and b2["params"]["exit"]["stop_loss_pct"] == 7

        # another user sees none of it
        signup(other)
        assert other.get(f"/v1/paper/bots/{b['id']}/positions").status_code == 404
        assert other.post(f"/v1/paper/bots/{b['id']}/action", json={"action": "stop"}).status_code == 404
        assert other.post("/v1/paper/bots", json={**bot, "strategy": f"my:{sid}"}).status_code == 400

        assert c.post(f"/v1/paper/bots/{b['id']}/delete").status_code == 400                      # stop first
        assert c.post(f"/v1/paper/bots/{b['id']}/action", json={"action": "pause"}).json()["status"] == "paused"
        assert c.post(f"/v1/paper/bots/{b['id']}/action", json={"action": "stop"}).json()["status"] == "stopping"
        assert c.get(f"/v1/paper/bots/{b['id']}/positions").json()["positions"] == []

        r = c.post("/v1/paper/account", json={"start_usd": 10_000, "reset": True}).json()
        assert r["account"]["cash_usd"] == 10_000 and r["bots"] == []
        assert c.get("/v1/paper/templates").json()["templates"][0]["params"]["exit"]["stop_loss_pct"] > 0
