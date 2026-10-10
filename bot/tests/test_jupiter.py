from lp_bot.jupiter import DEFAULT_COST, MAX_COST, cost_fraction


def test_cost_fraction() -> None:
    assert abs(cost_fraction(1.0, 0.99) - 0.01) < 1e-12        # 1% lost in fees and price impact
    assert cost_fraction(1.0, 1.02) == 0.0                      # stale pool price: never a negative cost
    assert cost_fraction(1.0, 0.5) == MAX_COST
    assert cost_fraction(0.0, 1.0) == DEFAULT_COST
