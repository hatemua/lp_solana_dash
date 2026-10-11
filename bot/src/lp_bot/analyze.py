"""Pool analyzer: for one opportunity (token + Meteora pool), which LP shape to open, or skip.

1. Vetoes: token safety, age, the real round trip (buy + sell back, Jupiter quotes), pool price vs market, and a
   falling / sell-pressure token (every shape ends up holding a falling token).
2. Regime from the pool's own bars: sideways (calm or volatile), rising, topped after a pump.
3. Each shape that fits the regime is replayed on the pool's last 6 hours of real per-minute data (price, fees, TVL),
   from a start every 30 min, with the same engine and exit rules the bot uses. Other LPs' liquidity is today's bins.
4. Choice: the shape whose replays are best in the worst case (mean - stdev / sqrt(n)), only if the mean clears
   MIN_EDGE after costs and most replays won. Otherwise: skip, with the reasons.
Pure apart from the inputs; main.py feeds it and stores the result (table pool_analysis), the dashboard reads it.
"""

import math
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .engine import SOFT_EXITS, TX_SOL, Position
from .guards import calm_entry, entry_guard
from .signals import chop_stats, price_change, topped
from .sim import Leg, even_bids_below, spot_two_sided
from .strategies import SELL_COST_ESTIMATE, V2_EXITS, Candidate, ExitRules, Plan, Strategy, V2BidFlip, V2Calm, V2Wide

MIN_EDGE = 1.0              # % per position after all costs
MAX_ROUND_TRIP = 0.03
PRICE_GAP = (-0.03, 0.04)
LOOKBACK_H = 6
START_EVERY_S = 30 * 60
MIN_FOLLOW_S = 60 * 60      # a replay start needs at least an hour of data after it


@dataclass
class Shape:
    key: str
    label: str
    build: Callable[[float, float, float], Plan]          # (price, bin step, capital SOL)
    rules: ExitRules


def _spot(n: int) -> Callable[[float, float, float], Plan]:
    def build(price: float, step: float, sol: float) -> Plan:
        half = sol / 2
        return Plan([Leg("S", spot_two_sided(price, n, step, half, half / price))], 0.0, half, {"bins_each_side": n})
    return build


def _bids(price: float, step: float, sol: float) -> Plan:
    return Plan([Leg("A", even_bids_below(price, 30, step, sol))], 0.0, 0.0, {})


SHAPES = [
    Shape("spot_10", "Spot +/-10 bins", _spot(10),
          ExitRules(take_profit=5, trail_trigger=3, trail_drop=1.5, stop=-8, out_below_min=15, out_above_min=15,
                    max_hours=2, crash_pct=-10, crash_window_min=15, fee_death_min=20, **V2_EXITS)),
    Shape("spot_25", "Spot +/-25 bins", _spot(25), V2Wide.rules),
    Shape("spot_40", "Spot +/-40 bins", _spot(40), V2Calm.rules),
    Shape("bids_30", "SOL bids, 30 bins below, follow the price up", _bids,
          ExitRules(take_profit=5, trail_trigger=3, trail_drop=1.5, stop=-12, out_below_min=30, out_above_min=5,
                    max_hours=4, crash_pct=-15, crash_window_min=15, fee_death_min=30, recenter_above=True,
                    **V2_EXITS)),
    Shape("bid_flip", "SOL bids, fills sold back above cost", _bids, V2BidFlip.rules),
]
BY_KEY = {s.key: s for s in SHAPES}
FIT = {                                  # which shapes make sense in which regime
    "sideways_calm": ["spot_10", "spot_25", "spot_40", "bids_30"],
    "sideways_volatile": ["spot_25", "spot_40", "bid_flip"],
    "rising": ["bids_30", "spot_40"],
    "topped": ["bid_flip", "bids_30"],
    "falling": [],
}
REGIME_TEXT = {
    "sideways_calm": "sideways, calm: the price stays in a narrow range",
    "sideways_volatile": "sideways, volatile: wide ranges stay in range longer",
    "rising": "rising: hold SOL below the price, no token to lose if it turns",
    "topped": "topped after a pump: buy the dip with bids, sell the bounce",
    "falling": "falling or sellers ahead: every shape would hold a falling token",
}
LATEST: dict[str, dict[str, Any]] = {}  # pool -> last analysis (the auto strategy reads it)


def token_vetoes(c: Candidate) -> list[str]:
    bad = []
    if not (c.mint_off and c.freeze_off):
        bad.append("mint or freeze authority on")
    if c.top10 > 30:
        bad.append(f"top 10 holders own {c.top10:.0f}%")
    if c.holders < 500:
        bad.append(f"only {c.holders} holders")
    if c.mcap < 250_000:
        bad.append("market cap < $250k")
    if c.bot_holders_pct > 30:
        bad.append(f"bots hold {c.bot_holders_pct:.0f}%")
    if c.token_age_h is None or c.token_age_h < 24:
        bad.append("token younger than 24 h (crash ~50x more often)")
    if c.tvl < 10_000:
        bad.append("pool TVL < $10k")
    return bad


def regime(c: Candidate) -> tuple[str, list[str]]:
    falling = calm_entry(c.flow) + (entry_guard(c.flow, c.tvl_change_30m, False) if c.flow else [])
    ch = price_change(c.bars, c.price)
    if ch is not None and ch <= -0.05:
        falling.append(f"down {ch:.0%} in 1 h")
    if falling:
        return "falling", sorted(set(falling))
    if topped(c.bars, c.price):
        return "topped", []
    st = chop_stats(c.bars)
    if ch is not None and ch >= 0.05 and (st["trend"] or 0) >= 1:
        return "rising", []
    return ("sideways_calm" if (st["vol"] or 0) < 0.015 else "sideways_volatile"), []


def replay(rows: list[dict[str, Any]], shape: Shape, step: float, capital_sol: float, sol_usd: float,
           others: Any, round_trip: float) -> list[dict[str, Any]]:
    """Open the shape at a start every 30 min of real minute data and run the bot's engine and exits on it."""
    rows = [r for r in rows if r.get("price")]
    if len(rows) < 30:
        return []
    t_end = rows[-1]["t"]
    out, next_start = [], rows[0]["t"]
    for i0, r0 in enumerate(rows):
        if r0["t"] < next_start or r0["t"] > t_end - MIN_FOLLOW_S:
            continue
        next_start = r0["t"] + START_EVERY_S
        plan = shape.build(r0["price"], step, capital_sol)
        n_pos = sum(math.ceil(len(lg.bins) / 69) for lg in plan.legs)
        p = Position(strategy=shape.key, pool="", name="", opened_at=r0["t"], entry_price=r0["price"],
                     capital_sol=capital_sol, step=step, sell_cost=max(SELL_COST_ESTIMATE, round_trip / 2),
                     legs=plan.legs, idle_sol=plan.idle_sol, costs_sol=plan.buy_sol * round_trip / 2 + n_pos * TX_SOL,
                     last_ts=r0["t"], last_price=r0["price"], info={})
        reason = None
        for j in range(i0 + 1, len(rows)):
            r, prev = rows[j], rows[j - 1]
            fees = max(0.0, (r["cum_fees"] or 0) - (prev["cum_fees"] or 0)) if prev["cum_fees"] else 0.0
            if r["t"] - prev["t"] > 600:
                fees = 0.0
            old = next((x for x in rows[max(0, j - 20):j] if x["t"] >= r["t"] - 15 * 60), None)
            drop = (r["tvl"] / old["tvl"] - 1) if old and old["tvl"] and r["tvl"] else None
            reason = p.step_minute(r["t"], r["price"], fees, sol_usd, others, shape.rules, drop, None)
            if reason in SOFT_EXITS and shape.rules.unwind and not p.info.get("unwind") \
                    and p.start_unwind(p.last_price, shape.rules, reason):
                reason = None
            if reason:
                break
        net = p.close(p.last_price)
        out.append({"start": r0["t"], "net_pct": net, "exit": reason or "still_open",
                    "minutes": round((p.last_ts - r0["t"]) / 60), "fees_pct": p.fees_sol / capital_sol * 100})
    return out


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [r["net_pct"] for r in results]
    if not nets:
        return {"n": 0}
    mean = statistics.fmean(nets)
    sd = statistics.pstdev(nets) if len(nets) > 1 else abs(mean)
    exits: dict[str, int] = {}
    for r in results:
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
    return {"n": len(nets), "mean_pct": mean, "median_pct": statistics.median(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets), "worst_pct": min(nets), "best_pct": max(nets),
            "lower_pct": mean - sd / math.sqrt(len(nets)), "fees_pct": statistics.fmean(r["fees_pct"] for r in results),
            "fees_pct_h": sum(r["fees_pct"] for r in results) / max(1 / 60, sum(r["minutes"] for r in results) / 60),
            "hold_min": statistics.median(r["minutes"] for r in results), "exits": exits}


def choose(shapes: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """The fitting shape with the best worst case, if it clears the bar."""
    ok, why_not = [], []
    for s in shapes:
        if not s["fits"]:
            continue
        st = s["stats"]
        if st.get("n", 0) < 4:
            why_not.append(f"{s['label']}: not enough history ({st.get('n', 0)} replays)")
        elif st["mean_pct"] < MIN_EDGE:
            why_not.append(f"{s['label']}: avg {st['mean_pct']:+.1f}% < +{MIN_EDGE:.0f}% after costs")
        elif st["lower_pct"] <= 0:
            why_not.append(f"{s['label']}: worst case {st['lower_pct']:+.1f}% (too uncertain)")
        elif st["win_rate"] < 0.5:
            why_not.append(f"{s['label']}: won only {st['win_rate']:.0%} of replays")
        else:
            ok.append(s)
    if not ok:
        return None, why_not
    best = max(ok, key=lambda s: s["stats"]["lower_pct"])
    st = best["stats"]
    conf = "high" if st["lower_pct"] >= 1 and st["n"] >= 8 else "medium" if st["lower_pct"] >= 0.5 else "low"
    return {"shape": best["key"], "label": best["label"], "expected_pct": st["mean_pct"],
            "lower_pct": st["lower_pct"], "win_rate": st["win_rate"], "confidence": conf}, why_not


def assess(c: Candidate, rt: tuple[float, float] | None, quoted: bool) -> dict[str, Any]:
    """The cheap part: pool and token facts, regime and vetoes (no replay)."""
    reg, falling = regime(c)
    vetoes = token_vetoes(c) + falling
    if quoted and rt is None:
        vetoes.append("no Jupiter quote (can't price the swap)")
    elif rt is not None:
        if rt[0] > MAX_ROUND_TRIP:
            vetoes.append(f"round trip costs {rt[0]:.1%} (> {MAX_ROUND_TRIP:.0%})")
        if not PRICE_GAP[0] <= rt[1] <= PRICE_GAP[1]:
            vetoes.append(f"pool price {rt[1]:+.1%} off the market")
    st = chop_stats(c.bars)
    ch = price_change(c.bars, c.price)
    fl = c.flow
    out: dict[str, Any] = {
        "pool": c.pool, "name": c.name, "mint": c.mint, "at": time.time(), "price": c.price, "tvl": c.tvl,
        "fees_1h": c.fees_1h, "fee_tvl_1h": c.fee_tvl_1h, "bin_step": c.bin_step, "age_h": c.token_age_h,
        "mcap": c.mcap, "holders": c.holders, "change_1h_pct": ch * 100 if ch is not None else None,
        "vol_5m_pct": (st["vol"] or 0) * 100, "regime": reg, "regime_text": REGIME_TEXT[reg],
        "round_trip_pct": rt[0] * 100 if rt else None, "price_gap_pct": rt[1] * 100 if rt else None,
        "buy_5m": fl.buy5m if fl else None, "sell_5m": fl.sell5m if fl else None,
        "chg_5m_pct": fl.chg5m_pct if fl else None, "vetoes": vetoes, "shapes": [], "choice": None, "why_not": [],
    }
    return out


def analyze(c: Candidate, rows: list[dict[str, Any]], others: Any, sol_usd: float, capital_sol: float,
            rt: tuple[float, float]) -> dict[str, Any]:
    """assess + replay every shape on the pool's real minute data + the choice."""
    out = assess(c, rt, True)
    if out["vetoes"]:
        return out
    fit = set(FIT[out["regime"]])
    for sh in SHAPES:
        stats = summarize(replay(rows, sh, c.step, capital_sol, sol_usd, others, rt[0]))
        out["shapes"].append({"key": sh.key, "label": sh.label, "fits": sh.key in fit, "stats": stats})
    out["choice"], out["why_not"] = choose(out["shapes"])
    return out


class Auto(Strategy):
    """Opens what the analyzer chose for the pool (fresh analysis only); exits with that shape's rules."""
    name = "auto"
    label = "Auto: the analyzer picks the pool and the shape"
    careful = True
    rules = ExitRules(**V2_EXITS)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        a = LATEST.get(c.pool)
        if not a or not a.get("choice") or time.time() - a["at"] > 15 * 60:
            return None
        ch = a["choice"]
        plan = BY_KEY[ch["shape"]].build(c.price, c.step, capital_sol)
        plan.info.update(shape=ch["shape"], expected_pct=round(ch["expected_pct"], 3),
                         lower_pct=round(ch["lower_pct"], 3), confidence=ch["confidence"], regime=a["regime"])
        return plan

    def rank(self, c: Candidate) -> float:
        a = LATEST.get(c.pool)
        return a["choice"]["lower_pct"] if a and a.get("choice") else -1e9


def rules_for(strategy: Strategy, info: dict[str, Any]) -> ExitRules:
    if strategy.name == Auto.name and info.get("shape") in BY_KEY:
        return BY_KEY[info["shape"]].rules
    return strategy.rules
