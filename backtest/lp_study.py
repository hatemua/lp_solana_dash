"""Which pools / tokens / shapes were profitable to LP? A study on our per-minute data (pool_stats).

Every hour, for every SOL pool with TVL >= $10k and fees >= $100 in the last hour, open three virtual $100
positions and hold them `HOLD_MIN` minutes:
  spot10   two-sided spot +/-10 bins (half swapped to the token)
  spot25   two-sided spot +/-25 bins
  bid69    SOL-only bid-ask in the 69 bins below the price (Meridian / S2 shape)
Fees: the pool's real per-minute fees (cum_fees delta) spread over the bins the price touched; our share against the
median liquidity profile (USD per bin / TVL, by distance from the active bin, from our bin snapshots) x the pool's
TVL at that minute. Costs: 0.5% to buy the token half, 0.5% to sell tokens held at the end (measured Jupiter quotes
were 0-0.8% on liquid tokens), 0.0002 SOL per tx. No exits: this measures the raw edge of each entry.

Then: win rate and mean P&L per shape, and the same split by features known at entry (fee/TVL, token age, mcap,
volatility, 1 h price change, trend, TVL, volume burst) to see what predicts profit.

Run inside the bot container: python lp_study.py   (env HOLD_MIN, STEP_MIN)
"""

import asyncio
import json
import math
import os
import statistics
from bisect import bisect_left
from collections import defaultdict

import asyncpg

from lp_bot.sim import Leg, bid_bins_below, fee_share, spot_two_sided

SOL = "So11111111111111111111111111111111111111112"
HOLD_MIN = int(os.environ.get("HOLD_MIN", "60"))
STEP_MIN = int(os.environ.get("STEP_MIN", "60"))
SWAP = 0.005
TX = 0.0002


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    sol_usd = float(await conn.fetchval("SELECT price_usd FROM tokens WHERE mint = $1", SOL))
    meta = {r["address"]: dict(r) for r in await conn.fetch(
        "SELECT p.address, p.name, p.bin_step, t.mcap, t.holders, t.audit, "
        "extract(epoch FROM t.created_at)::float AS token_created "
        "FROM pools p JOIN tokens t ON t.mint = p.token_x WHERE p.token_y = $1", SOL)}
    rows = await conn.fetch("SELECT pool, extract(epoch FROM ts)::float t, price, tvl, cum_fees, fees_1h, volume_1h, "
                            "volume_24h FROM pool_stats WHERE pool = ANY($1) AND price > 0 ORDER BY pool, ts",
                            list(meta))
    snaps = await conn.fetch("SELECT pool, extract(epoch FROM ts)::float t, active_bin_id, bins FROM bins_snapshot "
                             "WHERE ts > now() - interval '40 hours'")
    await conn.close()

    series: dict[str, list] = defaultdict(list)
    for r in rows:
        series[r["pool"]].append(r)
    # median liquidity profile by distance from the active bin, as a share of TVL
    tv_at = {p: ([x["t"] for x in s], s) for p, s in series.items()}
    prof: dict[int, list[float]] = defaultdict(list)
    for sn in snaps[::5]:
        if sn["pool"] not in tv_at:
            continue
        ts, s = tv_at[sn["pool"]]
        i = min(bisect_left(ts, sn["t"]), len(s) - 1)
        tvl = s[i]["tvl"] or 0
        if tvl <= 0:
            continue
        for b in sn["bins"]:
            d = b["bin_id"] - sn["active_bin_id"]
            prof[d].append((b["x"] * b["price"] + b["y"]) * sol_usd / tvl)
    profile = {d: statistics.median(v) for d, v in prof.items() if len(v) > 20}

    results = []
    for pool, s in series.items():
        m = meta[pool]
        step = (m["bin_step"] or 100) / 10_000
        audit = m["audit"] if isinstance(m["audit"], dict) else {}
        n = len(s)
        i = 60
        while i + HOLD_MIN < n:
            e = s[i]
            # contiguous minutes only
            if s[i + HOLD_MIN]["t"] - e["t"] > (HOLD_MIN + 5) * 60 or (e["tvl"] or 0) < 10_000 or (e["fees_1h"] or 0) < 100:
                i += STEP_MIN
                continue
            prev = [x["price"] for x in s[i - 60:i + 1]]
            rets = [math.log(b / a) for a, b in zip(prev, prev[1:], strict=False) if a > 0 and b > 0]
            vol = statistics.pstdev(rets) if len(rets) > 5 else 0.0
            chg1h = e["price"] / prev[0] - 1
            trend = math.log(e["price"] / prev[0]) / (vol * math.sqrt(len(rets))) if vol > 0 else 0.0
            feat = {"fee_tvl_1h": (e["fees_1h"] or 0) / e["tvl"] * 100, "tvl": e["tvl"],
                    "age_h": (e["t"] - m["token_created"]) / 3600 if m["token_created"] else None,
                    "mcap": m["mcap"] or 0, "vol_1m": vol, "chg1h": chg1h, "trend": trend,
                    "burst": (e["volume_1h"] or 0) / max(1.0, (e["volume_24h"] or 0) / 24),
                    "safe": bool(audit.get("mintAuthorityDisabled") and audit.get("freezeAuthorityDisabled"))}
            cap = 100 / sol_usd
            p0 = e["price"]
            shapes = {
                "spot10": Leg("S", spot_two_sided(p0, 10, step, cap / 2, cap / 2 / p0)),
                "spot25": Leg("S", spot_two_sided(p0, 25, step, cap / 2, cap / 2 / p0)),
                "bid69": Leg("A", bid_bins_below(p0, 69, step, cap)),
            }
            fees = dict.fromkeys(shapes, 0.0)
            for k in range(i + 1, i + HOLD_MIN + 1):
                a, b = s[k - 1], s[k]
                f = max(0.0, (b["cum_fees"] or 0) - (a["cum_fees"] or 0)) * 0.95 if a["cum_fees"] else 0.0
                if f <= 0:
                    continue
                tvl = b["tvl"] or e["tvl"]
                pa = b["price"]

                def others(price: float, pa: float = pa, tvl: float = tvl) -> float:
                    d = round(math.log(price / pa) / math.log(1 + step))
                    return profile.get(d, 0.0) * tvl

                for name, leg in shapes.items():
                    fees[name] += fee_share([leg], a["price"], pa, step, f, sol_usd, others) / sol_usd
            p1 = s[i + HOLD_MIN]["price"]
            for name, leg in shapes.items():
                buy = cap / 2 * SWAP if name.startswith("spot") else 0.0
                sell = leg.tokens(p1) * p1 * SWAP
                net = (leg.value(p1) + fees[name] - buy - sell - 2 * TX) / cap - 1
                results.append({"pool": m["name"], "shape": name, "net": net * 100,
                                "fees": fees[name] / cap * 100, "move": (p1 / p0 - 1) * 100, **feat})
            i += STEP_MIN

    print(f"hold {HOLD_MIN} min, entry every {STEP_MIN} min, {len(results) // 3} entries, "
          f"{len({r['pool'] for r in results})} pools, profile bins {len(profile)}")
    for shape in ("spot10", "spot25", "bid69"):
        rs = [r for r in results if r["shape"] == shape]
        nets = [r["net"] for r in rs]
        print(f"\n### {shape}: n={len(rs)} win {sum(x > 0 for x in nets) / len(nets):.0%} mean {statistics.mean(nets):+.2f}% "
              f"median {statistics.median(nets):+.2f}% fees mean {statistics.mean(r['fees'] for r in rs):.2f}% "
              f"p10 {sorted(nets)[len(nets) // 10]:+.1f}% p90 {sorted(nets)[len(nets) * 9 // 10]:+.1f}%")
        for feat, cuts in [("fee_tvl_1h", [0.3, 1, 3, 10]), ("age_h", [6, 24, 72, 24 * 7]),
                           ("mcap", [3e5, 1e6, 3e6, 1e7]), ("vol_1m", [0.003, 0.006, 0.01, 0.02]),
                           ("chg1h", [-0.15, -0.05, 0.05, 0.15]), ("trend", [-2, -0.5, 0.5, 2]),
                           ("tvl", [2e4, 5e4, 1.5e5, 5e5]), ("burst", [0.5, 1, 2, 4])]:
            buckets: dict[str, list[float]] = defaultdict(list)
            for r in rs:
                v = r[feat]
                if v is None:
                    continue
                j = sum(v >= c for c in cuts)
                lo = "-inf" if j == 0 else f"{cuts[j - 1]:g}"
                hi = "inf" if j == len(cuts) else f"{cuts[j]:g}"
                buckets[f"{lo}..{hi}"].append(r["net"])
            order = sorted(buckets, key=lambda k: float(k.split("..")[0].replace("-inf", "-1e18")))
            print(f"  {feat:<11} " + " | ".join(
                f"{k}: n{len(buckets[k])} {statistics.mean(buckets[k]):+.2f}% w{sum(x > 0 for x in buckets[k]) / len(buckets[k]):.0%}"
                for k in order))
    # best simple combinations for spot10
    rs = [r for r in results if r["shape"] == "spot10"]
    combos = {
        "age>=24h": lambda r: (r["age_h"] or 0) >= 24,
        "age>=24h & fee/TVL>=1%": lambda r: (r["age_h"] or 0) >= 24 and r["fee_tvl_1h"] >= 1,
        "age>=24h & |trend|<0.5": lambda r: (r["age_h"] or 0) >= 24 and abs(r["trend"]) < 0.5,
        "age>=24h & fee/TVL>=1% & |trend|<0.5": lambda r: (r["age_h"] or 0) >= 24 and r["fee_tvl_1h"] >= 1
        and abs(r["trend"]) < 0.5,
        "age>=72h & fee/TVL>=1% & vol<1%": lambda r: (r["age_h"] or 0) >= 72 and r["fee_tvl_1h"] >= 1
        and r["vol_1m"] < 0.01,
        "fee/TVL>=3%": lambda r: r["fee_tvl_1h"] >= 3,
        "fee/TVL>=3% & age<24h": lambda r: r["fee_tvl_1h"] >= 3 and (r["age_h"] or 0) < 24,
    }
    print("\n### spot10 combinations")
    for name, f in combos.items():
        sel = [r["net"] for r in rs if f(r)]
        if sel:
            print(f"  {name:<40} n={len(sel):<4} win {sum(x > 0 for x in sel) / len(sel):.0%} mean {statistics.mean(sel):+.2f}% "
                  f"median {statistics.median(sel):+.2f}%")
    top = sorted(rs, key=lambda r: -r["net"])[:8]
    print("\n### best spot10 entries:", [(r["pool"], round(r["net"], 1), round(r["fee_tvl_1h"], 1), round(r["age_h"] or 0))
                                         for r in top])


if __name__ == "__main__":
    asyncio.run(main())
