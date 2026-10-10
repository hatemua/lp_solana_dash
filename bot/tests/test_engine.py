from lp_bot.engine import Position
from lp_bot.sim import Leg, bid_ask
from lp_bot.strategies import ExitRules

STEP = 0.01
SOL_USD = 100.0


def pos(flip: bool = True) -> Position:
    legs = [Leg("A", bid_ask(1.0, 0.45, 0.95, STEP, 0.9, "bid"))]
    return Position(strategy="topped_bid", pool="P", name="T-SOL", opened_at=0, entry_price=1.0, capital_sol=1.0,
                    step=STEP, sell_cost=0.03, legs=legs, idle_sol=0.1, last_ts=0, last_price=1.0)


def nobody(_: float) -> float:
    return 0.0


def test_take_profit_on_fees() -> None:
    p = pos()
    rules = ExitRules(flip=True)
    reason = None
    t = 0
    price = 0.94                                   # inside our top bins: we earn while the pool trades
    while reason is None and t < 600:
        t += 60
        reason = p.step_minute(t, price, pool_fees_usd=2.0, sol_usd=SOL_USD, others_usd=nobody, rules=rules)
    assert reason == "take_profit" and p.fees_sol > 0


def test_flip_after_fill_and_bounce() -> None:
    p = pos()
    rules = ExitRules(flip=True, stop=-99, out_below_min=999)
    t = 0
    for price in [0.8, 0.6, 0.5, 0.48, 0.46]:     # falls through most of leg A
        t += 60
        assert p.step_minute(t, price, 0.0, SOL_USD, nobody, rules) is None
    assert not p.flipped
    t += 60
    p.step_minute(t, 0.46 * 1.06, 0.0, SOL_USD, nobody, rules)   # +6% bounce from the low
    assert p.flipped and any(lg.name == "F" for lg in p.legs)
    f = next(lg for lg in p.legs if lg.name == "F")
    assert f.lowest() > 0.46 * 1.06 and f.tokens(0.49) > 0


def test_out_above_and_rug() -> None:
    p = pos()
    rules = ExitRules(out_above_min=3)
    reasons = [p.step_minute(60 * i, 1.2, 0.0, SOL_USD, nobody, rules) for i in range(1, 4)]
    assert reasons == [None, None, "out_above"]
    p2 = pos()
    assert p2.step_minute(60, 0.99, 0.0, SOL_USD, nobody, ExitRules(), tvl_drop_15m=-0.6) == "rug"


def test_stop_when_price_collapses() -> None:
    p = pos()
    assert p.step_minute(60, 0.2, 0.0, SOL_USD, nobody, ExitRules(stop=-15)) == "stop"


def test_state_roundtrip() -> None:
    from datetime import UTC, datetime

    p = pos()
    p.step_minute(60, 0.9, 1.0, SOL_USD, nobody, ExitRules())
    row = {"strategy": p.strategy, "pool": p.pool, "name": p.name, "opened_at": datetime.fromtimestamp(0, UTC),
           "entry_price": 1.0, "state": p.state(), "id": 7}
    q = Position.from_row(row)
    assert q.id == 7 and abs(q.net_pct(0.9) - p.net_pct(0.9)) < 1e-12


def test_crash_exit_and_failed_flip() -> None:
    p = pos()
    rules = ExitRules(crash_pct=-25, crash_window_min=30, stop=-99, out_below_min=999)
    assert p.step_minute(60, 0.99, 0.0, SOL_USD, nobody, rules) is None
    assert p.step_minute(120, 0.70, 0.0, SOL_USD, nobody, rules) == "crash"        # -29% within 30 min
    q = pos()
    rules = ExitRules(flip=True, flip_stop=True, stop=-99, out_below_min=999)
    t = 0
    for price in [0.8, 0.6, 0.5, 0.46, 0.46 * 1.06]:                                # fill, then +6% bounce: flip
        t += 60
        q.step_minute(t, price, 0.0, SOL_USD, nobody, rules)
    assert q.flipped
    assert q.step_minute(t + 60, 0.45, 0.0, SOL_USD, nobody, rules) == "flip_failed"
