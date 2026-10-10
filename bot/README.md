# lp-bot: paper LP bot (M3)

Shadow-tests DLMM LP strategies on **live** Meteora data with virtual $100 positions. **Paper only:** no wallet, no
keys, no transactions (`BOT_MODE=paper` is hard-coded in compose; any other value stops the bot).

Dashboard: https://lp.joulity.com/bot · API: `/v1/bot/status`, `/v1/bot/positions` · MCP: `bot_status`, `bot_positions`.

## Strategies

| Id | Entry | Position | Exits |
|---|---|---|---|
| `topped_bid` (S1) | pump ≥ +30% then 15–45% below the peak; pool fees ≥ $500/h; mcap ≥ $250k, ≥ 500 holders, top-10 ≤ 30%, mint/freeze off | SOL-only bid-ask −5%…−55% (90%), 10% idle | flip on +5% bounce after ≥ 70% fill (tokens → ask up to avg buy +10%), TP +5%, trailing +3/−1.5, stop −15%, out of range 30 min, fee death, 6 h |
| `meridian` (S2) | Meridian defaults: TVL $10k–150k, mcap $150k–10M, ≥ 500 holders, organic ≥ 60, top-10 ≤ 60%, bin step 80–125; fee/TVL ≥ 0.5%/h, fee velocity ≥ 1, 1 h change −15%…+30% | SOL-only bid-ask, 69 bins right below the price | TP +5%, trailing, stop −15%, out of range 30 min, 12 h |
| `chop_spot` (S3) | chop ≥ 5, \|trend\| < 1, 5-min volatility 1–5%, fee velocity ≥ 1, same safety as S1 | two-sided spot ±2σ·√12 (5–34 bins a side); half swapped to the token | TP +5%, trailing, stop −10%, out of range 15 min (re-entered after 15 min = re-center), 6 h |
| `topped_bid_v2` (S1b) | S1 + token ≥ 24 h old | as S1 | as S1 + crash exit (−25% from the 30-min high) + exit when the price breaks the pre-bounce low after a flip |
| `evil_panda` (S4) | safe token ≥ 24 h, pumping ≥ +10% in 1 h, bin step ≥ 80 | SOL-only bid-ask −60%…−90% | flip on bounce, TP +5%, stop −20%, below range 60 min, 24 h |
| `grid` (S5) | safe token ≥ 24 h, \|trend\| < 2, volatility 1–8%, fees not fading | two-sided bid-ask grid ±3σ·√12 (8–34 bins a side), half swapped to the token | TP +5%, stop −12%, crash −25%, out of range 30 min, 6 h |
| `fee_leader` (S6) | the table's top pools: fee/TVL ≥ 2%/h and fees ≥ $1k/h; mint/freeze off, top-10 ≤ 30%, ≥ 300 holders; **no age or mcap floor** | two-sided bid-ask grid ±3σ·√12 | TP +5%, stop −10%, crash −20% in 15 min, out of range 15 min, fee death 20 min, 2 h |

All: max 3 open per strategy, one position per token, 1 h cooldown per pool (15 min for S3), rug exit (TVL −50% in
15 min). Rules come from [`docs/research/strategy-playbook.md`](../docs/research/strategy-playbook.md).

## How a minute is simulated

- Price and pool fees: `pool_stats` every minute (`cum_fees` delta = fees earned by the pool in that minute).
- Fees are spread over the bins the price touched; in each of our bins we earn `ours / (ours + others)`, others =
  live bin liquidity from the executor (read-only), or the latest stored snapshot.
- Valuation: DLMM bin composition at the current price; the net counts selling any tokens back to SOL
  (pool fee + 1% slippage), tx costs (0.0002 SOL per action), and the S3 entry swap.

## Run

```bash
pip install -e ".[dev]" && ruff check . && mypy && pytest -q
DATABASE_URL=... REDIS_URL=... EXECUTOR_URL=... lp-bot
```
