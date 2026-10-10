# LP playbook v1: regimes, legs, flips and exits

Sources: LP Army (academy, Meteora bootcamp recaps), Evil Panda, Meridian, Scout bot, Bankless, Meteora docs; see
[`strategies-web.md`](strategies-web.md). Every rule here is **unvalidated** until the M3 paper bot and the M4
backtest measure it; numbers are the community's starting points, to be tuned on our data.

The core idea: no single shape works in every market ("one strategy is a losing strategy"). The bot reads the
**regime** of a token, runs one or more **legs** (positions) suited to it, **flips** inventory when a leg fills, and
exits on **portfolio P&L = fees + mark-to-market − costs**, not on range alone.

## 1. Screen (before any leg)

| Check | Rule | Data we have |
|---|---|---|
| Market cap | ≥ $250k (bootcamp), ≥ $500k–700k (fee strategies) | Jupiter ✔ |
| LP fees | pool fees ≥ $1,000–1,200 in the last hour | Meteora ✔ |
| Fee / active liquidity | fees 1 h ÷ liquidity within ±20 bins ≥ 2% | bins ✔ (watched pools) |
| Total fees paid by traders | < 20 SOL scam, 20–50 grey, > 50 SOL OK | Jupiter `fees` ✔ (to add) |
| Holders / organic | ≥ 500 holders, organic score ≥ 70 | Jupiter ✔ |
| Concentration | top-10 ≤ 30%, dev ≤ 5%, mint + freeze off | Jupiter audit ✔ |
| Bundling / insiders / clusters | bundled ≤ 60%, insiders 0%, no Bubblemaps cluster | ✘ (GMGN / Bubblemaps not public: manual flag) |
| Volume shape | no sudden volume stops, two-way flow, no flat bot-like bars | 1-min token OHLCV ✔ |
| Pool | bin step 80–125 (memecoins), base fee 2–10% for new/volatile, best sibling pool per token | Meteora ✔ |

## 2. Regimes (per token, re-evaluated every minute)

| Regime | Detection (first version) | Meaning |
|---|---|---|
| **PUMP** | token volume ≥ $300k / 5 min (Rabbit), 1 h change ≥ +30%, price within 5% of 1 h high | momentum, buyers dominate |
| **TOPPED** | after a PUMP: price ≥ 15% below the 1 h high, or 2 of 3 top signals on 15 m (close > upper Bollinger(20,2), RSI(2) > 90, MACD histogram turning down) | the dump phase starts: best time for SOL bids below |
| **CHOP** | chop index ≥ 5, \|trend\| < 1, volatility 5 m 1–5%, fee velocity ≥ 1 | back-and-forth: two-sided earns on every pass |
| **DUMP** | trend ≤ −2 or price −30% in 1 h with volume rising | falling knife: only far-below bids, no token inventory |
| **FADE** | fees 15 min < 0.5 × previous hour and volume burst < 0.5 | party over: exit or do not enter |
| **DEAD / RUG** | > 50% of TVL pulled in 15 min, safety flag, volume ≈ 0 | close everything now |

## 3. Legs (positions) by regime

Capital per token = 1 unit (e.g. $100). Max 3 tokens at once. Max 3 legs per token.

| Regime | Leg | Shape / side | Range | Size |
|---|---|---|---|---|
| PUMP | **Rabbit ask** (only if we hold the token) or wait | token-side bid-ask above price | 0 to +30% | 30% |
| TOPPED | **A: main bid** | SOL-only bid-ask | from 5–10% below the nearest support (VPVR node / prior rejection) down to −55%…−60% | 60% |
| TOPPED | **B: deep layer** (layer cake) | SOL-only bid-ask, overlapping A's bottom | −50% to −85…−90% (Evil Panda zone) | 30% |
| CHOP | **C: chop leg** (anti-sawtooth) | spot or curve, two-sided, tight | ±(2·σ·√(hold/5 min)), 15–34 bins | 20–40% |
| CHOP (calm, established coin) | **wide spot & chill** | spot, SOL-only | −57% to −95% | 60% |
| DUMP | only leg B (far below), no new A | | | |
| FADE / DEAD | no new legs | | | |

Reserve ≥ 10% of the unit (and ≥ 0.2 SOL) for rent and fees. Position rent 0.057 SOL is refunded on close;
new bin arrays (0.07 SOL each, not refunded) are avoided by preferring ranges inside existing arrays.

## 4. The flip (SOL → token → SOL)

When a SOL bid leg is **≥ 70% converted** to the token (price fell through it):

1. If the regime is DUMP or DEAD → do not flip, apply the stop (section 5).
2. Else wait for a **bounce signal**: 2 of 3 on 15 m (RSI(2) crossing up from < 10, MACD histogram first green bar,
   close back above the lower Bollinger band) **or** price +5% from the post-fill low.
3. Withdraw the leg (claim fees), re-deposit the tokens **token-side bid-ask above price**: from +1 bin up to the
   leg's average buy price + 10% (so a full bounce sells everything back to SOL at a profit).
4. The flipped leg exits when ≥ 90% is back in SOL, or the price falls below the post-fill low (then sell the rest
   via swap: stop).

Bounce without a flip: if the price comes back above leg A before it filled, A is all SOL again: keep it while fees
pay (≥ 0.5%/h of the leg), else close with fees as profit.

## 5. Exits (portfolio per token, checked every minute)

Net = fees claimed + unclaimed + value of all legs at the current price − capital − costs.

| Rule | Trigger | Action |
|---|---|---|
| Take profit | net ≥ +5% | close all legs |
| Trailing | net ≥ +3%, then falls 1.5 points from its peak | close all |
| Session target | net ≥ +1% and regime turns FADE | close all |
| Fee death | our fees < 0.5% of deployed capital per hour for 30 min | close legs with net ≥ 0; keep filled legs for the flip |
| Out of range above | all legs SOL and price above every range for 30 min | close (profit = fees) |
| Out of range below | below the deepest leg for 30 min, or net ≤ −15% | close and sell the token |
| Regime DEAD / RUG | immediately | close and sell |
| Time | 6 h (fast plays), 24 h (wide spot & chill) | close |

Compounding (toothpaste): on wide spot legs, claim fees every 2 h when ≥ 1% of the leg and add them to a small SOL
bid-ask below price.

## 6. What the paper bot records (M3)

Per leg: entry/exit time, regime at entry, range, shape, amounts, fees per 5 min (from the real fee share of
bins crossed), mark-to-market, flip events, exit reason. Per token: net P&L, max drawdown, hold time. Report: P&L by
regime, by leg type, by exit rule; at least 30 closed tokens before any conclusion.

## 7. Open questions for M4

- Bounce signal and TOPPED detection: tune on 1-min token candles (we store them for the hot tokens).
- Support levels: VPVR from 5 m pool OHLCV vs fixed −55%.
- Fee share model: our liquidity / (our + others' in each crossed bin) using bin snapshots every minute.
- Does the deep layer B ever pay for its rent in 24 h? (Our earlier test: Evil Panda −85% almost never filled in 6 h.)

## 8. First backtest (backtest/playbook_v1.py, 2026-10-10, last 26 h, 346 SOL pools)

TOPPED entries (pump ≥ +30%, then 15–45% below the peak), leg A = SOL bid-ask −5%..−55% (V1, 90%) or A 60% + deep
leg B −50%..−85% 30% (V2); real 5-min fees, fee share from real bin snapshots / median liquidity profile × TVL.
On-chain check: bin arrays already exist down to ~−94% in active pools, so deep ranges pay no extra rent.

| Variant, min fees 1 h | Trades | Win rate | 1 slot of $100 | 3 slots of $100 |
|---|---|---|---|---|
| V1, $500 | 64 | 31% | +$19.1 / 26 h (+$17.6/day) | +$25.5 (+$23.6/day) |
| V1, $1,000 | 37 | 27% | +$6.6 (+$6.1/day) | +$13.3 (+$12.3/day) |
| V2, $500 | 66 | 17% | +$11.4 | −$7.7 (3 stops at −21%) |
| V2, $1,000 | 35 | 17% | +$3.4 | +$7.5 |

Reading: most entries never fill (44 of 64 exit "out above": the price bounced away, cost ≈ $0.07); losses are tiny
because nothing filled; **one flip (SWOLF, +17.7%) makes most of the profit**. The deep leg B adds the large losses.
26 h and one good flip is far too little to conclude: re-run daily as data accumulates; paper bot next.
Caveats: fee share assumes nobody outside the ±70-bin snapshot (optimistic for deep bins); token screen uses current
holders/audit (look-ahead); no slippage on entry (SOL only, none needed).
