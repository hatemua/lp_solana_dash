# Meteora DLMM LP on memecoins — fee-burst strategy (backtest 2026-10-08)

## Idea
Pick a good token, enter a concentrated LP when volume bursts (fees boost), exit when the burst fades.

## Data
- Meteora public API `dlmm.datapi.meteora.ag`: `/pools` (snapshot), `/pools/{addr}/ohlcv?timeframe=5m` and `/pools/{addr}/volume/history?timeframe=5m` (fees, protocol_fees), max 6 h per call, no key. 1h timeframe: 72 h per call.
- No bin-level liquidity endpoint → our share of the active-bin liquidity is unknown; modelled as k × (our $ / pool TVL) × (0.2 / range half-width).
- 194 SOL–memecoin pools (top 500 by 24 h volume, TVL ≥ $10k), last 72 h of 5-min bars; Jupiter token stats for filters.

## Snapshot (24 h)
| Group | Pools | fee/TVL 24 h median | price 24 h median | passive LP net median |
|---|---|---|---|---|
| all | 194 | 1.0% | −2.0% | −0.4% |
| good (age ≥ 3 d, mcap ≥ $250k, holders ≥ 500, score ≥ 70, fees ≥ 25 SOL) | 109 | 1.0% | −2.4% | −0.6% |
| good, fee tier ≥ 1% | 40 | 1.7% | −0.9% | +1.3% |

## Backtest (`ideas/lp/bt.py`, `bt2.py`)
- Entry: good token (age ≥ 3 d), last-hour volume ≥ M × median hourly volume of the previous 24 h, LP fees last hour ≥ $200, price 1 h change between −15% and +30%. Enter at bar close.
- Position: $40, concentrated (Uniswap-v3 math) spot ±20%; fees only while price is in range; swap cost on the token side at entry and exit.
- Exit: 30-min volume < 0.5 × entry rate (fade), out of range 15 min, stop −20% at bar low, max 6 h.
- Result (239 trades): median hold 45 min, 220/239 exits on fade. Fees per trade small (median $0.07, mean $0.21 at k=2).

| swap cost per side \ fee share k | k=1 | k=2 | k=4 | k=8 |
|---|---|---|---|---|
| pool fee + 0.3% | −$0.44 | −$0.33 | −$0.12 | +$0.29 |
| 0.6% (Jupiter route) | −$0.21 | −$0.10 | +$0.11 | +$0.52 |
| 0.3% | −$0.11 | +$0.01 | +$0.21 | +$0.63 |

Other variants (k=2, direct swap): burst M=2 −$0.43, M=5 −$0.42; range ±10% −$0.53, ±30% −$0.30; SOL-only −30% −$0.06; Evil Panda −85% −$0.03 (almost never filled in 6 h); no good filter −$0.40; fade 0.3 −$0.22.

## Read
- The burst lasts ~45 min; fees on $40 in that time are cents unless our share of the active bin is high (k ≥ 4–8).
- The deciding unknown is k: measure it live with the DLMM SDK (`getActiveBin`, `getBinsAroundActiveBin`) → real share of active-bin liquidity.
- Swap cost of entering 50/50 eats most of the fees in 2% pools; route the swap via Jupiter or use one-sided positions.
- Biases: universe = pools active today (survivorship, favours results); TVL now used as TVL then.
- Verdict: not proven; best case ≈ +1.3% per hour-long position. Needs a live shadow test with real bin liquidity before any money.

## Rabbit Strat backtest (CryptoWhiskers, lparmy.com/strategies)
Rules: volume ≥ $300k in 5 min (or $50–80k in 1 min); best setups $500k–1M+ in 5 min. Enter token side; 50/50 spot if the coin already pumped hard. Exit fast when volume drops. Pump → dump → pump again: size up, stay longer.

Data (`ideas/lp/fetch2.py`, `rabbit.py`): 248 DLMM pools / 172 tokens (194 active pools + every SOL pool created in the last 3.5 days with ≥ $100k volume, incl. dead ones), Jupiter 1-min token volume across all venues (~4 days), pool 5-min LP fees.
Model: $40; token side = range [P0, P0×1.3] (50/50 ±30% if 1 h change ≥ +50%); fee share = $40 / ($40 + other LPs' $ in our range); swap cost per side on the token part; stop −10% at the minute low; exit when volume (15-min avg) < half the trigger after ≥ 15 min, price above range 3 min, or 2 h.

| Setup | Trades (≈4 days) | Avg per $40 | Break-even: other LPs in our range ≤ |
|---|---|---|---|
| all tokens, 300k, 5-min hold, 1% swap, others $50k | 817 | −$0.92 | $12k |
| memecoins, 300k, 15-min min hold, 0.5% swap, others $25k | 341 | −$0.38 | $26k |
| memecoins, 500k (best setups), others $10k | 39 | +$6.97 | $53k |
| memecoins, 500k, others $25k | 40 | +$1.09 | $45k |
| memecoins, 500k, others $50k | 40 | −$1.08 | $38k |

- Profit at 500k/others $25k comes from one token (AUTON +$56.8 of +$43.5); without it −$0.37 per trade. 1st half +$2.53, 2nd half −$0.35.
- Pump-dump-pump rule: worse (−$1.44, staying 2× longer −$2.33).
- Exiting after 5 min makes the swap cost (≈ $0.80) larger than the fees; 1% swap per side kills it, 0.5% is needed.
- Current TVL of the pools traded: median ~$190k → other LPs within ±30% are plausibly > $40k, i.e. around or above break-even.
- Verdict: works only if (a) volume ≥ $500k/5 min on a memecoin, (b) other LPs' liquidity in our range < ~$40k, (c) swap ≤ 0.5%/side. (b) must be measured live from bin data (DLMM SDK) — next step is a shadow test that records the active-bin liquidity at each signal.
