# DLMM LP strategies found on the web (2026-10-10)

Public sources only. None of them publishes a backtest or a win rate: these are rules people use, not proven edges.
Our own backtests are in [`lp-meteora.md`](lp-meteora.md).

## Common ground

- Rank pools by **fees relative to the liquidity that can be hit** (fee / active TVL), over several windows rather
  than one 24 h number. Raw volume alone is not enough: high volume into a deep pool pays little per dollar.
- Prefer **repeatable activity** (chop) over a single spike; avoid entering right after a vertical move.
- Memecoins: wide ranges, bin step 80–125, assume the token can go to zero, decide exits before entry.
- Bid-ask placed **below the price, SOL only** is the most common memecoin setup: earns fees on the dump and buys
  the token in stages instead of holding it from the start.

## Rules by source

| Source | Screen | Position | Exit |
|---|---|---|---|
| [LP-Meteora-Scout-Bot](https://github.com/querastudio/LP-Meteora-Scout-Bot) | TVL ≥ $10k, vol 24 h ≥ $100k, fee/TVL 1 h ≥ 0.5%, 24 h ≥ 10%; ≥ 10 trades/h, 1 h change −30%..+50%, ≤ 30% below 24 h high, 5 m change ≥ −3%; best sibling pool per token | (alerts only) | – |
| [Meridian](https://github.com/yunus-0x/meridian) | fee/active-TVL ≥ 0.05, TVL $10k–150k, mcap $150k–10M, ≥ 500 holders, organic ≥ 60, top-10 ≤ 60%, bot holders ≤ 30%, bin step 80–125, ≥ 30 SOL all-time fees | bid-ask, SOL only below price (e.g. 69 bins) | out of range 30 min, stop −15%, take profit at fees 5% of capital, trailing (+3% trigger, −1.5% drop) |
| [Evil Panda plugin](https://clawhub.ai/plugins/lp-evil-panda) | vol 24 h ≥ $50k, liquidity ≥ $10k, mcap $100k–50M, pool age 1–720 h, 1 h change ≥ +10%, organic ≥ 30, mint/freeze off | spot, SOL only, range −80% to −90% below price | 2 of 3 on 15 m candles: close above Bollinger(20,2), RSI(2) > 90, first green MACD(12/26/9) bar; 24 h stop if out of range and losing; max 3 positions |
| Evil Panda (creator, [X](https://x.com/evilpanda)) | coins that chop sideways; enter after a dump, not on the pump | wide two-sided bid-ask | sell on the first bounce; volume $300k–500k exit rule |
| [LP-METEORA-BOT](https://github.com/aldiopramudya/LP-METEORA-BOT) | "mature, active-but-calm" memecoin pools (5 m data); rules from ~130 manual positions | bid-ask, SOL only below price | max 6 h, stop-loss, fee death ("party over"), price runs above range |
| [meteoracle](https://github.com/juliench82/meteoracle) | sort by fee/TVL 1 h; TVL ≥ $500, fees 24 h ≥ $5, fee/TVL 24 h ≥ 0.5% | SOL side (Evil Panda) | 4 h rolling fee/TVL below a threshold |
| Rabbit Strat (lparmy.com) | token volume ≥ $300k in 5 min, best $500k–1M+ | token side, or 50/50 spot after a big pump | exit fast when volume drops |
| [Practical DLMM guide](https://www.sebmonty.link/blog/complete-meteora-dlmm-liquidity-course) | sort by fees / active TVL, TVL ≥ $1k first pass, two-way flow, holder clusters | range from a reason you can state | range broken and thesis gone, fees dry up, preset P&L levels |

## Added to the dashboard

Presets `high_volume` (Scout) and `meridian`, next to `rabbit500`, `rabbit300`, `fee_burst`, `evil_panda` and
`safe_established`. Usable in the web filters, `GET /v1/pools?preset=...`, `/v1/signals/best?strategy=...` and the
MCP (`search_pools`, `best_pools_now`).

## To test (M3 paper bot / M4 backtest)

1. SOL-only bid-ask below price on `meridian` pools with Meridian's exits, against our fee-burst entry.
2. Evil Panda exits (2 of 3 indicators on 15 m) versus our volume-fade exit.
3. Fee / active TVL (fees ÷ liquidity within ±20 bins) as the main rank, instead of whole-pool fee/TVL.
