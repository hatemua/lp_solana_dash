# lp-mcp: read-only MCP server

`https://lp.api.joulity.com/mcp`: streamable HTTP (stateless, JSON responses), Node 22 + `@modelcontextprotocol/sdk`.
It calls the REST API on the internal network (`http://api:8000`) with GET requests only.

## Safety

- Every tool is read-only (`readOnlyHint: true`). The API client can only send `GET`; no tool reaches a
  `/v1/tx/*` or `/v1/wallet/*` route. `test/tools.test.ts` checks both, on every tool and in the source.
- No tool builds, signs or sends a transaction, and the server holds no keys.
- The API's per-IP rate limits apply to the real client: the MCP forwards `X-Forwarded-For`.

## Auth

OAuth 2.1, with the API as the authorization server:

| | |
|---|---|
| Protected resource metadata | `/.well-known/oauth-protected-resource/mcp` |
| Authorization server metadata | `/.well-known/oauth-authorization-server` |
| Dynamic client registration | `POST /oauth/register` (public clients, https or loopback redirect URIs) |
| Authorize | `GET /oauth/authorize` → login on lp.joulity.com → consent → code (PKCE S256 required) |
| Token | `POST /oauth/token` (`authorization_code`, `refresh_token`; refresh tokens rotate) |
| Revoke | `POST /oauth/revoke`, or *Connected apps* on https://lp.joulity.com/account |

An unauthenticated request gets `401` with `WWW-Authenticate: Bearer resource_metadata=...`, which is how Claude
discovers the login flow. Access tokens last 1 h, refresh tokens 30 days; only SHA-256 hashes are stored.
The MCP validates a token through `GET /v1/auth/me` and caches the result for 60 s.

`MCP_BEARER_TOKEN` (optional, ≥ 24 chars, server `.env` only) also works as a static bearer token for scripts:

```bash
curl -s https://lp.api.joulity.com/mcp -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Tools

| Tool | What it returns |
|---|---|
| `search_pools` | Pools matching `q` / a preset / `<field>_min`/`_max` filters (compact rows) |
| `get_pool` | One pool: stats, fees, token, safety |
| `get_pool_bins` | Liquidity per bin around the active bin (stored snapshot or live) |
| `get_token` | Token metadata, price, holders, audit |
| `pool_history` | OHLCV + fees (5m/1h, ≤ 72 h), fee/TVL or TVL flow (≤ 168 h) |
| `best_pools_now` | Ranked pools with LP score, entry decision, range/shape/size for `amount_usd` |
| `pool_signal` | Score, metrics and suggestion for one pool |
| `exit_check` | HOLD / RE-CENTER / EXIT for a range opened at `entry_time` |
| `backtest_results`, `signal_stats` | Not available until M4 (returns `available: false`) |
| `bot_status`, `bot_positions` | Not available until M3 (returns `available: false`) |

Signals are the v0 heuristics in the API (`api/src/lp_api/signals.py`) until the M4 engine replaces them.

## Develop

```bash
npm ci
npm run lint && npm run typecheck && npm test
API_URL=http://localhost:8000 PUBLIC_API_URL=http://localhost:8000 npm run dev   # :8100
```
