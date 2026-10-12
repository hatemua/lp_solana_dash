import pytest
from pydantic import ValidationError

from lp_api.lpbots import CreateBody, create_message, owner_message, strategy_stats


def test_strategy_stats_for_tp_and_sl_hints() -> None:
    rows = [{"net_pct": x, "pnl_usd": x} for x in (4.0, -2.0, 1.0, 8.0, -6.0)]
    s = strategy_stats(rows)
    assert s["closed"] == 5 and s["win_rate"] == 60.0
    assert s["median_win_pct"] == 4.0 and s["median_loss_pct"] == -2.0
    assert s["best_pct"] == 8.0 and s["worst_pct"] == -6.0 and s["pnl_usd"] == 5.0
    assert strategy_stats([])["win_rate"] is None


def test_limits_are_bounded() -> None:
    CreateBody(name="a", strategy="auto", take_profit_pct=5, stop_loss_pct=8)
    with pytest.raises(ValidationError):
        CreateBody(name="a", strategy="auto", stop_loss_pct=90)          # a stop loss can't be wider than 50 %
    with pytest.raises(ValidationError):
        CreateBody(name="a", strategy="auto", max_open=20)
    with pytest.raises(ValidationError):
        CreateBody(name="a", strategy="auto", mode="real")


def test_signed_messages_name_the_bot_and_action() -> None:
    assert owner_message(7, "withdraw", 1800000000) == "LP Dash bot 7: withdraw at 1800000000"   # = executor's text
    assert '"My bot"' in create_message("My bot", 1)
