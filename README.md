# LP Solana Dash

Indexer of Solana tokens and Meteora DLMM pools, web dashboard (filters, charts, wallet LP), Python LP bot (paper first), signal engine (when to enter / exit a pool) and a read-only MCP server.

- **Build spec for the engineer:** [`docs/BUILD_PROMPT.md`](docs/BUILD_PROMPT.md) (milestones M1–M5)
- **Research behind it:** [`docs/research/`](docs/research/)
  - [`dlmm-mechanics.md`](docs/research/dlmm-mechanics.md): how DLMM bins, fees and costs work
  - [`reverse-engineer-wallets.md`](docs/research/reverse-engineer-wallets.md): profitable wallets are active DLMM LPs
  - [`lp-meteora.md`](docs/research/lp-meteora.md): LP backtests (fee burst, Rabbit Strat)
  - [`lp-shadow-prompt.md`](docs/research/lp-shadow-prompt.md): paper-trading model with real bin liquidity

Production: web `https://lp.joulity.com`, API + MCP `https://lp.api.joulity.com`.

## Connect Claude (MCP)

Read-only MCP server at **`https://lp.api.joulity.com/mcp`** (streamable HTTP, OAuth 2.1). Tools: `search_pools`,
`get_pool`, `get_pool_bins`, `get_token`, `pool_history`, `best_pools_now`, `pool_signal`, `exit_check`,
`backtest_results`, `signal_stats`, `bot_status`, `bot_positions`. **No tool can trade, sign or move funds.**

1. Create an account at https://lp.joulity.com/signup.
2. Add the server:
   - **claude.ai / Claude Desktop:** Settings → Connectors → *Add custom connector* → URL
     `https://lp.api.joulity.com/mcp` → *Connect*. You are sent to LP Dash: log in, click **Allow**.
   - **Claude Code:** `claude mcp add --transport http lp-dash https://lp.api.joulity.com/mcp`, then `/mcp` →
     *Authenticate* (opens the browser for the same login and Allow).
3. Ask e.g. *"best pools to LP now with $200"*, *"pool signal for <address>"*, *"exit check for my range 0.0012–0.0016
   on <address> opened at 10:00 UTC"*.

Revoke an app at https://lp.joulity.com/account. Details: [`mcp/README.md`](mcp/README.md).

Rules: paper mode first; live money only after positive paper results and the security checklist; no secrets in git.
