from lp_bot.signals import Bar, chop_stats, fee_velocity, topped
from lp_bot.strategies import BY_NAME, Candidate


def bars_from(closes: list[float], fees: float = 10.0) -> list[Bar]:
    return [Bar(ts=i * 300, high=c * 1.01, low=c * 0.99, close=c, fees=fees) for i, c in enumerate(closes)]


def cand(**kw: object) -> Candidate:
    base = dict(pool="P", name="TOK-SOL", price=1.0, tvl=50_000, fees_1h=1_000, fee_tvl_1h=1.0, bin_step=100,
                base_fee_pct=2.0, mcap=1_000_000, holders=2_000, organic=80, top10=20, mint_off=True, freeze_off=True)
    base.update(kw)
    return Candidate(**base)  # type: ignore[arg-type]


def test_topped_needs_a_pump_and_a_pullback() -> None:
    pump = [1.0] * 20 + [1.0 + 0.05 * i for i in range(1, 11)] + [1.5] * 3       # +50% then flat at the peak
    b = bars_from(pump)
    assert topped(b, 1.5) is None                    # still at the peak
    sig = topped(b, 1.2)                             # 20% below the peak
    assert sig and sig["pump"] >= 1.3 and -0.25 < sig["from_peak"] < -0.15
    assert topped(bars_from([1.0] * 40), 0.8) is None   # no pump before


def test_chop_and_fee_velocity() -> None:
    zig = bars_from([1.0, 1.03] * 8)
    st = chop_stats(zig)
    assert st["chop"] and st["chop"] > 5 and abs(st["trend"]) < 1
    rising = bars_from([1.0] * 15)
    for x in rising[-3:]:
        x.fees = 30.0
    assert fee_velocity(rising) == 3.0


def test_topped_bid_entry_respects_safety() -> None:
    s = BY_NAME["topped_bid"]
    pump = [1.0] * 20 + [1.0 + 0.05 * i for i in range(1, 11)] + [1.5] * 3
    c = cand(price=1.2, bars=bars_from(pump))
    plan = s.entry(c, capital_sol=1.0)
    assert plan and plan.legs[0].name == "A" and plan.idle_sol == 0.1
    assert s.entry(cand(price=1.2, bars=bars_from(pump), mint_off=False), 1.0) is None
    assert s.entry(cand(price=1.2, bars=bars_from(pump), top10=45), 1.0) is None


def test_meridian_filters() -> None:
    s = BY_NAME["meridian"]
    flat = bars_from([1.0] * 15)
    for x in flat[-3:]:
        x.fees = 20.0
    plan = s.entry(cand(bars=flat), 1.0)
    assert plan and len(plan.legs[0].bins) == 69 and plan.legs[0].highest() < 1.0
    assert s.entry(cand(bars=flat, tvl=500_000), 1.0) is None          # too big for Meridian
    assert s.entry(cand(bars=flat, bin_step=20), 1.0) is None


def test_chop_spot_two_sided() -> None:
    s = BY_NAME["chop_spot"]
    zig = bars_from([1.0, 1.03] * 8)
    for x in zig[-3:]:
        x.fees = 30.0
    plan = s.entry(cand(price=1.0, bars=zig), 1.0)
    assert plan and plan.buy_sol == 0.5
    sides = {b.side for b in plan.legs[0].bins}
    assert sides == {"bid", "ask"}


def test_grid_bid_ask_two_sided_weights() -> None:
    from lp_bot.sim import Leg, grid_bid_ask

    leg = Leg("G", grid_bid_ask(1.0, 10, 0.01, sol_amount=1.0, tok_amount=2.0))
    bids = sorted((b for b in leg.bins if b.side == "bid"), key=lambda b: -b.price)
    asks = sorted((b for b in leg.bins if b.side == "ask"), key=lambda b: b.price)
    assert abs(sum(b.sol for b in bids) - 1.0) < 1e-12 and abs(sum(b.tok for b in asks) - 2.0) < 1e-12
    assert bids[-1].sol > bids[0].sol and asks[-1].tok > asks[0].tok           # more liquidity far from the price


def test_new_strategies_filters() -> None:
    zig = bars_from([1.0, 1.03] * 8)
    for x in zig[-3:]:
        x.fees = 30.0
    young, old = cand(bars=zig, token_age_h=3), cand(bars=zig, token_age_h=100)
    assert BY_NAME["grid"].entry(young, 1.0) is None and BY_NAME["grid"].entry(old, 1.0)
    leader = cand(bars=zig, fee_tvl_1h=5.0, fees_1h=5_000, token_age_h=2, mcap=100_000)
    plan = BY_NAME["fee_leader"].entry(leader, 1.0)
    assert plan and {b.side for b in plan.legs[0].bins} == {"bid", "ask"}       # no age or mcap floor
    assert len(plan.legs[0].bins) <= 40 and plan.buy_sol == 0.5                    # spot, close to the price
    assert BY_NAME["fee_leader"].entry(cand(bars=zig, fee_tvl_1h=1.0), 1.0) is None
    pump = bars_from([1.0] * 20 + [1.0 + 0.05 * i for i in range(1, 11)] + [1.5] * 3)
    assert BY_NAME["topped_bid_v2"].entry(cand(price=1.2, bars=pump, token_age_h=5), 1.0) is None
    assert BY_NAME["topped_bid_v2"].entry(cand(price=1.2, bars=pump, token_age_h=50), 1.0)
    rising = bars_from([1.0] * 20 + [1.0 + 0.02 * i for i in range(1, 13)])
    ep = BY_NAME["evil_panda"].entry(cand(price=1.24, bars=rising, token_age_h=100), 1.0)
    assert ep and ep.legs[0].highest() < 1.24 * 0.41 and ep.legs[0].lowest() > 1.24 * 0.09


def test_rabbit_entry_on_rising_token() -> None:
    rising = bars_from([1.0] * 20 + [1.0 + 0.02 * i for i in range(1, 13)])
    for x in rising[-3:]:
        x.fees = 30.0
    plan = BY_NAME["rabbit"].entry(cand(price=1.24, bars=rising, token_age_h=100, fees_1h=2_000), 1.0)
    assert plan and plan.buy_sol == 1.0 and {b.side for b in plan.legs[0].bins} == {"ask"}
    assert plan.legs[0].lowest() > 1.24 and plan.legs[0].highest() <= 1.24 * 1.30
    assert BY_NAME["rabbit"].entry(cand(price=1.24, bars=rising, token_age_h=3, fees_1h=2_000), 1.0) is None
