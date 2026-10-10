"""Signals v0: LP score, entry, range/shape/size and exit check, from data the indexer already stores.

Heuristics, NOT yet validated by a backtest. The M4 signal engine replaces these functions (same inputs/outputs)
with backtest-tuned rules. Every output carries `version: "v0-heuristic"` and its reasons.
"""

import itertools
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

VERSION = "v0-heuristic"

# score weights (sum 100) and thresholds; M4 tunes these on stored history
WEIGHTS = {"fees": 30, "fee_velocity": 15, "chop": 20, "trend": 15, "room": 10, "volume": 10}
ENTRY_SCORE = 60
MIN_POOL_AGE_H = 1.0                # brand-new pools: fee/TVL is meaningless and rugs are common
MIN_FEE_VELOCITY = 0.5              # fees fading to under half of the last hour's pace: no entry
MAX_SHARE_OF_ACTIVE_BIN = 0.20      # size so our share of the active bin stays below this
HOLD_MINUTES = 60                   # expected hold used for the range width


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    fees: float


@dataclass
class Metrics:
    fee_tvl_1h: float | None = None
    fees_1h: float | None = None
    fee_velocity: float | None = None       # last 5 min fees / average 5 min fees of the last hour
    volume_burst: float | None = None       # 1 h volume / average hour of 24 h
    change_5m: float | None = None
    change_15m: float | None = None
    change_1h: float | None = None
    volatility_5m: float | None = None      # stdev of 5-min log returns (fraction)
    chop: float | None = None               # sum |moves| / |net move| over the last hour
    trend: float | None = None              # net move / (volatility * sqrt(n))
    from_1h_high: float | None = None
    from_24h_high: float | None = None
    active_bin_usd: float | None = None
    liq_5_bins_usd: float | None = None
    liq_20_bins_usd: float | None = None
    tvl: float | None = None
    tvl_flow_1h: float | None = None
    safety_ok: bool = False
    safety_reasons: list[str] = field(default_factory=list)


def _chg(a: float | None, b: float | None) -> float | None:
    return (b / a - 1) if a and b else None


def candle_metrics(candles: list[Candle]) -> dict[str, float | None]:
    """Price behaviour from 5-min candles (oldest first)."""
    closes = [c.close for c in candles if c.close and c.close > 0]
    out: dict[str, float | None] = {k: None for k in ("change_5m", "change_15m", "change_1h", "volatility_5m",
                                                      "chop", "trend", "from_1h_high", "from_24h_high",
                                                      "fee_velocity")}
    if len(closes) < 2:
        return out
    last = closes[-1]
    out["change_5m"] = _chg(closes[-2], last)
    out["change_15m"] = _chg(closes[-4], last) if len(closes) >= 4 else None
    out["change_1h"] = _chg(closes[-13], last) if len(closes) >= 13 else None
    window = closes[-13:]
    rets = [math.log(b / a) for a, b in itertools.pairwise(window) if a > 0 and b > 0]
    if len(rets) >= 3:
        vol = statistics.pstdev(rets)
        out["volatility_5m"] = vol
        net = abs(math.log(window[-1] / window[0]))
        moves = sum(abs(r) for r in rets)
        hi = max(c.high for c in candles[-12:] if c.high) if any(c.high for c in candles[-12:]) else window[-1]
        lo = min(c.low for c in candles[-12:] if c.low) if any(c.low for c in candles[-12:]) else window[0]
        span = math.log(hi / lo) if hi > 0 and lo > 0 else 0.0
        # a flat price is not "choppy": below 0.5% of total movement the index means nothing
        out["chop"] = moves / max(net, 0.1 * span, 1e-9) if moves >= 0.005 else None
        out["trend"] = (math.log(window[-1] / window[0]) / (vol * math.sqrt(len(rets)))) if vol > 0 else 0.0
    highs_1h = [c.high for c in candles[-12:] if c.high]
    highs_24 = [c.high for c in candles[-288:] if c.high]
    out["from_1h_high"] = _chg(max(highs_1h), last) if highs_1h else None
    out["from_24h_high"] = _chg(max(highs_24), last) if highs_24 else None
    fees = [c.fees or 0 for c in candles[-13:]]
    if len(fees) >= 6 and sum(fees[:-1]) > 0:
        out["fee_velocity"] = fees[-1] / (sum(fees[:-1]) / len(fees[:-1]))
    if out["chop"] is not None:
        out["chop"] = min(out["chop"], 50.0)
    return out


def bins_liquidity(bins: list[dict[str, Any]], active_bin: int, usd_x: float | None, usd_y: float | None) \
        -> dict[str, float | None]:
    if not bins or usd_x is None or usd_y is None:
        return {"active_bin_usd": None, "liq_5_bins_usd": None, "liq_20_bins_usd": None}

    def val(b: dict[str, Any]) -> float:
        return float(b.get("x") or 0) * usd_x + float(b.get("y") or 0) * usd_y

    return {
        "active_bin_usd": sum(val(b) for b in bins if b["bin_id"] == active_bin),
        "liq_5_bins_usd": sum(val(b) for b in bins if abs(b["bin_id"] - active_bin) <= 5),
        "liq_20_bins_usd": sum(val(b) for b in bins if abs(b["bin_id"] - active_bin) <= 20),
    }


def safety(pool: dict[str, Any]) -> tuple[bool, list[str]]:
    bad = []
    if pool.get("mint_disabled") is False:
        bad.append("mint authority is ON")
    if pool.get("freeze_disabled") is False:
        bad.append("freeze authority is ON")
    if (pool.get("top10_pct") or 0) > 40:
        bad.append(f"top-10 holders {pool['top10_pct']:.0f}% (> 40%)")
    if (pool.get("dev_pct") or 0) > 10:
        bad.append(f"dev holds {pool['dev_pct']:.1f}% (> 10%)")
    if pool.get("is_sus"):
        bad.append("token flagged suspicious by Jupiter")
    return not bad, bad


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(m: Metrics, amount_usd: float) -> tuple[float, dict[str, float], list[str]]:
    """LP score 0-100 with its components and human-readable reasons."""
    parts: dict[str, float] = {}
    reasons: list[str] = []
    # fees: fee/TVL in the last hour; 1%/h or more = full marks
    ft = m.fee_tvl_1h or 0
    parts["fees"] = clamp01(ft / 0.01)
    reasons.append(f"fee/TVL 1h {ft * 100:.2f}%")
    # fee velocity: fees now vs the last hour (rising is good)
    fv = m.fee_velocity
    parts["fee_velocity"] = 0.5 if fv is None else clamp01(fv / 2)
    reasons.append("fee velocity n/a" if fv is None else f"fee velocity {fv:.2f}x")
    # chop: back-and-forth trading is what LPs earn on
    ch = m.chop
    parts["chop"] = 0.5 if ch is None else clamp01((ch - 1) / 5)
    reasons.append("chop n/a" if ch is None else f"chop index {ch:.1f}")
    # trend: hard down-trends hurt (inventory loses value)
    tr = m.trend
    parts["trend"] = 0.5 if tr is None else clamp01(1 + tr / 3) if tr < 0 else 1.0
    reasons.append("trend n/a" if tr is None else f"trend {tr:+.2f}")
    # room: our expected share of the active bin
    if m.active_bin_usd is not None:
        share = amount_usd / (m.active_bin_usd + amount_usd) if amount_usd > 0 else 0
        parts["room"] = clamp01(share / MAX_SHARE_OF_ACTIVE_BIN)
        reasons.append(f"our share of the active bin {share * 100:.1f}% at ${amount_usd:,.0f}")
    else:
        parts["room"] = 0.5
        reasons.append("bin liquidity n/a")
    # volume burst
    vb = m.volume_burst
    parts["volume"] = 0.5 if vb is None else clamp01(vb / 3)
    reasons.append("volume burst n/a" if vb is None else f"volume burst {vb:.1f}x")
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    if not m.safety_ok:
        total = min(total, 30.0)
        reasons.append("safety: " + "; ".join(m.safety_reasons))
    return round(total, 1), {k: round(v, 3) for k, v in parts.items()}, reasons


def entry_decision(score_: float, m: Metrics, pool_age_h: float | None) -> tuple[bool, list[str]]:
    """Entry = score >= threshold and safety OK and the pool is not brand new and fees are not fading."""
    why = []
    if score_ < ENTRY_SCORE:
        why.append(f"score {score_:.0f} < {ENTRY_SCORE}")
    if not m.safety_ok:
        why.append("safety check failed")
    if pool_age_h is not None and pool_age_h < MIN_POOL_AGE_H:
        why.append(f"pool is only {pool_age_h * 60:.0f} min old")
    if m.fee_velocity is not None and m.fee_velocity < MIN_FEE_VELOCITY:
        why.append(f"fees fading (velocity {m.fee_velocity:.2f}x)")
    return not why, why


def bins_for(pct: float, bin_step: int) -> int:
    return max(1, math.ceil(math.log(1 + pct) / math.log(1 + bin_step / 10_000))) if pct > 0 else 1


def suggestion(m: Metrics, bin_step: int, active_bin: int | None, amount_usd: float) -> dict[str, Any]:
    """Range width from volatility (+/- k * stdev over the hold), shape from chop/volatility/trend, size cap."""
    vol = m.volatility_5m if m.volatility_5m is not None else 0.03
    width = max(0.03, min(0.6, 2.0 * vol * math.sqrt(HOLD_MINUTES / 5)))
    topped = (m.change_1h or 0) > 0.3 and (m.from_1h_high or 0) > -0.05
    if topped:
        shape, side, why = "bidask", "sol_below", "token pumped >30% in 1 h and sits near its high: one-sided SOL below"
    elif vol > 0.06:
        shape, side, why = "bidask", "both", "very volatile: bid-ask puts more liquidity at the edges"
    elif (m.chop or 0) >= 3:
        shape, side, why = "spot", "both", "choppy: spot around the price earns on every pass"
    else:
        shape, side, why = "curve", "both", "calm: curve concentrates liquidity near the price"
    n = bins_for(width, bin_step)
    if side == "both":
        n = min(n, 34)
        rng = (active_bin - n, active_bin + n) if active_bin is not None else None
    else:
        n = min(n, 68)
        rng = (active_bin - n, active_bin) if active_bin is not None else None
    max_size = None
    if m.active_bin_usd:
        # our share s = a / (B + a) <= S  ->  a <= S*B/(1-S); spread over ~n bins, the active bin gets ~1/n of it
        per_bin = MAX_SHARE_OF_ACTIVE_BIN * m.active_bin_usd / (1 - MAX_SHARE_OF_ACTIVE_BIN)
        max_size = per_bin * max(1, (rng[1] - rng[0] + 1) if rng else 1)
    size = min(amount_usd, max_size) if max_size else amount_usd
    exp_fees = None
    if m.fees_1h is not None and m.liq_20_bins_usd:
        exp_fees = m.fees_1h * size / (m.liq_20_bins_usd + size)
    risk = "high" if vol > 0.06 or not m.safety_ok or (m.trend or 0) < -2 else "medium" if vol > 0.03 else "low"
    return {
        "range_pct": round(width * 100, 1),
        "side": side,
        "shape": shape,
        "shape_reason": why,
        "bins": n,
        "bin_range": list(rng) if rng else None,
        "size_usd": round(size, 2),
        "max_size_usd": round(max_size, 2) if max_size else None,
        "expected_fees_per_hour_usd": round(exp_fees, 4) if exp_fees is not None else None,
        "risk": risk,
    }


def exit_check(m: Metrics, price_now: float | None, range_low: float, range_high: float,
               entry_fee_velocity: float | None, minutes_out_of_range: float) -> dict[str, Any]:
    reasons: list[str] = []
    action = "HOLD"
    in_range = price_now is not None and range_low <= price_now <= range_high
    if m.fee_velocity is not None and entry_fee_velocity and m.fee_velocity < 0.5 * entry_fee_velocity:
        reasons.append(f"fee velocity fell to {m.fee_velocity:.2f}x from {entry_fee_velocity:.2f}x at entry")
        action = "EXIT"
    if m.volume_burst is not None and m.volume_burst < 1.0:
        reasons.append(f"volume burst over ({m.volume_burst:.1f}x the 24 h average)")
        action = "EXIT"
    if price_now is not None and in_range and (m.trend or 0) < -1.5 \
            and price_now < range_low + (range_high - range_low) / 3:
        reasons.append("price in the lower third of the range and trending down")
        action = "EXIT"
    if (m.tvl_flow_1h or 0) < -0.2 * (m.tvl or 1):
        reasons.append(f"liquidity pulled: {m.tvl_flow_1h:,.0f} USD in 1 h")
        action = "EXIT"
    if not m.safety_ok:
        reasons.append("safety: " + "; ".join(m.safety_reasons))
        action = "EXIT"
    if not in_range and action == "HOLD":
        if minutes_out_of_range >= 15:
            reasons.append(f"out of range for {minutes_out_of_range:.0f} min")
            action = "RE-CENTER" if (m.fee_velocity or 0) >= 1 and m.safety_ok else "EXIT"
        else:
            reasons.append(f"out of range for {minutes_out_of_range:.0f} min (waiting up to 15)")
    if not reasons:
        reasons.append("fees, volume and price behaviour still support the position")
    return {"action": action, "in_range": in_range, "reasons": reasons, "version": VERSION}
