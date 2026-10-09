# LP Solana Dash

Indexer of Solana tokens and Meteora DLMM pools, web dashboard (filters, charts, wallet LP), Python LP bot (paper first), signal engine (when to enter / exit a pool) and a read-only MCP server.

- **Build spec for the engineer:** [`docs/BUILD_PROMPT.md`](docs/BUILD_PROMPT.md) (milestones M1–M5)
- **Research behind it:** [`docs/research/`](docs/research/)
  - [`dlmm-mechanics.md`](docs/research/dlmm-mechanics.md): how DLMM bins, fees and costs work
  - [`reverse-engineer-wallets.md`](docs/research/reverse-engineer-wallets.md): profitable wallets are active DLMM LPs
  - [`lp-meteora.md`](docs/research/lp-meteora.md): LP backtests (fee burst, Rabbit Strat)
  - [`lp-shadow-prompt.md`](docs/research/lp-shadow-prompt.md): paper-trading model with real bin liquidity

Production: web `https://lp.joulity.com`, API + MCP `https://lp.api.joulity.com`.

Rules: paper mode first; live money only after positive paper results and the security checklist; no secrets in git.
