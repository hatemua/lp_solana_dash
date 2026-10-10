import { z } from "zod";
import type { Api } from "./api.js";

/**
 * Read-only tools. None of them can build, sign or send a transaction, or move funds: the API client only
 * issues GET requests and no tool calls a transaction or wallet route (enforced by test/tools.test.ts).
 */

const ADDR = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/, "base58 Solana address");
const AMOUNT = z.number().positive().max(1_000_000).default(100).describe("Position size in USD");
const PRESET = z.enum([
  "rabbit500", "rabbit300", "fee_burst", "evil_panda", "safe_established", "high_volume", "meridian",
]);

// compact pool row for list results (the API returns many more columns)
const POOL_FIELDS = [
  "address", "name", "token_symbol", "token_mint", "price", "tvl", "volume_1h", "volume_24h", "fees_1h", "fees_24h",
  "fee_tvl_1h", "fee_tvl_24h", "bin_step", "base_fee_pct", "dynamic_fee_pct", "volume_burst", "price_change_1h",
  "price_change_24h", "pool_age_h", "token_age_h", "mcap", "holders", "organic_score", "top10_pct",
  "mint_disabled", "freeze_disabled", "lp_score",
];

function pick(row: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of keys) if (row[k] !== undefined && row[k] !== null) out[k] = row[k];
  return out;
}

function notYet(milestone: string, what: string) {
  return {
    available: false,
    milestone,
    note: `${what} is not deployed yet (${milestone}). This tool will return data once it is.`,
  };
}

export interface ToolDef {
  name: string;
  title: string;
  description: string;
  input: Record<string, z.ZodTypeAny>;
  // args are validated by the MCP SDK against `input` before run() is called
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  run: (api: Api, args: any) => Promise<unknown>;
}

export const TOOLS: ToolDef[] = [
  {
    name: "search_pools",
    title: "Search Meteora DLMM pools",
    description:
      "Search and filter indexed Meteora DLMM pools. `q` matches symbol, name, pool or mint address. `filters` takes " +
      "<field>_min / <field>_max for numeric fields (tvl, price, volume_5m/1h/24h, fees_5m/1h/24h, fee_tvl_1h/24h, " +
      "dynamic_fee_pct, bin_step, base_fee_pct, pool_age_h, token_age_h, mcap, holders, organic_score, top10_pct, " +
      "dev_pct, token_volume_5m/1h, price_change_5m/1h/24h, volume_burst, lp_score) and booleans (sol_pair, is_hot, " +
      "mint_disabled, freeze_disabled). Fee/TVL values are percentages.",
    input: {
      q: z.string().max(64).optional(),
      preset: PRESET.optional().describe("Apply a strategy preset (explicit filters override it)"),
      filters: z.record(z.union([z.number(), z.boolean()])).optional(),
      sort: z.string().max(32).optional().describe("Any numeric field, pool_created_at or token_symbol"),
      order: z.enum(["asc", "desc"]).optional(),
      limit: z.number().int().min(1).max(50).default(20),
    },
    async run(api, a) {
      const d = (await api.get("/v1/pools", {
        ...(a.filters ?? {}),
        q: a.q,
        preset: a.preset,
        sort: a.sort,
        order: a.order,
        limit: a.limit,
      })) as { total?: number; data?: Record<string, unknown>[] };
      return { total: d.total, pools: (d.data ?? []).map((p) => pick(p, POOL_FIELDS)) };
    },
  },
  {
    name: "get_pool",
    title: "Pool details",
    description: "Latest stats, fees, token info and safety fields of one pool.",
    input: { address: ADDR },
    run: (api, a) => api.get(`/v1/pools/${a.address}`),
  },
  {
    name: "get_pool_bins",
    title: "Pool liquidity bins",
    description:
      "Liquidity per bin around the active bin (amounts of token X and Y, price per bin). Uses the latest stored " +
      "snapshot, or reads the chain when `live` is true.",
    input: {
      address: ADDR,
      live: z.boolean().default(false),
      each_side: z.number().int().min(5).max(70).default(20).describe("Bins on each side of the active bin"),
    },
    run: (api, a) => api.get(`/v1/pools/${a.address}/bins`, { live: a.live, each_side: a.each_side }),
  },
  {
    name: "get_token",
    title: "Token details",
    description: "Token metadata, price, market cap, holders, organic score and audit (mint/freeze authority, top 10).",
    input: { mint: ADDR },
    run: (api, a) => api.get(`/v1/tokens/${a.mint}`),
  },
  {
    name: "pool_history",
    title: "Pool history",
    description:
      "Time series for a pool. kind=ohlcv: price candles with volume and LP fees (5m or 1h, up to 72 h); " +
      "kind=fee_tvl: TVL and fee/TVL snapshots (up to 168 h); kind=flow: TVL change split into price effect and " +
      "net deposits/withdrawals (up to 168 h).",
    input: {
      address: ADDR,
      kind: z.enum(["ohlcv", "fee_tvl", "flow"]).default("ohlcv"),
      timeframe: z.enum(["5m", "1h"]).default("5m").describe("ohlcv only"),
      hours: z.number().int().min(1).max(168).default(24),
    },
    run(api, a) {
      if (a.kind === "ohlcv") {
        return api.get(`/v1/pools/${a.address}/ohlcv`, { timeframe: a.timeframe, hours: Math.min(a.hours, 72) });
      }
      return api.get(`/v1/pools/${a.address}/${a.kind === "fee_tvl" ? "fee-tvl" : "flow"}`, { hours: a.hours });
    },
  },
  {
    name: "best_pools_now",
    title: "Best pools to LP now",
    description:
      "Ranks current candidate pools by LP score (signals v0 heuristics, not yet backtested) and suggests a range, " +
      "shape and size for `amount_usd`. strategy: any or a preset id. Pools with entry=false list their reasons.",
    input: {
      strategy: z.union([z.literal("any"), PRESET]).default("any"),
      amount_usd: AMOUNT,
      limit: z.number().int().min(1).max(25).default(10),
    },
    run: (api, a) => api.get("/v1/signals/best", { strategy: a.strategy, amount_usd: a.amount_usd, limit: a.limit }),
  },
  {
    name: "pool_signal",
    title: "LP signal for one pool",
    description:
      "LP score, entry decision, metrics (fee/TVL, fee velocity, chop, trend, volatility, active-bin depth, safety) " +
      "and a suggested range/shape/size for one pool (signals v0).",
    input: { address: ADDR, amount_usd: AMOUNT },
    run: (api, a) => api.get(`/v1/signals/pool/${a.address}`, { amount_usd: a.amount_usd }),
  },
  {
    name: "exit_check",
    title: "Exit check for an open range",
    description:
      "HOLD / RE-CENTER / EXIT for a position with the given price range (token Y per token X, as the pool quotes " +
      "it) opened at entry_time (unix seconds or ISO date).",
    input: {
      address: ADDR,
      range_low: z.number().positive(),
      range_high: z.number().positive(),
      entry_time: z.union([z.number().positive(), z.string().max(40)]),
    },
    async run(api, a) {
      const t = typeof a.entry_time === "number" ? a.entry_time : Date.parse(a.entry_time) / 1000;
      if (!Number.isFinite(t)) throw new Error("entry_time: use unix seconds or an ISO date");
      return api.get(`/v1/signals/exit-check/${a.address}`, {
        range_low: a.range_low,
        range_high: a.range_high,
        entry_time: t,
      });
    },
  },
  {
    name: "backtest_results",
    title: "Backtest results",
    description: "Results of the signal backtest (M4).",
    input: {},
    run: async () => notYet("M4", "The signal engine backtest"),
  },
  {
    name: "signal_stats",
    title: "Signal statistics",
    description: "Hit rate and returns of past signals (M4).",
    input: {},
    run: async () => notYet("M4", "Signal tracking"),
  },
  {
    name: "bot_status",
    title: "Paper bot status",
    description:
      "Paper LP bot (virtual $100 positions on live pool data, no wallet): heartbeat and, per strategy " +
      "(topped_bid, meridian, chop_spot), open/closed counts, win rate, realized and unrealized P&L, fees, costs " +
      "and the equity curve.",
    input: {},
    run: (api) => api.get("/v1/bot/status"),
  },
  {
    name: "bot_positions",
    title: "Paper bot positions",
    description: "Positions of the paper LP bot: pool, range, entry/last price, net %, P&L, fees, costs, exit reason.",
    input: {
      status: z.enum(["open", "closed", "all"]).default("open"),
      strategy: z.enum(["topped_bid", "meridian", "chop_spot"]).optional(),
      limit: z.number().int().min(1).max(200).default(30),
    },
    run: (api, a) => api.get("/v1/bot/positions", { status: a.status, strategy: a.strategy, limit: a.limit }),
  },
];
