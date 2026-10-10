import math

from lp_bot import sim as S

STEP = 0.01


def test_bid_ask_ladder_shape_and_amount() -> None:
    bins = S.bid_ask(1.0, 0.45, 0.95, STEP, 1.0, "bid")
    assert math.isclose(sum(b.sol for b in bins), 1.0)
    assert all(0.45 <= b.price <= 0.95 for b in bins)
    deeper = sorted(bins, key=lambda b: b.price)
    assert deeper[0].sol > deeper[-1].sol                     # bid-ask: more SOL far from the price


def test_bid_leg_converts_on_the_way_down_and_back() -> None:
    leg = S.Leg("A", S.bid_ask(1.0, 0.45, 0.95, STEP, 1.0, "bid"))
    assert math.isclose(leg.value(1.0), 1.0) and leg.converted(1.0) == 0
    assert leg.converted(0.40) == 1.0 and leg.sol_held(0.40) == 0
    assert leg.value(0.40) < leg.value(0.7) < 1.0             # holding tokens bought higher
    assert math.isclose(leg.value(1.0), 1.0)                  # price back above: all SOL again, no loss


def test_spot_two_sided_value_at_entry() -> None:
    bins = S.spot_two_sided(2.0, 10, STEP, sol_amount=1.0, tok_amount=0.5)
    leg = S.Leg("S", bins)
    assert math.isclose(leg.value(2.0), 1.0 + 0.5 * 2.0)
    assert math.isclose(leg.tokens(2.0), 0.5) and leg.tokens(3.0) == 0    # above the range everything is sold


def test_fee_share_split_with_other_lps() -> None:
    leg = S.Leg("A", [S.Bin(price=1.0, sol=1.0)])
    # price sits in our bin; we hold $100, others $100: half of the bin's fees
    got = S.fee_share([leg], 1.0, 1.0, STEP, fees_usd=10, sol_usd=100, others_usd=lambda p: 100.0)
    assert math.isclose(got, 5.0)
    # price far away: nothing
    assert S.fee_share([leg], 2.0, 2.0, STEP, 10, 100, lambda p: 0.0) == 0
    # move across 3 bins, ours is one of them and alone: a third of the fees
    assert math.isclose(S.fee_share([leg], 1.0 * 1.01, 1.0 / 1.01, STEP, 9, 100, lambda p: 0.0), 3.0)


def test_leg_roundtrip_dict() -> None:
    leg = S.Leg("F", S.bid_ask(1.0, 1.01, 1.3, STEP, 5.0, "ask"))
    assert S.Leg.from_dict(leg.to_dict()) == leg
