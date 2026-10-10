"""Are crashes detectable in time, and does it depend on token age? Per-minute prices (pool_stats).

Crash = the price falls >= 20% from a rolling 60-min high. For each crash, simulate our in-position price guards:
fast drop (-6% within 3 min) and crash exit (-10% from the 15-min high), and measure where we would have exited
(vs the high before the crash) compared with the low. Split by token age (< 24 h vs >= 24 h).

Run inside the bot container: python crash_study.py
"""

import asyncio
import os
import statistics
from collections import defaultdict

import asyncpg

SOL = "So11111111111111111111111111111111111111112"


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    rows = await conn.fetch(
        "SELECT s.pool, p.name, extract(epoch FROM s.ts)::float t, s.price, s.tvl, "
        "extract(epoch FROM s.ts - t.created_at) / 3600 AS age_h "
        "FROM pool_stats s JOIN pools p ON p.address = s.pool JOIN tokens t ON t.mint = p.token_x "
        "WHERE p.token_y = $1 AND s.price > 0 AND s.tvl >= 10000 ORDER BY s.pool, s.ts", SOL)
    await conn.close()
    series: dict[str, list] = defaultdict(list)
    for r in rows:
        series[r["pool"]].append(r)

    hours = defaultdict(float)           # pool-hours observed, by age group
    events = defaultdict(list)
    for pool, s in series.items():
        i = 0
        n = len(s)
        while i < n:
            r = s[i]
            grp = "young <24h" if (r["age_h"] or 0) < 24 else "older >=24h"
            hours[grp] += 1 / 60
            win = [x for x in s[max(0, i - 60):i + 1] if r["t"] - x["t"] <= 3600]
            hi = max(x["price"] for x in win)
            if r["price"] <= hi * 0.80:                    # crash in progress: >= 20% below the 60-min high
                # where would the guards have exited? walk from the high forward
                j0 = max(k for k in range(max(0, i - 60), i + 1) if s[k]["price"] == hi)
                exit_px, how = None, None
                for k in range(j0 + 1, i + 1):
                    p = s[k]["price"]
                    last3 = [x["price"] for x in s[max(j0, k - 3):k + 1]]
                    last15 = [x["price"] for x in s[max(j0, k - 15):k + 1]]
                    if p <= max(last3) * 0.94:
                        exit_px, how = p, "fast_drop"
                        break
                    if p <= max(last15) * 0.90:
                        exit_px, how = p, "crash_-10%"
                        break
                low = min(x["price"] for x in s[i:min(n, i + 60)])
                gap1 = s[j0 + 1]["price"] / hi - 1 if j0 + 1 < n else 0          # first minute after the high
                events[grp].append({"pool": r["name"], "exit": (exit_px / hi - 1) * 100 if exit_px else None,
                                    "low": (low / hi - 1) * 100, "how": how, "first_min": gap1 * 100,
                                    "minutes": (s[i]["t"] - s[j0]["t"]) / 60})
                # skip ahead past this crash
                while i < n and s[i]["price"] <= hi * 0.95:
                    i += 1
                continue
            i += 1

    for grp in ("young <24h", "older >=24h"):
        ev = events[grp]
        print(f"\n### {grp}: {hours[grp]:.0f} pool-hours observed, {len(ev)} crashes "
              f"({len(ev) / max(hours[grp], 1) * 24:.2f} per pool-day)")
        if not ev:
            continue
        ex = [e["exit"] for e in ev if e["exit"] is not None]
        print(f"  guard exit vs pre-crash high: mean {statistics.mean(ex):.1f}%  median {statistics.median(ex):.1f}%  "
              f"worst {min(ex):.1f}%")
        print(f"  bottom after the crash:       mean {statistics.mean(e['low'] for e in ev):.1f}%  "
              f"worst {min(e['low'] for e in ev):.1f}%")
        gapped = sum(1 for e in ev if e["first_min"] <= -10)
        print(f"  crashes with a >= 10% drop in the first minute (no time to react): {gapped}/{len(ev)}")
        print(f"  minutes from the high to -20%: median {statistics.median(e['minutes'] for e in ev):.0f}")
        by = defaultdict(int)
        for e in ev:
            by[e["how"]] += 1
        print(f"  first guard to fire: {dict(by)}")
        for e in sorted(ev, key=lambda e: e["low"])[:6]:
            print(f"    {e['pool']:<16} exit {e['exit'] if e['exit'] is None else round(e['exit'], 1)}% "
                  f"low {e['low']:.1f}% first-min {e['first_min']:.1f}% over {e['minutes']:.0f} min ({e['how']})")


if __name__ == "__main__":
    asyncio.run(main())
