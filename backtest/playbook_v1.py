"""Backtest of LP playbook v1 (docs/research/strategy-playbook.md) on our stored data.

Entry: TOPPED regime (pump >= +30% then price 15-45% below the peak), fees >= $X in the last hour, token screen.
Legs: A = SOL-only bid-ask from -5% to -55% (V1: 90% of budget; V2: 60% + deep leg B -50%..-85% 30%).
Flip: when A is >= 70% converted and the price bounces +5% from the low, A's tokens move to a token-side bid-ask
above the price up to avg buy x 1.10. Exits on net P&L: +5% TP, trailing (+3% / -1.5), fee death, out of range,
-15% stop, 6 h.

Fees: each 5-min bar's real LP fees are spread over the bins between its low and high; in each bin our share is
ours / (ours + others), others from the nearest real bin snapshot (79 pools) or, otherwise, the median liquidity
profile by bin distance x the pool's TVL at that minute. Costs: tx fees, swap (base fee + 1% slippage) when tokens
are sold, non-refundable bin-array rent only for arrays deeper than ARRAYS_FREE (default 4) below the price.

Run inside the api container: python playbook_v1.py  (reads DATABASE_URL; prints only aggregate results)
"""

import asyncio
import json
import math
import os
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

import asyncpg

SOL = "So11111111111111111111111111111111111111112"
BUDGET = 100.0
BIN_ARRAY_RENT_SOL = 0.07143744
TX_SOL = 0.0002                      # per open/close/flip action incl. priority fee
SLIPPAGE = 0.01
HOURS = float(os.environ.get("HOURS", "26"))
MIN_FEES_1H = float(os.environ.get("MIN_FEES_1H", "500"))
PROFILE_D = 70
ARRAYS_FREE = int(os.environ.get("ARRAYS_FREE", "4"))


@dataclass
class Bin:
    price: float          # SOL per token
    sol: float = 0.0      # SOL deposited (bid bins) -> USD value derived
    tok: float = 0.0      # tokens deposited (ask bins)
    side: str = "bid"


@dataclass
class Leg:
    name: str
    bins: list[Bin]
    def value(self, p: float) -> float:            # in SOL
        v = 0.0
        for b in self.bins:
            if b.side == "bid":                    # SOL until price falls to/below the bin, then tokens
                v += b.sol if p > b.price else (b.sol / b.price) * p
            else:                                  # tokens until price rises above the bin, then SOL
                v += b.tok * p if p < b.price else b.tok * b.price
        return v
    def converted(self, p: float) -> float:        # share of a bid leg now held as token
        tot = sum(b.sol for b in self.bins)
        return sum(b.sol for b in self.bins if p <= b.price) / tot if tot else 0.0
    def tokens(self, p: float) -> float:
        return sum(b.sol / b.price for b in self.bins if b.side == "bid" and p <= b.price) + \
            sum(b.tok for b in self.bins if b.side == "ask" and p < b.price)
    def sol_held(self, p: float) -> float:
        return sum(b.sol for b in self.bins if b.side == "bid" and p > b.price) + \
            sum(b.tok * b.price for b in self.bins if b.side == "ask" and p >= b.price)


def ladder(p0: float, lo: float, hi: float, step: float, amount: float, side: str) -> list[Bin]:
    """Bid-ask ladder from p0*hi to p0*lo (fractions of p0), weight growing away from the price."""
    r = 1 + step
    if side == "bid":
        k0, k1 = math.ceil(math.log(1 / hi) / math.log(r)), math.floor(math.log(1 / lo) / math.log(r))
        ks = list(range(max(1, k0), k1 + 1))
        w = [i + 1 for i in range(len(ks))]
        tot = sum(w)
        return [Bin(price=p0 / r ** k, sol=amount * wi / tot, side="bid") for k, wi in zip(ks, w, strict=True)]
    k0, k1 = math.ceil(math.log(lo) / math.log(r)), math.floor(math.log(hi) / math.log(r))
    ks = list(range(max(1, k0), k1 + 1))
    w = [i + 1 for i in range(len(ks))]
    tot = sum(w)
    return [Bin(price=p0 * r ** k, tok=amount * wi / tot, side="ask") for k, wi in zip(ks, w, strict=True)]


@dataclass
class Pos:
    pool: str
    t0: int
    p0: float
    sol_usd: float
    legs: list[Leg]
    capital_sol: float
    idle_sol: float = 0.0
    fees_sol: float = 0.0
    costs_sol: float = 0.0
    peak_net: float = -1e9
    trail_on: bool = False
    low_since_fill: float | None = None
    flipped: bool = False
    out_below: int = 0
    out_above: int = 0
    weak_fee_bars: int = 0
    events: list[str] = field(default_factory=list)

    def value(self, p: float) -> float:
        return self.idle_sol + sum(lg.value(p) for lg in self.legs)
    def net_pct(self, p: float, sell_cost: float) -> float:
        tok_val = sum(lg.tokens(p) for lg in self.legs) * p
        v = self.value(p) - tok_val * sell_cost + self.fees_sol - self.costs_sol
        return (v / self.capital_sol - 1) * 100


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    sol_usd = await conn.fetchval("SELECT price_usd FROM tokens WHERE mint = $1", SOL)
    pools = {r["address"]: dict(r) for r in await conn.fetch(
        "SELECT p.address, p.name, p.bin_step, p.base_fee_pct, p.token_x, t.mcap, t.holders, t.price_usd, t.audit "
        "FROM pools p JOIN tokens t ON t.mint = p.token_x WHERE p.token_y = $1", SOL)}
    bars = defaultdict(list)
    for r in await conn.fetch(
            "SELECT pool, extract(epoch FROM ts)::bigint t, high, low, close, fees FROM pool_ohlcv_5m "
            "WHERE ts > now() - make_interval(hours => $1) - interval '3 hours' AND ts <= now() - interval '10 minutes' "
            "ORDER BY pool, ts", HOURS):
        if r["pool"] in pools and r["close"]:
            bars[r["pool"]].append((r["t"], r["high"] or r["close"], r["low"] or r["close"], r["close"], r["fees"] or 0))
    tvl = defaultdict(list)
    for r in await conn.fetch("SELECT pool, extract(epoch FROM ts)::bigint t, tvl FROM pool_stats "
                              "WHERE ts > now() - make_interval(hours => $1) - interval '3 hours' AND tvl > 0 "
                              "ORDER BY pool, ts", HOURS):
        tvl[r["pool"]].append((r["t"], r["tvl"]))
    snaps = defaultdict(list)
    for r in await conn.fetch("SELECT pool, extract(epoch FROM ts)::bigint t, active_bin_id, bins FROM bins_snapshot "
                              "WHERE ts > now() - make_interval(hours => $1) - interval '3 hours' ORDER BY pool, ts",
                              HOURS):
        bl = sorted(((b["price"], (b["x"] * b["price"] + b["y"]) * sol_usd) for b in r["bins"]), key=lambda x: x[0])
        snaps[r["pool"]].append((r["t"], r["active_bin_id"], r["bins"], bl))
    await conn.close()

    def at(series: list, t: int, max_gap: int):
        i = bisect_left(series, (t,))
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(series) and abs(series[j][0] - t) <= max_gap:
                if best is None or abs(series[j][0] - t) < abs(best[0] - t):
                    best = series[j]
        return best

    # median liquidity profile (USD per bin / TVL) by signed distance from the active bin
    prof_samples: dict[int, list[float]] = defaultdict(list)
    for pool, ss in snaps.items():
        for t, active, raw, _ in ss[::10]:
            tv = at(tvl[pool], t, 900)
            if not tv:
                continue
            for b in raw:
                d = b["bin_id"] - active
                if abs(d) <= PROFILE_D:
                    prof_samples[d].append((b["x"] * b["price"] + b["y"]) * sol_usd / tv[1])
    profile = {d: statistics.median(v) for d, v in prof_samples.items() if v}

    def others_usd(pool: str, t: int, price: float, p_active: float, step: float) -> float:
        s = at(snaps[pool], t, 900)
        if s:
            bl = s[3]
            i = bisect_left(bl, (price,))
            cands = [bl[j] for j in (i - 1, i) if 0 <= j < len(bl)]
            if cands:
                nb = min(cands, key=lambda c: abs(c[0] - price))
                if abs(nb[0] / price - 1) < step:
                    return nb[1]
            return 0.0                                          # outside the snapshot: nobody there
        tv = at(tvl[pool], t, 900)
        if not tv:
            return float("nan")
        d = round(math.log(price / p_active) / math.log(1 + step))
        return profile.get(d, 0.0) * tv[1]

    results: dict[str, list[dict]] = {"V1": [], "V2": []}
    t_start = max(t for b in bars.values() for t, *_ in b) - int(HOURS * 3600)
    for variant in results:
        for pool, bs in bars.items():
            meta = pools[pool]
            step = (meta["bin_step"] or 100) / 10_000
            sell_cost = (meta["base_fee_pct"] or 1) / 100 + SLIPPAGE
            audit = meta["audit"] if isinstance(meta["audit"], dict) else {}
            if not (audit.get("mintAuthorityDisabled") and audit.get("freezeAuthorityDisabled")):
                continue
            if (meta["holders"] or 0) < 500 or (audit.get("topHoldersPercentage") or 0) > 30:
                continue
            closes = [b[3] for b in bs]
            pos: Pos | None = None
            cooldown_until = 0
            for i in range(60, len(bs)):
                t, hi, lo, close, fee_usd = bs[i]
                if pos is None:
                    if t < t_start or t < cooldown_until:
                        continue
                    peak_i = max(range(i - 24, i + 1), key=lambda j: bs[j][1])
                    peak = bs[peak_i][1]
                    base = min(b[2] for b in bs[max(0, peak_i - 36):peak_i + 1])
                    mcap_t = (meta["mcap"] or 0) * close / closes[-1]
                    fees_1h = sum(b[4] for b in bs[i - 11:i + 1])
                    if not (peak / base >= 1.30 and 0.55 * peak <= close <= 0.85 * peak and fees_1h >= MIN_FEES_1H
                            and mcap_t >= 250_000):
                        continue
                    if not at(tvl[pool], t, 900):
                        continue
                    cap = BUDGET / sol_usd
                    legs = [Leg("A", ladder(close, 0.45, 0.95, step, cap * (0.9 if variant == "V1" else 0.6), "bid"))]
                    if variant == "V2":
                        legs.append(Leg("B", ladder(close, 0.15, 0.50, step, cap * 0.3, "bid")))
                    pos = Pos(pool, t, close, sol_usd, legs, cap, idle_sol=cap * 0.1)
                    # bin arrays: on-chain check (2026-10-10) found arrays initialized down to 4-5 arrays (~-94%)
                    # below the active bin in active pools, so only arrays deeper than ARRAYS_FREE pay rent
                    far = [b for lg in legs for b in lg.bins
                           if math.log(close / b.price) / math.log(1 + step) > 70 * ARRAYS_FREE]
                    arrays = len({math.floor(math.log(b.price) / math.log(1 + step) / 70) for b in far})
                    positions = sum(math.ceil(len(lg.bins) / 69) for lg in legs)
                    pos.costs_sol += arrays * BIN_ARRAY_RENT_SOL + positions * TX_SOL
                    pos.events.append(f"open arrays={arrays} positions={positions}")
                    continue
                # ---- fees earned in this bar
                lo_p, hi_p = lo, hi
                ours = []
                for lg in pos.legs:
                    for b in lg.bins:
                        if lo_p <= b.price <= hi_p:
                            v = (b.sol if b.side == "bid" else b.tok * b.price) * sol_usd
                            ours.append((b.price, v))
                n_cross = max(1, round(math.log(hi_p / lo_p) / math.log(1 + step)) + 1) if hi_p > lo_p else 1
                earned = 0.0
                for price, v in ours:
                    oth = others_usd(pool, t, price, close, step)
                    if math.isnan(oth):
                        oth = v * 4
                    earned += fee_usd / n_cross * v / (v + oth) if v > 0 else 0
                pos.fees_sol += earned / sol_usd
                deployed = sum(lg.value(close) for lg in pos.legs) * sol_usd
                # ---- flip
                a = next((lg for lg in pos.legs if lg.name == "A"), None)
                if a and not pos.flipped:
                    if a.converted(close) >= 0.7:
                        pos.low_since_fill = min(pos.low_since_fill or lo, lo)
                        if close >= pos.low_since_fill * 1.05:
                            toks = a.tokens(close)
                            spent = sum(b.sol for b in a.bins if close <= b.price)
                            avg = spent / toks if toks else close
                            pos.idle_sol += a.sol_held(close)
                            pos.legs.remove(a)
                            if avg * 1.10 > close * (1 + step):
                                pos.legs.append(Leg("F", ladder(close, 1 + step, avg * 1.10 / close, step, toks, "ask")))
                            else:
                                pos.idle_sol += toks * close * (1 - sell_cost)
                            pos.flipped = True
                            pos.costs_sol += 2 * TX_SOL
                            pos.events.append("flip")
                net = pos.net_pct(close, sell_cost)
                pos.peak_net = max(pos.peak_net, net)
                held_h = (t - pos.t0) / 3600
                fee_rate = earned * 12 / deployed * 100 if deployed > 0 else 0
                pos.weak_fee_bars = pos.weak_fee_bars + 1 if fee_rate < 0.5 else 0
                all_bins = [b for lg in pos.legs for b in lg.bins]
                lowest = min((b.price for b in all_bins), default=close)
                highest = max((b.price for b in all_bins), default=close)
                pos.out_below = pos.out_below + 1 if close < lowest else 0
                pos.out_above = pos.out_above + 1 if close > highest and not any(lg.tokens(close) for lg in pos.legs) else 0
                f = next((lg for lg in pos.legs if lg.name == "F"), None)
                reason = None
                if net >= 5:
                    reason = "take_profit"
                elif net >= 3:
                    pos.trail_on = True
                if not reason and pos.trail_on and net <= pos.peak_net - 1.5:
                    reason = "trailing"
                if not reason and net <= -15:
                    reason = "stop"
                if not reason and pos.out_below >= 6:
                    reason = "out_below"
                if not reason and pos.out_above >= 6:
                    reason = "out_above"
                if not reason and f and f.sol_held(close) >= 0.9 * (f.sol_held(close) + f.tokens(close) * close):
                    reason = "flip_done"
                if not reason and pos.weak_fee_bars >= 6 and net >= 0:
                    reason = "fee_death"
                if not reason and held_h >= 6:
                    reason = "time"
                if reason:
                    pos.costs_sol += TX_SOL * len(pos.legs)
                    final = pos.net_pct(close, sell_cost)
                    results[variant].append({"pool": meta["name"], "t0": pos.t0, "hours": held_h, "net_pct": final,
                                             "fees_usd": pos.fees_sol * sol_usd, "costs_usd": pos.costs_sol * sol_usd,
                                             "reason": reason, "flipped": pos.flipped,
                                             "max_conv": a.converted(lo) if a else 1.0})
                    pos = None
                    cooldown_until = t + 3600

    print(f"window {HOURS} h, SOL ${sol_usd:.2f}, pools {len(bars)}, min fees 1h ${MIN_FEES_1H:.0f}, "
          f"profile bins {len(profile)}")
    for variant, rs in results.items():
        if not rs:
            print(variant, "no trades")
            continue
        nets = [r["net_pct"] for r in rs]
        wins = [n for n in nets if n > 0]
        print(f"\n{variant}: trades {len(rs)}  win rate {len(wins) / len(rs):.0%}  avg {statistics.mean(nets):+.2f}%  "
              f"median {statistics.median(nets):+.2f}%  avg win {statistics.mean(wins) if wins else 0:+.2f}%  "
              f"avg loss {statistics.mean([n for n in nets if n <= 0] or [0]):+.2f}%  "
              f"total ${sum(nets):+.2f} on $100 each  fees ${sum(r['fees_usd'] for r in rs):.2f}  "
              f"costs ${sum(r['costs_usd'] for r in rs):.2f}  avg hold {statistics.mean(r['hours'] for r in rs):.1f} h")
        by = defaultdict(list)
        for r in rs:
            by[r["reason"]].append(r["net_pct"])
        print("  by exit:", ", ".join(f"{k} {len(v)} ({statistics.mean(v):+.1f}%)" for k, v in sorted(by.items())))
        for slots in (1, 3):                       # a real wallet: at most `slots` positions of $100 at once
            free = [0.0] * slots
            taken = []
            for r in sorted(rs, key=lambda r: r["t0"]):
                k = min(range(slots), key=lambda j: free[j])
                if free[k] <= r["t0"]:
                    free[k] = r["t0"] + r["hours"] * 3600
                    taken.append(r["net_pct"])
            print(f"  {slots} slot(s) of $100: {len(taken)} trades, ${sum(taken):+.2f} in {HOURS:.0f} h "
                  f"(${sum(taken) * 24 / HOURS:+.2f}/day)")
        fl = [r["net_pct"] for r in rs if r["flipped"]]
        print(f"  flipped {len(fl)}" + (f" avg {statistics.mean(fl):+.1f}%" if fl else ""))
        for r in sorted(rs, key=lambda r: r["t0"]):
            print(f"   {r['pool']:<18} {r['hours']:4.1f}h {r['net_pct']:+6.1f}% fees ${r['fees_usd']:5.2f} "
                  f"costs ${r['costs_usd']:5.2f} {r['reason']:<11} conv {r['max_conv']:.0%}{' flip' if r['flipped'] else ''}")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
