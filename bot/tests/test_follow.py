from lp_bot.follow import day_stop, decide


def test_decide_take_profit_and_stop_loss() -> None:
    assert decide(5.0, 5.0, 8.0) == "take_profit"
    assert decide(4.9, 5.0, 8.0) is None
    assert decide(-8.0, 5.0, 8.0) == "stop_loss"
    assert decide(-7.9, 5.0, 8.0) is None
    assert decide(50.0, None, None) is None             # no own exits: the bot closes with the house position
    assert decide(-50.0, None, 10.0) == "stop_loss"


def test_day_stop_loss_and_target() -> None:
    b = {"day_loss_sol": 0.05, "day_target_sol": 0.1}
    assert day_stop(b, 0.0) is None
    assert "loss" in (day_stop(b, -0.05) or "")
    assert "target" in (day_stop(b, 0.12) or "")
    assert day_stop({"day_loss_sol": None, "day_target_sol": None}, -5.0) is None
