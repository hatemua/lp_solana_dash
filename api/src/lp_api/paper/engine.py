"""Exit decisions for one paper position (pure). The runner calls decide() every tick with the token's live row and
executes what it returns with a real Jupiter sell quote."""

from dataclasses import dataclass, field
from typing import Any

from .strategies import Exit

CRASH_EXITS = ("rug", "fast_drop", "stop_loss", "sell_pressure", "data_gap")
DATA_GAP_TICKS = 20                         # ~10 min without a price for the token


@dataclass
class Pos:
    id: int
    bot_id: int
    mint: str
    symbol: str | None
    narrative: str | None
    decimals: int
    opened_at: float                        # epoch seconds
    entry_price: float                      # USD mid price at entry
    entry_liquidity: float
    usd_in: float
    tokens: float                           # bought (after swap costs)
    tokens_left: float
    peak_price: float
    last_price: float
    tp_done: bool = False
    misses: int = 0
    quote_fails: int = 0
    px_log: list[tuple[float, float]] = field(default_factory=list)

    def gain_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100

    def state(self) -> dict[str, Any]:
        return {"tp_done": self.tp_done, "misses": self.misses, "quote_fails": self.quote_fails,
                "px_log": self.px_log[-12:]}


def decide(p: Pos, row: dict[str, Any] | None, now: float, x: Exit, narrative_cold: bool = False,
           stopping: bool = False) -> tuple[float, str] | None:
    """(share of the remaining tokens to sell, reason), or None to hold. Updates the price log and the peak."""
    if stopping:
        return 1.0, "bot_stopped"
    if row is None or not row.get("price"):
        p.misses += 1
        return (1.0, "data_gap") if p.misses >= DATA_GAP_TICKS else None
    p.misses = 0
    price = float(row["price"])
    p.px_log = [(t, v) for t, v in p.px_log if now - t <= 150] + [(now, price)]
    p.last_price = price
    p.peak_price = max(p.peak_price, price)
    gain = p.gain_pct(price)

    liq = row.get("liquidity")
    if liq is not None and p.entry_liquidity > 0 and liq <= p.entry_liquidity * (1 - x.rug_liquidity_drop_pct / 100):
        return 1.0, "rug"
    if x.fast_drop_pct:
        recent = max(v for t, v in p.px_log if now - t <= 120)
        if price <= recent * (1 - x.fast_drop_pct / 100):
            return 1.0, "fast_drop"
    if gain <= -x.stop_loss_pct:
        return 1.0, "stop_loss"
    b5, s5 = row.get("buy_5m") or 0, row.get("sell_5m") or 0
    if x.sell_pressure_exit and b5 + s5 >= 5_000 and s5 > 3 * max(b5, 1) and (row.get("chg_5m") or 0) <= -5:
        return 1.0, "sell_pressure"
    if x.take_profit_pct is not None and not p.tp_done and gain >= x.take_profit_pct:
        return (x.take_profit_part, "take_profit") if x.take_profit_part < 1 else (1.0, "take_profit")
    if x.trail_trigger_pct is not None and x.trail_drop_pct is not None:
        if p.gain_pct(p.peak_price) >= x.trail_trigger_pct and price <= p.peak_price * (1 - x.trail_drop_pct / 100):
            return 1.0, "trailing_stop"
    if narrative_cold:
        return 1.0, "narrative_cooled"
    if (now - p.opened_at) / 3600 >= x.max_hours:
        return 1.0, "time"
    return None
