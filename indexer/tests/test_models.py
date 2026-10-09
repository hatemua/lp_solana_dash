from datetime import UTC, datetime
from typing import Any

from indexer import models as M
from indexer.config import SOL_MINT

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_pool_row_from_real_api(pools_page: dict[str, Any]) -> None:
    p = pools_page["data"][0]
    row = M.pool_row(p, NOW)
    assert row["address"] == p["address"]
    assert row["bin_step"] == p["pool_config"]["bin_step"]
    assert row["base_fee_pct"] == p["pool_config"]["base_fee_pct"]
    assert row["created_at"].tzinfo is not None
    assert isinstance(row["tags"], list)
    assert row["tvl"] == p["tvl"]


def test_token_rows_both_sides(pools_page: dict[str, Any]) -> None:
    p = pools_page["data"][1]
    rows = M.token_rows(p, NOW)
    assert {r["mint"] for r in rows} == {p["token_x"]["address"], p["token_y"]["address"]}
    assert all(r["symbol"] for r in rows)


def test_hot_rule(pools_page: dict[str, Any]) -> None:
    data = pools_page["data"]
    memecoin = data[1]                       # SOL pair, TVL > $10k
    small = data[-1]                         # TVL < $10k
    assert M.is_sol_pair(memecoin) and M.is_hot(memecoin, 10_000, 50_000)
    assert not M.is_hot(small, 10_000, 50_000) or (small["volume"]["1h"] or 0) >= 50_000
    stable = dict(data[1], token_x={"address": "USDC"}, token_y={"address": "USDT"})
    assert not M.is_hot(stable, 10_000, 50_000)
    banned = dict(memecoin, is_blacklisted=True)
    assert not M.is_hot(banned, 10_000, 50_000)


def test_other_mint(pools_page: dict[str, Any]) -> None:
    p = pools_page["data"][1]
    m = M.other_mint(p)
    assert m and m != SOL_MINT and m in (p["token_x"]["address"], p["token_y"]["address"])


def test_stat_row_windows(pools_page: dict[str, Any]) -> None:
    p = pools_page["data"][1]
    s = M.stat_row(p, NOW, volume_5m=123.0, fees_5m=1.5)
    assert s["volume_1h"] == p["volume"]["1h"] and s["fees_24h"] == p["fees"]["24h"]
    assert s["fee_tvl_1h"] == p["fee_tvl_ratio"]["1h"]
    assert s["volume_5m"] == 123.0 and s["fees_5m"] == 1.5
    assert s["cum_fees"] == p["cumulative_metrics"]["fees"]


def test_ohlcv_join_by_timestamp(ohlcv: dict[str, Any], volume: dict[str, Any]) -> None:
    rows = M.ohlcv_rows("POOL", ohlcv, volume)
    assert len(rows) == len(ohlcv["data"]) > 0
    by_ts = {b["timestamp"]: b for b in volume["data"]}
    for r, b in zip(rows, ohlcv["data"], strict=True):
        assert r["close"] == b["close"] and r["volume"] == b["volume"]
        if b["timestamp"] in by_ts:
            assert r["fees"] == by_ts[b["timestamp"]]["fees"]


def test_jupiter_token_row(jup_assets: list[dict[str, Any]]) -> None:
    a = jup_assets[0]
    row = M.jupiter_token_row(a, NOW)
    assert row["mint"] == a["id"]
    assert row["holders"] == a.get("holderCount")
    assert row["audit"] == (a.get("audit") or None)
    assert row["created_at"] is None or row["created_at"].tzinfo is not None


def test_candle_rows(jup_chart: dict[str, Any]) -> None:
    rows = M.candle_rows("MINT", jup_chart)
    assert len(rows) == len(jup_chart["candles"]) > 0
    assert rows[0]["ts"].tzinfo is not None


def test_tvl_flow_swap_is_not_flow() -> None:
    # a swap: 10 X out, 1000 Y in at price 100 -> no liquidity flow
    prev = {"token_x_amount": 100.0, "token_y_amount": 10_000.0, "price_x_usd": 100.0, "price_y_usd": 1.0}
    cur = {"token_x_amount": 90.0, "token_y_amount": 11_000.0, "price_x_usd": 100.0, "price_y_usd": 1.0}
    assert abs(M.tvl_flow(prev, cur)["net_flow_usd"] or 0) < 1e-6


def test_tvl_flow_add_and_price_effect() -> None:
    prev = {"token_x_amount": 100.0, "token_y_amount": 10_000.0, "price_x_usd": 100.0, "price_y_usd": 1.0}
    cur = {"token_x_amount": 150.0, "token_y_amount": 15_000.0, "price_x_usd": 110.0, "price_y_usd": 1.0}
    f = M.tvl_flow(prev, cur)
    assert f["net_flow_usd"] == 50 * 110 + 5_000           # LP added 50 X + 5000 Y
    assert f["price_effect_usd"] == 100 * 10               # old 100 X gained $10 each


def test_tvl_flow_missing_data() -> None:
    assert M.tvl_flow({}, {"token_x_amount": 1})["net_flow_usd"] is None
