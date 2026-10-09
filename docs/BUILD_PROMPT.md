# Build prompt — "LP Hub": Solana DLMM indexer + web app + LP bot + MCP (copy-paste for the engineer)

---
## PROMPT START

### Why we build this (read first)
Our research (in `docs/research/`) showed:
- The "profitable smart wallets" on Solana are mostly **active Meteora DLMM liquidity providers**, not traders: they add liquidity in a narrow range at the current price, remove and re-add every 1–5 min to stay in the active bin, and sell the tokens they receive (`docs/research/reverse-engineer-wallets.md`).
- How DLMM works and what decides LP profit: `docs/research/dlmm-mechanics.md`.
- Backtests and the Rabbit Strat analysis: `docs/research/lp-meteora.md`. Shadow-test spec: `docs/research/lp-shadow-prompt.md`.
Goal: one platform that **indexes all Solana tokens and DLMM pools**, lets a user **find pools with filters and charts**, **add liquidity from their own wallet**, or **run an LP bot** (paper first, real money only when enabled), and exposes the data to AI agents through an **MCP server**.

### Git (required)
- Repo: **`github.com/hatemua/lp_solana_dash`** (this repo). Research referenced below is in **`docs/research/`**.
- Layout: `indexer/` (Python), `bot/` (Python), `executor/` (Node/TS, Meteora SDK), `web/` (Next.js), `mcp/` (TS), `infra/` (docker-compose, nginx), `docs/`.
- Branches: `main` is protected; one branch per milestone (`m1-indexer`, `m2-web`, `m3-bot`, `m4-lp-tracker`, `m5-mcp`), one **pull request per milestone** into `main` with a short description and screenshots. Commit small and often, push every day.
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
- Helius websockets for wallet/pool activity (LP wallets, our bot wallet).

### M1 — Indexer + database
- **Tables:** `tokens` (mint, symbol, name, decimals, created_at, launchpad, holders, mcap, organic_score, audit json, updated_at), `pools` (address, token_x, token_y, bin_step, base_fee_pct, protocol_fee_pct, created_at, launchpad, tags), `pool_stats` (time series every 1–5 min: price, tvl, volume/fees 5m/1h/24h, fee/tvl, dynamic_fee_pct), `pool_ohlcv_5m`, `token_ohlcv_1m` (only for tracked tokens), `bins_snapshot` (for watched pools: active_bin, liquidity per bin ±70 bins, every 30–60 s), `lp_wallets` + `lp_events` (add/remove/claim per wallet, pool, bins, amounts, tx).
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
  - recent LP events.
- **Wallet**: connect wallet; show the user's DLMM positions (value, fees earned, in range / out of range).
- **Add liquidity** (non-custodial): choose range (±%, or bins), shape (spot / curve / bid-ask), one-sided SOL or token or 50/50, amount. Show cost preview: position rent (≈0.057 SOL, refundable), **bin-array rent if new bins (≈0.075 SOL, not refundable)**, estimated swap cost. The transaction is built by the SDK and **signed in the user's wallet**; the server never sees the user's key. Also remove liquidity / claim fees.

### M3 — LP bot (Python) — paper first
- **Strategies** (config files, one class each, same interface):
  - **R500 / R300 (Rabbit)**: volume burst trigger, token side or 50/50, exit on volume fade / stop / range break / max hold;
  - **Fee burst**: hourly volume ≥ 3× median of 24 h, spot ±20%;
  - **Re-center** (what the profitable LP wallets do): narrow range at the active bin, re-center when the active bin leaves the middle N bins or every X min, sell received tokens when > Y% of the position (with a minimum time between re-centers to limit costs);
  - **Evil Panda** (one-sided SOL far below).
- **Paper mode (default)**: simulated positions using the **real bins** (our share per bin = our liquidity ÷ (existing + ours)), fees from pool fee deltas, real Jupiter quotes for swaps, all rents and tx costs (exactly the model in `docs/research/lp-shadow-prompt.md`).
- **Live mode** (only when `BOT_MODE=live` AND an admin confirms in the UI):
  - a **dedicated bot wallet**; its key only in the server's `.env` or a secrets manager, never in git or in the database, never the admin key; the user "inserts money" by sending SOL to that wallet;
  - hard limits in config: max $ per position, max open positions, max % of wallet per pool, **daily loss stop**, total drawdown kill switch, max tx per hour, allowed pools only (filters);
  - every action goes through the executor and is logged (`bot_actions` table with tx signature);
  - a global **STOP button** in the UI and a `POST /bot/stop` endpoint.
- **Bot dashboard**: equity curve, open positions (range, in-range %, fees, P&L split into fees / price / costs), closed positions, per-strategy stats, logs.

### M4 — LP wallet tracker
Track the LP wallets from our research (list in `docs/research/reverse-engineer-wallets.md`; start with `3ZhqYVzg3RsoPhyRWTtGxB6F9LWyRBrpbVQ8bkaAkC1x`, `2WMJxEGiEGgFqaJKwyp45DVvDArSrGjaCEkpNoM9mrbz`, `syfpFKDZZbnfrdvE6dPRYXT12SUM2qDUR3zkUP2pXBx`, `AQPh29SFaSbsMTef9KzbEC9pVfthtjksnBLPyKiMxqs4`, `SF2QWGL9LHfxCNkNViskAGUELajCs8ASHZ6iVLvK4wM`).
- Decode their DLMM actions (program `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9t5`): pool, bin range, amounts, time between re-centers, tokens sold after.
- Compute **real P&L from SOL flows + position value** (not tracker PnL; trackers count LP withdrawals as free tokens).
- Page: per wallet equity curve, which pools, range widths, re-center frequency → this is how we learn the exact rules to put in the Re-center strategy.

### M5 — MCP server
- Tools, **read-only**:
  - `search_pools(filters)`, `get_pool(address)`, `get_pool_bins(address)`, `get_token(mint)`;
  - `top_opportunities(preset)`;
  - `lp_wallet_activity(wallet)`;
  - `bot_status()`, `bot_positions()`, `strategy_stats(strategy)`.
- Optional, protected by an admin token: `bot_pause()`, `bot_resume()`.
- No MCP tool may move funds or sign transactions.
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
- **M4:** real P&L curve for the 5 LP wallets over 7 days.
- **M5:** Claude can call `search_pools` and `top_opportunities` through MCP at `https://lp.api.joulity.com/mcp`.

Live money only after M3 paper results are positive over ≥ 30 positions and the security checklist is done.

## PROMPT END
