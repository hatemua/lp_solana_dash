"""Crash guards, in three layers. Pure functions; the bot applies them every minute.

1. Entry (don't catch a falling knife): the token is not dumping (5 min, 1 h), sellers don't dominate, LPs are not
   pulling liquidity, holders are not leaving, and the token did not crash on us in the last 24 h.
2. In position (get out fast): see engine.Position.step_minute: fast drop (-6% within 3 min), crash vs the recent
   high, sell pressure while falling, liquidity pulled (TVL -30% in 15 min).
3. Portfolio (stop the bleeding): daily loss limit, SOL itself dumping, token blacklist after a crash exit.
"""

from dataclasses import dataclass

CRASH_EXITS = ("crash", "fast_drop", "rug", "sell_pressure", "dumping", "lp_exodus", "holders_leaving", "stop",
               "flip_failed")


@dataclass
class TokenFlow:
    """Jupiter token stats (all venues) for one token."""
    chg5m_pct: float | None = None
    chg1h_pct: float | None = None
    buy5m: float = 0.0
    sell5m: float = 0.0
    holder_change_1h_pct: float | None = None


def entry_guard(flow: TokenFlow, tvl_change_30m: float | None, blacklisted: bool) -> list[str]:
    """Reasons not to enter now (empty = OK)."""
    bad = []
    if blacklisted:
        bad.append("crashed_recently")                 # crash-type exit on this token in the last 24 h
    if flow.chg5m_pct is not None and flow.chg5m_pct <= -8:
        bad.append("dumping_5m")
    if flow.chg1h_pct is not None and flow.chg1h_pct <= -25:
        bad.append("dumping_1h")
    if flow.buy5m + flow.sell5m >= 2_000 and flow.sell5m > 2.0 * max(flow.buy5m, 1.0):
        bad.append("sell_pressure")                    # sellers > 2x buyers in the last 5 min
    if tvl_change_30m is not None and tvl_change_30m <= -0.20:
        bad.append("lp_exodus")                        # LPs pulled >= 20% of the pool in 30 min
    if flow.holder_change_1h_pct is not None and flow.holder_change_1h_pct <= -2:
        bad.append("holders_leaving")
    return bad


def exit_pressure(flow: TokenFlow | None) -> bool:
    """In position: sellers > 3x buyers over 5 min while the price falls >= 5%."""
    if flow is None or flow.chg5m_pct is None:
        return False
    return (flow.buy5m + flow.sell5m >= 2_000 and flow.sell5m > 3.0 * max(flow.buy5m, 1.0)
            and flow.chg5m_pct <= -5)


def exit_warning(flow: TokenFlow | None, tvl_change_30m: float | None) -> str | None:
    """In position: early signs that a crash is starting (exit while holding the token, before the stop)."""
    if exit_pressure(flow):
        return "sell_pressure"
    if flow is not None and flow.chg5m_pct is not None and flow.chg5m_pct <= -8:
        return "dumping"
    if tvl_change_30m is not None and tvl_change_30m <= -0.20:
        return "lp_exodus"
    if flow is not None and flow.holder_change_1h_pct is not None and flow.holder_change_1h_pct <= -3:
        return "holders_leaving"
    return None


def portfolio_pause(day_pnl_usd: float, loss_limit_usd: float, sol_chg1h_pct: float | None) -> str | None:
    """No new entries while this returns a reason."""
    if day_pnl_usd <= -abs(loss_limit_usd):
        return f"daily loss limit (${day_pnl_usd:.2f} <= -${abs(loss_limit_usd):.0f})"
    if sol_chg1h_pct is not None and sol_chg1h_pct <= -4:
        return f"SOL dumping ({sol_chg1h_pct:.1f}% in 1 h)"
    return None
