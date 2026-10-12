"""Crash guards, in three layers. Pure functions; the bot applies them every minute.

1. Entry (don't catch a falling knife): the token is not dumping (5 min, 1 h), sellers don't dominate, LPs are not
   pulling liquidity, holders are not leaving, and the token did not crash on us in the last 24 h.
2. In position (get out before / during the crash), see engine.Position.step_minute: early warnings while we hold
   the token (dumping -8% in 5 min, sellers 3x buyers while falling, LPs pulling 20% in 30 min, holders leaving),
   fast drop (-6% within 3 min), crash vs the recent high, rug (TVL -30% in 15 min), stop.
3. Portfolio (stop the bleeding): daily loss limit, SOL itself dumping, token blacklist after a crash exit.
"""

from dataclasses import dataclass

CRASH_EXITS = ("crash", "fast_drop", "rug", "sell_pressure", "dumping", "lp_exodus", "holders_leaving", "stop",
               "flip_failed", "data_gap")


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


def calm_entry(flow: TokenFlow | None) -> list[str]:
    """V2 entry, a margin below the exit warnings: a token close to a warning gets sold minutes after we buy it."""
    if flow is None or flow.chg5m_pct is None:
        return ["no_flow"]
    bad = []
    if flow.chg5m_pct <= -3:
        bad.append("falling_5m")
    if flow.chg1h_pct is not None and flow.chg1h_pct <= -10:
        bad.append("falling_1h")
    if flow.buy5m + flow.sell5m >= 500 and flow.sell5m > 1.2 * flow.buy5m:
        bad.append("sellers_ahead")
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


@dataclass
class DayRules:
    target_usd: float = 30.0          # day's result per strategy at which we stop and close: the day is made
    lock_min_peak_usd: float = 10.0   # the profit lock starts once the day was up this much
    lock_giveback: float = 0.4        # stop when the day falls this fraction below its peak (+$41 -> stop at +$24.6)
    streak: int = 3                   # this many losses in a row ...
    streak_pause_h: float = 6.0       # ... pause new entries this long (the market is dumping)


def day_guard(closed_today: list[float], open_usd: float, recent: list[tuple[float, float]], now: float,
              r: DayRules) -> tuple[str | None, bool]:
    """(reason to stop new entries, close the open positions too) for one strategy.

    closed_today: today's closed P&L (USD) in close order; open_usd: open positions' P&L now;
    recent: (close time, P&L) of the latest closed positions, newest first.
    Auto 2026-10-12: up +$41.24 at 03:25 UTC, gave it back to +$8.95 by 12:45 with no rule to keep it.
    """
    cum, peak = 0.0, 0.0
    for pnl in closed_today:
        cum += pnl
        peak = max(peak, cum)
    day = cum + open_usd
    if day >= r.target_usd:
        return f"day target reached (${day:+.2f} >= ${r.target_usd:.0f}): profit kept", True
    if peak >= r.lock_min_peak_usd and day <= peak * (1 - r.lock_giveback):
        return f"profit lock: day ${day:+.2f}, peak ${peak:+.2f} (gave back {1 - day / peak:.0%})", True
    last = recent[: r.streak]
    if len(last) == r.streak and all(p < 0 for _, p in last) and now - last[0][0] < r.streak_pause_h * 3600:
        return f"{r.streak} losses in a row: paused until {r.streak_pause_h:.0f} h after the last one", False
    return None, False
