"""Regime signals from 5-min pool bars (oldest first): pump-then-top, chop, fee velocity."""

import itertools
import math
import statistics
from dataclasses import dataclass


@dataclass
class Bar:
    ts: float
    high: float
    low: float
    close: float
    fees: float


def topped(bars: list[Bar], price: float, min_pump: float = 1.30, lo: float = 0.55, hi: float = 0.85) -> dict | None:
    """A pump of >= min_pump within ~5 h, peak in the last 2 h, and the price now between lo and hi of the peak."""
    if len(bars) < 30 or price <= 0:
        return None
    recent = bars[-24:]
    peak_i = max(range(len(bars) - len(recent), len(bars)), key=lambda j: bars[j].high)
    peak = max(bars[peak_i].high, price)
    base = min(b.low for b in bars[max(0, peak_i - 36):peak_i + 1] if b.low > 0)
    if base <= 0 or peak / base < min_pump:
        return None
    if not (lo * peak <= price <= hi * peak):
        return None
    return {"peak": peak, "base": base, "pump": peak / base, "from_peak": price / peak - 1}


def fee_velocity(bars: list[Bar]) -> float | None:
    """Average 5-min fees of the last 15 min / average of the hour before."""
    fees = [b.fees for b in bars[-15:]]
    recent, before = fees[-3:], fees[:-3]
    if len(before) < 4 or sum(before) <= 0:
        return None
    return (sum(recent) / len(recent)) / (sum(before) / len(before))


def chop_stats(bars: list[Bar]) -> dict:
    """Chop index (path / net move), trend (net move in volatility units) and 5-min volatility, last hour."""
    w = [b.close for b in bars[-13:] if b.close > 0]
    out: dict = {"chop": None, "trend": None, "vol": None}
    if len(w) < 6:
        return out
    rets = [math.log(b / a) for a, b in itertools.pairwise(w)]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    out["vol"] = vol
    net = abs(math.log(w[-1] / w[0]))
    moves = sum(abs(r) for r in rets)
    hi = max(b.high for b in bars[-12:])
    lo = min(b.low for b in bars[-12:] if b.low > 0)
    span = math.log(hi / lo) if hi > 0 and lo > 0 else 0.0
    out["chop"] = moves / max(net, 0.1 * span, 1e-9) if moves >= 0.005 else None
    out["trend"] = math.log(w[-1] / w[0]) / (vol * math.sqrt(len(rets))) if vol > 0 else 0.0
    return out


def price_change(bars: list[Bar], price: float, n: int = 12) -> float | None:
    if len(bars) < n or bars[-n].close <= 0:
        return None
    return price / bars[-n].close - 1
