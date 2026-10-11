from lp_bot.engine import Position
from lp_bot.guards import TokenFlow, entry_guard, exit_pressure, exit_warning, portfolio_pause
from lp_bot.sim import Leg, spot_two_sided
from lp_bot.strategies import ExitRules

CALM = TokenFlow(chg5m_pct=0.5, chg1h_pct=3, buy5m=5_000, sell5m=5_500, holder_change_1h_pct=0.5)


def test_entry_guard() -> None:
    assert entry_guard(CALM, 0.02, False) == []
    assert entry_guard(CALM, 0.02, True) == ["crashed_recently"]
    assert "dumping_5m" in entry_guard(TokenFlow(chg5m_pct=-9), None, False)
    assert "dumping_1h" in entry_guard(TokenFlow(chg1h_pct=-30), None, False)
    assert "sell_pressure" in entry_guard(TokenFlow(buy5m=1_000, sell5m=4_000), None, False)
    assert "sell_pressure" not in entry_guard(TokenFlow(buy5m=100, sell5m=400), None, False)   # too small to judge
    assert "lp_exodus" in entry_guard(CALM, -0.25, False)
    assert "holders_leaving" in entry_guard(TokenFlow(holder_change_1h_pct=-3), None, False)


def test_exit_pressure_and_portfolio_pause() -> None:
    assert exit_pressure(TokenFlow(chg5m_pct=-6, buy5m=1_000, sell5m=4_000))
    assert not exit_pressure(TokenFlow(chg5m_pct=-2, buy5m=1_000, sell5m=4_000))      # sells but no drop
    assert not exit_pressure(None)
    assert portfolio_pause(-30, 25, 0) and portfolio_pause(-5, 25, -5) and portfolio_pause(-5, 25, -1) is None


def spot_pos() -> Position:
    legs = [Leg("S", spot_two_sided(1.0, 10, 0.01, 0.5, 0.5))]
    return Position(strategy="pro_spot", pool="P", name="T-SOL", opened_at=0, entry_price=1.0, capital_sol=1.0,
                    step=0.01, sell_cost=0.015, legs=legs, last_price=1.0)


def none(_: float) -> float:
    return 0.0


def test_fast_drop_rug_and_pressure_exits() -> None:
    rules = ExitRules(stop=-99, crash_pct=None, out_below_min=999)
    p = spot_pos()
    assert p.step_minute(60, 1.0, 0, 100, none, rules) is None
    assert p.step_minute(120, 0.93, 0, 100, none, rules) == "fast_drop"               # -7% in a minute
    q = spot_pos()
    assert q.step_minute(60, 0.99, 0, 100, none, rules, tvl_drop_15m=-0.35) == "rug"
    r = spot_pos()
    assert r.step_minute(60, 0.99, 0, 100, none, rules, danger="lp_exodus") == "lp_exodus"


def test_exit_warnings() -> None:
    assert exit_warning(CALM, 0.0) is None
    assert exit_warning(TokenFlow(chg5m_pct=-6, buy5m=1_000, sell5m=4_000), None) == "sell_pressure"
    assert exit_warning(TokenFlow(chg5m_pct=-9), None) == "dumping"
    assert exit_warning(CALM, -0.25) == "lp_exodus"
    assert exit_warning(TokenFlow(holder_change_1h_pct=-4), None) == "holders_leaving"


def test_calm_entry_has_a_margin_below_the_exit_warnings() -> None:
    from lp_bot.guards import calm_entry
    assert calm_entry(None) == ["no_flow"]
    assert calm_entry(TokenFlow(chg5m_pct=0.5, chg1h_pct=2, buy5m=3000, sell5m=2500)) == []
    assert "falling_5m" in calm_entry(TokenFlow(chg5m_pct=-4, buy5m=3000, sell5m=2000))
    assert "sellers_ahead" in calm_entry(TokenFlow(chg5m_pct=0, buy5m=1000, sell5m=1900))   # passed the old guard
