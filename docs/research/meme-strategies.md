# Memecoin bot strategies: research notes (2026-10-11)

Paper (demo) trading only. No public backtest of a memecoin strategy was found, so these templates are hypotheses;
the house bots and the meme_snapshots table are how we test them.

## What the research says

- **Momentum vs reversal depends on size.** Weekly reversal shows up only in small, illiquid coins, while large
  liquid coins show momentum; the distance from the 1-week high predicts negative returns for small coins
  (Fičura 2023, [FFA working paper](https://wp.ffu.vse.cz/artkey/wps-202301-0003_impact-of-size-and-volume-on-cryptocurrency-momentum-and-reversal.php)).
  Applied: *Blue-chip trend* (large memes, momentum), *Dip reversal* (mid memes, bounce).
- **Narratives rotate.** Memes group into dog, cat, frog, AI, political, celebrity; one narrative leads for days to
  weeks and capital then rotates, with 24-48 h rotations common in 2025
  ([Sharpe narrative tracker](https://www.sharpe.ai/learn/memecoin-narrative-tracker), vendor claims, unverified).
  Applied: *Narrative rotation* ranks narratives by their liquid tokens' median 1 h / 6 h move and turnover.
- **Bounces after big drops** of 20-40% within 48-72 h are claimed for tokens down 50%+ with liquidity holding
  ([Sharpe losers](https://www.sharpe.ai/memecoins/losers), no methodology). Applied: *Dip reversal* waits for
  the turn (1 h up, buyers ahead) instead of catching the knife.
- **New launches mostly die.** About 69% of pump.fun tokens last traded on launch day and 4.55% still trade after
  90 days ([CoinGecko via Crypto Briefing](https://cryptobriefing.com/pump-fun-token-survival-rate-study/));
  24 h graduation rate ~0.2% ([arXiv 2607.02823](https://arxiv.org/abs/2607.02823)). Applied: *Fresh momentum* is
  high risk, needs real liquidity, holders, organic buyers, socials, and sells half at +30%.
- **Risk practice** (bot guides, e.g. [gts.ai](https://gts.ai/defi/pumpfun-bot)): a max loss per trade, a max per
  token, a daily stop; stops can slip on thin liquidity. Applied: every template has a stop, a fast-drop exit,
  a rug exit on liquidity loss, sell-pressure exit, max holding time, round-trip cost check before buying.
- **Our own LP-bot data** (crash_study): tokens younger than 24 h crash ~50x more often; a re-entry right after a
  win lost $6.91 on QI. Applied: age floors on most templates and a per-bot cooldown after selling a token.

## Data sources

- Jupiter tokens v2 lists (trending 5m/1h/6h/24h, most traded, top organic, recent): price, mcap, liquidity,
  holders, audit (mint/freeze, top holders), organic score, buy/sell volume per window.
- DexScreener: boosts (paid promotion), new profiles, community takeovers, and per-pair txns (buy/sell counts),
  volume and liquidity across all DEXes, socials. Used to cross-check the Jupiter price (mismatch flag).
- Jupiter swap quotes (read-only) to price every simulated buy and sell, including the round trip at entry.

## Templates

| Template | Entry | Exit | Risk |
|---|---|---|---|
| Blue-chip trend | mcap ≥ $10M, liq ≥ $500k, ≥ 7 d, 1 h +2..15%, 6 h ≥ 0, buy/sell ≥ 1.2 | stop 6%, trail 5% from +4%, 12 h | low |
| Dip reversal | mcap $1-50M, ≥ 3 d, 24 h ≤ -20%, 1 h ≥ +1%, 5 m ≥ 0, buy/sell ≥ 1.2 | TP 15%, stop 10%, trail 6% from +8%, 24 h | medium |
| Narrative rotation | top-2 narratives by heat, ≥ 1 d, liq ≥ $100k, momentum ≥ 50 | stop 10%, trail 8% from +6%, narrative leaves top 4, 12 h | medium |
| Safe runner | ≥ 1 d, mcap ≥ $1M, holders ≥ 2k, top-10 ≤ 30%, organic ≥ 50, rising | half at +20%, trail 7%, stop 8%, 8 h | medium |
| Fresh momentum | 2-24 h old, liq ≥ $50k, holders ≥ 500, organic ≥ 50, 1 h +5..60% | half at +30%, trail 15%, stop 15%, 6 h | high |

All: no red flag, not dumping 5 m, no 5 m sell pressure, round-trip cost ≤ 3% (4% for fresh), cooldown 6 h,
no re-buy for 24 h after a crash exit, entries paused while SOL falls 4% in 1 h.
