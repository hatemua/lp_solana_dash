import math

from lp_api import signals as S


def candles(closes: list[float], fees: list[float] | None = None) -> list[S.Candle]:
    fees = fees or [1.0] * len(closes)
    return [S.Candle(ts=i, open=c, high=c * 1.01, low=c * 0.99, close=c, volume=100, fees=f)
            for i, (c, f) in enumerate(zip(closes, fees, strict=True))]


def test_chop_high_when_price_goes_back_and_forth() -> None:
    choppy = S.candle_metrics(candles([1, 1.05, 1, 1.05, 1, 1.05, 1, 1.05, 1, 1.05, 1, 1.05, 1.01]))
    trending = S.candle_metrics(candles([1 + 0.02 * i for i in range(13)]))
    assert choppy["chop"] is not None and trending["chop"] is not None
    assert choppy["chop"] > 10 and abs(trending["chop"] - 1) < 1e-6
    assert trending["trend"] > 3


def test_fee_velocity_and_changes() -> None:
    m = S.candle_metrics(candles([1.0] * 12 + [1.1], fees=[1.0] * 12 + [3.0]))
    assert abs((m["fee_velocity"] or 0) - 3.0) < 1e-9
    assert abs((m["change_5m"] or 0) - 0.1) < 1e-9
    assert abs((m["change_1h"] or 0) - 0.1) < 1e-9


def test_too_little_data() -> None:
    assert S.candle_metrics(candles([1.0]))["chop"] is None


def test_bins_liquidity_usd() -> None:
    bins = [{"bin_id": 9, "x": 0, "y": 2}, {"bin_id": 10, "x": 100, "y": 1}, {"bin_id": 40, "x": 50, "y": 0}]
    out = S.bins_liquidity(bins, 10, usd_x=0.01, usd_y=150)
    assert out["active_bin_usd"] == 100 * 0.01 + 150
    assert out["liq_5_bins_usd"] == out["active_bin_usd"] + 300
    assert S.bins_liquidity(bins, 10, None, 150)["active_bin_usd"] is None


def test_safety_flags() -> None:
    ok, bad = S.safety({"mint_disabled": True, "freeze_disabled": True, "top10_pct": 20, "dev_pct": 1})
    assert ok and not bad
    ok, bad = S.safety({"mint_disabled": False, "freeze_disabled": True, "top10_pct": 60})
    assert not ok and len(bad) == 2


def test_score_caps_unsafe_tokens_and_rewards_fees() -> None:
    good = S.Metrics(fee_tvl_1h=0.012, fee_velocity=2.0, chop=6, trend=0.5, active_bin_usd=400, volume_burst=3,
                     safety_ok=True)
    sc, parts, reasons = S.score(good, amount_usd=100)
    assert sc >= S.ENTRY_SCORE and parts["fees"] == 1.0 and any("fee/TVL" in r for r in reasons)
    unsafe = S.Metrics(**{**good.__dict__, "safety_ok": False, "safety_reasons": ["mint authority is ON"]})
    assert S.score(unsafe, 100)[0] <= 30


def test_suggestion_shapes() -> None:
    choppy = S.suggestion(S.Metrics(volatility_5m=0.01, chop=5, safety_ok=True), 100, 0, 100)
    assert choppy["shape"] == "spot" and choppy["side"] == "both"
    assert choppy["bin_range"][0] == -choppy["bin_range"][1]
    topped = S.suggestion(S.Metrics(volatility_5m=0.02, change_1h=0.5, from_1h_high=-0.01, safety_ok=True), 100, 0, 100)
    assert topped["side"] == "sol_below" and topped["bin_range"][1] == 0
    wild = S.suggestion(S.Metrics(volatility_5m=0.09, chop=2, safety_ok=True), 100, 0, 100)
    assert wild["shape"] == "bidask" and wild["risk"] == "high"


def test_size_cap_from_active_bin() -> None:
    sug = S.suggestion(S.Metrics(volatility_5m=0.0001, chop=5, active_bin_usd=80, safety_ok=True), 100, 0, 1000)
    # range of 3 bins (minimum width 3% at bin step 100) -> cap = 0.2*80/0.8 = 20 per bin
    per_bin = S.MAX_SHARE_OF_ACTIVE_BIN * 80 / (1 - S.MAX_SHARE_OF_ACTIVE_BIN)
    n = sug["bin_range"][1] - sug["bin_range"][0] + 1
    assert math.isclose(sug["max_size_usd"], round(per_bin * n, 2)) and sug["size_usd"] < 1000


def test_exit_check_rules() -> None:
    m = S.Metrics(fee_velocity=0.4, volume_burst=2, safety_ok=True)
    assert S.exit_check(m, 1.0, 0.9, 1.1, entry_fee_velocity=2.0, minutes_out_of_range=0)["action"] == "EXIT"
    calm = S.Metrics(fee_velocity=1.5, volume_burst=2, safety_ok=True, trend=0.2)
    assert S.exit_check(calm, 1.0, 0.9, 1.1, 1.6, 0)["action"] == "HOLD"
    out = S.exit_check(calm, 1.3, 0.9, 1.1, 1.6, minutes_out_of_range=20)
    assert out["action"] == "RE-CENTER" and not out["in_range"]
    assert S.exit_check(calm, 1.3, 0.9, 1.1, 1.6, minutes_out_of_range=5)["action"] == "HOLD"
