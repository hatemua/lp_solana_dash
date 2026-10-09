# 24-hour shadow test — Meteora DLMM LP (Rabbit Strat + fee burst) — copy-paste for the engineer

---
## PROMPT START

### Goal
Build **"LP Shadow"**: a 24-hour **paper** test of LP strategies on Meteora DLMM memecoin pools. The question we must answer: **when we would enter, how much liquidity do the other LPs have in our price range, and what share of the pool fees would our $40 really earn?** The backtest says the Rabbit Strat is profitable only if the other LPs hold less than about $40k in our range; we cannot see that in history, so measure it live from the bins.

**Safety:** paper only. No wallet, no private key, never read `.env` secrets, never build or sign a transaction. The Meteora SDK is used **read-only** (pool and bin data).

### Stack
- Server: same as the copy test. Folder `~/lp-shadow/`, run `python3 run.py --hours 24` (or Node for the SDK part) in tmux/systemd. SQLite `lp.db`.
- **RPC:** Helius free plan (your own key in `config.yaml`, never in git). Public RPC failed 75% of calls in the copy test — do not rely on it.
- Meteora DLMM SDK (`@meteora-ag/dlmm`, read-only): `DLMM.create(connection, pool)`, `getActiveBin()`, `getBinsAroundActiveBin(left, right)` (per bin: binId, price, xAmount, yAmount, supply). A small Node helper that prints JSON is fine; Python calls it.

### Data sources (public, no key except Helius)
| What | Endpoint | How often |
|---|---|---|
| Pool list | `GET https://dlmm.datapi.meteora.ag/pools?page=1..3&page_size=100&sort_by=volume_24h:desc` and `...&sort_by=pool_created_at:desc` (newest 100) | every 5 min |
| Pool snapshot (TVL, fees, `cumulative_metrics.fees`, `protocol_fees`, `pool_config.bin_step`, `base_fee_pct`) | `GET https://dlmm.datapi.meteora.ag/pools/{address}` | every 60 s for pools of tokens with a signal or an open position |
| Pool 5-min fees / price history | `GET .../pools/{address}/volume/history?timeframe=5m&start_time=&end_time=` and `.../ohlcv?timeframe=5m` (max 6 h per call) | at signal time (to compute the last hour / last 24 h) |
| Token volume on all venues + safety | `GET https://datapi.jup.ag/v1/assets/search?query=<mint1,...,mint50>` → `stats5m.buyVolume+sellVolume`, `stats1h.priceChange`, `mcap`, `holderCount`, `organicScore`, `audit{...}`, `createdAt` | every **30 s** for all candidate tokens (50 per call) |
| Bins (the key data) | SDK `getActiveBin()` + `getBinsAroundActiveBin(70, 70)` | at every signal, then every **30 s** while a paper position is open |
| Swap cost | `GET https://lite-api.jup.ag/swap/v1/quote` for $40 SOL→token and back | at entry and exit |

**Candidates:** SOL-paired DLMM pools, TVL ≥ $10k, not stables/majors (exclude SOL, USDC, USDT, cbBTC, ETH, WBTC, JUP, RAY, ZEC, HYPE, PUMP, MET, BONK, WIF, JTO, PYTH, LSTs). For each token use the **pool with the most LP fees in the last hour**.

### Signals (all computed from data available at that moment)
- **R500 (Rabbit, best setups):** token 5-min volume (all venues) ≥ **$500k**.
- **R300 (Rabbit):** token 5-min volume ≥ $300k **or** 1-min volume ≥ $60k (1-min = difference of two 30-s polls, or Jupiter 1-min candle).
- **FB (fee burst):** pool volume last hour ≥ 3 × median hourly volume of the previous 24 h, pool LP fees last hour ≥ $200, token age ≥ 3 days, price 1 h change between −15% and +30%.
- One paper position per token per strategy at a time; after an exit, wait 5 min before a new signal on that token.

### Paper position ($40, simulated bin by bin)
Use the real bins from the SDK at the entry moment.
- **Shape:**
  - R500 / R300: **token side**, spot (equal value per bin) from the active bin up to **+30%** (number of bins = ln(1.3) / ln(1 + bin_step/10 000)). If the token's 1 h price change ≥ +50%: **50/50 spot ±30%** instead.
  - FB: 50/50 spot ±20%.
- **Our share per bin** = our liquidity in that bin / (existing liquidity in that bin + ours), using the bin amounts valued at the bin price. Refresh the existing amounts every 30 s (others add and remove liquidity).
- **Composition:** follow DLMM rules from the active bin id every 30 s: bins above the active bin hold token, bins below hold SOL, the active bin is mixed. When the active bin moves up, our token in the bins crossed is sold at those bin prices; when it moves down, our SOL buys token at those bin prices.
- **Fees:** every 60 s, pool LP fees in the interval = Δ(`cumulative_metrics.fees` − `protocol_fees`) (or the 5-min fee bar if the snapshot is not fresh). Our fees = that × our share in the bins the active bin visited in the interval (time-weighted); zero when the active bin is outside our range.
- **Costs:**
  - swap: real Jupiter quote for buying the token part at entry and selling the token left at exit (store the quoted price impact + fee);
  - gas $0.03 per open and per close;
  - position rent ≈ 0.057 SOL: refundable, just record it;
  - **bin-array creation:** count the bin arrays in our range that do not exist yet (SDK / account lookup) × their rent, **not refundable** → add as a cost.
- **Exits:**
  - R500 / R300: after ≥ 15 min, when the token's 5-min volume (15-min average) < half the trigger; stop at −10% of position value (incl. fees) checked every 30 s; price above our range for 3 min; max 2 h.
  - FB: 30-min volume < 0.5 × the entry hourly rate (after ≥ 30 min); out of range 15 min; stop −20%; max 6 h.
- Store for every position: entry/exit time, pool, bin step, fee tier, **others' $ in our range at entry and the median while open**, our median share in the active bin, fees $, price P&L $, swap cost $, bin-array cost $, exit reason, max gain/drawdown.

### Also log every signal, even without a position
Pool, token, volume, TVL, active bin, **others' $ within ±30% of the price**, others' $ in the active bin. This gives the distribution we need even if the paper wallets are full.

### Paper wallets
One per strategy: **$100**, positions of $40, max 2 open (the rest is for rent/gas), daily stop −$30. Also keep an unconstrained tally (every signal gets a position) so we get enough trades.

### LIVE results (required, like the copy test)
Every **5 min** write `reports_lp/live.html` and `live.txt`, served at `/lp/` with HTTP basic auth (send URL and credentials):
- Overview: running time, candidates, signals per strategy, open positions, Helius ok/429/errors.
- Per strategy: trades, closed, open, win %, $/trade (closed), realized $, unrealized $, total $, fees $ vs price $ vs cost $.
- **Liquidity table:** at signal time, median and p25/p75 of others' $ in our range and in the active bin; our median share in the active bin; split by token age (< 1 d / 1–3 d / > 3 d).
- **Break-even:** per strategy, the others-in-range $ below which the trades were profitable.
- Last 20 positions with status; top 10 tokens by our fees.
- `python3 report.py --now` (same tables in the terminal) and `python3 export.py --partial` (export without stopping).

### Exports
At 12 h and 24 h: zip parts ≤ 18 MB, **signals and positions tables in part 1**, send all parts.

### Acceptance
Runs 24 h unattended; every signal has the bin data and others-in-range $; every position has fees/price/cost split; live page updates every 5 min; no key, wallet or transaction code.

## PROMPT END
