# Build prompt — "LP Hub": Solana DLMM indexer + web app + LP bot + MCP (copy-paste for the engineer)

---
## PROMPT START

### Why we build this (read first)
Our research (in `docs/research/`) showed that the money on Solana memecoins is made by **active Meteora DLMM liquidity providers** who pick the right pool at the right time, keep liquidity near the price while volume is high, and get out when fees fade or the price starts dumping. How DLMM works and what decides LP profit: `docs/research/dlmm-mechanics.md`. Backtests (fee burst, Rabbit Strat): `docs/research/lp-meteora.md`. Paper model: `docs/research/lp-shadow-prompt.md`.
Goal: a **dashboard of tokens and DLMM pools** (data, filters, charts) plus a **signal engine** that tells us **when to enter a pool, with which range, and when to exit**. The signals are exposed through an **MCP server** (so we can ask Claude "where should I LP now?") and used by our **LP bot** (paper first). Users can also add liquidity from their own wallet. **We do not track or copy other wallets.**

### Git (required)
- Repo: **`github.com/hatemua/lp_solana_dash`** (this repo). Research referenced below is in **`docs/research/`**.
- Layout: `indexer/` (Python), `bot/` (Python), `executor/` (Node/TS, Meteora SDK), `web/` (Next.js), `mcp/` (TS), `infra/` (docker-compose, nginx), `docs/`.
- Branches: `main` is protected; one branch per milestone (`m1-indexer`, `m2-web`, `m3-bot`, `m4-signals`, `m5-mcp`), one **pull request per milestone** into `main` with a short description and screenshots. Commit small and often, push every day.
- **Never commit secrets**: no private keys, `.env`, `.pem`, RPC/API keys, wallet files (`.gitignore` already blocks them). Ship `.env.example` with empty values. Secret scan (gitleaks) in CI.
- CI (GitHub Actions): lint + type-check + unit tests for every service on each PR.

### Stack
| Part | Tech |
|---|---|
| Web app | **Next.js** (App Router, TypeScript), Tailwind, `@solana/wallet-adapter` (Phantom, Solflare, Backpack), **TradingView lightweight-charts** |
| Indexer + bot | **Python 3.11** (asyncio, httpx, SQLAlchemy/asyncpg, pydantic) |
| Chain executor | small **Node/TypeScript** service using **`@meteora-ag/dlmm`** SDK + `@solana/web3.js` (the DLMM SDK is TypeScript only); the Python bot calls it over HTTP on localhost |
| Database | **PostgreSQL 16** (+ TimescaleDB extension if possible, for time series) |
| Cache / queues / live data | **Redis** (latest pool stats, pub/sub to the web app, job queues, rate-limit counters) |
| AI access | **MCP server** (TypeScript, `@modelcontextprotocol/sdk`) |
| Deploy | **docker-compose** (postgres, redis, indexer, api, executor, bot, web, mcp) on our server; nginx in front with HTTPS (Let's Encrypt) |
| RPC | Helius (key in `.env`), public RPC only as backup |

### Domains (production)
| Domain | Serves |
|---|---|
| **`https://lp.joulity.com`** | Next.js web app (dashboard, pools, pool pages, wallet LP, bot dashboard) |
| **`https://lp.api.joulity.com`** | REST API (`/v1/...`), live updates (SSE `/v1/stream` or WebSocket `/v1/ws`), health (`/health`), and the MCP server over streamable HTTP at **`/mcp`** |
- DNS: A/AAAA records of both names → our server; nginx terminates TLS (Let's Encrypt, auto-renew) and proxies to the containers (web :3000, api :8000, mcp :8100). Postgres, Redis and the executor are **not** exposed publicly (internal docker network only).
- CORS on the API: allow only `https://lp.joulity.com` (plus `http://localhost:3000` in dev).
- Web env: `NEXT_PUBLIC_API_URL=https://lp.api.joulity.com`.
- Auth: public read endpoints rate-limited; admin and bot endpoints (`/v1/bot/*`, `/v1/admin/*`) require login (wallet sign-in with an allow-listed admin wallet, or email + 2FA) and an admin session; MCP requires a bearer token.
- Staging (optional): `staging.lp.joulity.com` / `staging.lp.api.joulity.com` from the `develop` branch.

### Data sources
- Meteora DLMM data API `https://dlmm.datapi.meteora.ag`:
  - `/pools?page=&page_size=100&sort_by=volume_24h:desc` (also `pool_created_at:desc`);
  - `/pools/{address}`;
  - `/pools/{address}/ohlcv?timeframe=5m|1h&start_time=&end_time=` (max 6 h per call at 5m, 72 h at 1h);
  - `/pools/{address}/volume/history?timeframe=5m|1h` (fees, protocol_fees).
- Meteora DLMM SDK (via executor): `getActiveBin`, `getBinsAroundActiveBin`, `getPositionsByUserAndLbPair`, add/remove liquidity, claim fees.
- Jupiter: `https://datapi.jup.ag/v1/assets/search?query=<≤50 mints>` (holders, mcap, organicScore, audit, stats5m/1h/24h), `https://datapi.jup.ag/v2/charts/{mint}?interval=1_MINUTE|15_MINUTE&to=&candles=`, `https://lite-api.jup.ag/price/v3?ids=`, `https://lite-api.jup.ag/swap/v1/quote`.
- Helius websockets for pool activity and our bot wallet.

### M1 — Indexer + database
- **Tables:** `tokens` (mint, symbol, name, decimals, created_at, launchpad, holders, mcap, organic_score, audit json, updated_at), `pools` (address, token_x, token_y, bin_step, base_fee_pct, protocol_fee_pct, created_at, launchpad, tags), `pool_stats` (time series every 1–5 min: price, tvl, volume/fees 5m/1h/24h, fee/tvl, dynamic_fee_pct), `pool_ohlcv_5m`, `token_ohlcv_1m` (only for tracked tokens), `bins_snapshot` (for watched pools: active_bin, liquidity per bin ±70 bins, every 30–60 s), `pool_metrics_1m` (computed every minute for hot pools — see M4), `pool_tvl_flow` (TVL / liquidity added and removed per 5 min, from pool snapshots).
- **Jobs:**
  - full pool list every 10 min (all ~137k pools, paged);
  - hot pools (SOL pairs, TVL ≥ $10k or volume 1h ≥ $50k) stats every 60 s;
  - new pools every 1 min;
  - token info (Jupiter) for tokens of hot pools every 5 min;
  - OHLCV backfill 72 h for hot pools;
  - bins snapshots for watched pools.
- Latest stats of hot pools also in Redis (`pool:{addr}` hashes + sorted sets for ranking). Respect rate limits (backoff on 429), idempotent upserts, metrics (rows/min, API errors) on a `/health` endpoint.

### M2 — API + web app (filters + charts)
- API (FastAPI in Python or Next.js route handlers, your choice), read from Redis first, Postgres for history.
- **Pools page**: table with server-side filters and sort:
  - TVL, volume 5m/1h/24h, fees 1h/24h, **fee/TVL 1h and 24h**, dynamic fee %, bin step, base fee %;
  - pool age, token age, mcap, holders, organic score, audit flags (mint/freeze authority, top-10 %, dev holdings);
  - "volume burst" (token 5-min volume ≥ X, e.g. the Rabbit rule $300k / $500k), price change 5m/1h/24h;
  - saved filter presets ("Rabbit 500k", "Fee burst", "Evil Panda", "Safe established"). Live updates every 30 s (Redis pub/sub or SSE).
- **Pool page**:
  - candlestick chart (5m/1h) with volume and fees bars;
  - **liquidity-by-bin chart** around the active bin (who holds what near the price);
  - fee/TVL history;
  - token safety panel;
  - **signal panel**: LP score, entry/exit status, suggested range and shape (from M4), with the reasons.
- **Wallet**: connect wallet; show the user's DLMM positions (value, fees earned, in range / out of range).
- **Add liquidity** (non-custodial): choose range (±%, or bins), shape (spot / curve / bid-ask), one-sided SOL or token or 50/50, amount. Show cost preview: position rent (≈0.057 SOL, refundable), **bin-array rent if new bins (≈0.075 SOL, not refundable)**, estimated swap cost. The transaction is built by the SDK and **signed in the user's wallet**; the server never sees the user's key. Also remove liquidity / claim fees.

### M3 — LP bot (Python) — paper first
- The bot **uses the M4 signals** for entry, range, size and exit (until M4 is ready, each strategy uses its own simple rules below).
- **Strategies** (config files, one class each, same interface):
  - **R500 / R300 (Rabbit)**: volume burst trigger, token side or 50/50, exit on volume fade / stop / range break / max hold;
  - **Fee burst**: hourly volume ≥ 3× median of 24 h, spot ±20%;
  - **Re-center**: narrow range at the active bin, re-center when the active bin leaves the middle N bins or every X min, sell received tokens when > Y% of the position (with a minimum time between re-centers to limit costs);
  - **Evil Panda** (one-sided SOL far below).
- **Paper mode (default)**: simulated positions using the **real bins** (our share per bin = our liquidity ÷ (existing + ours)), fees from pool fee deltas, real Jupiter quotes for swaps, all rents and tx costs (exactly the model in `docs/research/lp-shadow-prompt.md`).
- **Live mode** (only when `BOT_MODE=live` AND an admin confirms in the UI):
  - a **dedicated bot wallet**; its key only in the server's `.env` or a secrets manager, never in git or in the database, never the admin key; the user "inserts money" by sending SOL to that wallet;
  - hard limits in config: max $ per position, max open positions, max % of wallet per pool, **daily loss stop**, total drawdown kill switch, max tx per hour, allowed pools only (filters);
  - every action goes through the executor and is logged (`bot_actions` table with tx signature);
  - a global **STOP button** in the UI and a `POST /bot/stop` endpoint.
- **Bot dashboard**: equity curve, open positions (range, in-range %, fees, P&L split into fees / price / costs), closed positions, per-strategy stats, logs.

### M4 — Signal engine: when to enter, which range, when to exit
Python service that computes, **every minute for every hot pool**, metrics → an **LP score** → **entry / exit signals**, stores them (`pool_metrics_1m`, `signals`), pushes them to Redis, and is validated by a **backtest** on our own stored history.
- **Metrics per pool:**
  - fees: fee/TVL 5m and 1h, **fee velocity** (fees last 5 min ÷ average 5 min of the last hour), dynamic fee %;
  - volume: pool volume 5m/1h, **volume burst** (5 min ÷ average 5 min of 24 h), token volume on all venues 1m/5m (Jupiter);
  - price behaviour: change 5m/15m/1h, realized volatility (1-min returns, 30 min), **chop index** (sum of |moves| ÷ |net move| over 30–60 min: high = back-and-forth = good for LP), trend (net move ÷ volatility), distance from 1 h / 24 h high;
  - liquidity: **liquidity within ±5 / ±20 bins of the active bin**, **our expected share of the active bin for a given $ size**, TVL change 5m/1h (LPs arriving or pulling out);
  - token health: holders change 5m/1h, buy/sell ratio, top-10 %, dev %, mint/freeze authority, organic score, sudden liquidity pull.
- **LP score 0–100** (weights in config): high and rising fees, high chop, not trending down hard, enough room (our share ≥ X% of the active bin), safe token. Show the score and **the reasons** (each component).
- **Entry signal**: score ≥ threshold for N minutes + safety OK. Output: **suggested range** (width from volatility, e.g. ±k·σ for the expected hold), **shape** (spot when choppy, bid-ask when very volatile, one-sided SOL below when the token looks topped), **size** (cap so our share stays below Y% of the active bin), **expected fees per hour** for that size, risk level.
- **Exit signals** (for a given position: pool, range, entry time): fee velocity < 50% of entry level, volume burst over, trend turns down (price in the lower part of the range and falling), price out of range for M minutes, TVL / liquidity pulled fast, holders dropping, safety flag changes. Output: HOLD / RE-CENTER / EXIT + reasons.
- **Backtest** (`backtest/`): replay stored minute data with the paper LP model (real bins when available, `docs/research/lp-shadow-prompt.md`), measure every signal and preset (trades, win %, $/trade, fees vs price vs costs), **walk-forward** (tune on older days, test on newer). Results page in the web app; thresholds are only changed through backtest results.

### M5 — MCP server
- Tools, **read-only**:
  - data: `search_pools(filters)`, `get_pool(address)`, `get_pool_bins(address)`, `get_token(mint)`, `pool_history(address, timeframe)`;
  - signals: **`best_pools_now(strategy, amount_usd)`** (ranked pools with score, suggested range/shape/size, expected fees/h, risks), **`pool_signal(address, amount_usd)`** (score + reasons + entry yes/no), **`exit_check(address, range_low, range_high, entry_time)`** (HOLD / RE-CENTER / EXIT + reasons);
  - research: `backtest_results(strategy)`, `signal_stats(signal)`;
  - bot: `bot_status()`, `bot_positions()`.
- Optional, protected by an admin token: `bot_pause()`, `bot_resume()`. No MCP tool may move funds or sign transactions.
- Served at `https://lp.api.joulity.com/mcp` (streamable HTTP, bearer token). Document how to connect it from Claude (README section).

### Security checklist (must pass before live mode)
- **Keys and secrets:**
  - no secrets in git (CI secret scan);
  - bot key loaded only from env/secrets manager;
  - admin key never used.
- **Access:**
  - web admin pages behind auth;
  - API rate limits;
  - CORS restricted;
  - executor listens on localhost only.
- **Transaction safety:**
  - every live transaction is simulated first (`simulateTransaction`) and checked against limits;
  - slippage limits on every swap.
- **Failure behaviour:** if the RPC is down or the data is stale (> 2 min), the bot opens nothing and keeps existing positions under watch.

### Acceptance per milestone
- **M1:** ≥ 95% of DLMM pools with TVL ≥ $10k indexed; hot pool stats < 2 min old; 72 h OHLCV for hot pools.
- **M2:** live at `https://lp.joulity.com` (API at `https://lp.api.joulity.com`); filters return in < 1 s; pool page shows candles + bins chart; a real add/remove liquidity works from a wallet with 0.1 SOL on a test pool.
- **M3:** paper bot runs 24 h unattended with all strategies; dashboard shows fees/price/cost split.
- **M4:** LP score + entry/exit signals updated every minute for all hot pools; backtest report (walk-forward) for each preset on ≥ 7 days of stored data.
- **M5:** Claude can call `best_pools_now`, `pool_signal` and `exit_check` through MCP at `https://lp.api.joulity.com/mcp`.

Live money only after M3 paper results are positive over ≥ 30 positions and the security checklist is done.

## PROMPT END
