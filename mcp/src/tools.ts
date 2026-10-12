import { z } from "zod";
import type { Api } from "./api.js";
import type { Bots } from "./bots.js";

/**
 * Tools. None of them can build, sign or send a transaction, or move funds: the API client only issues GET requests
 * and no tool calls a transaction or wallet route (enforced by test/tools.test.ts). The one change a tool can make is
 * suggest_bot_settings (take profit / stop loss of the user's own bot, through bots.ts).
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
  write?: boolean;                      // changes something (not read-only)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  run: (api: Api, args: any, bots: Bots | null) => Promise<unknown>;
}

function signedIn(bots: Bots | null): Bots {
  if (!bots) throw new Error("connect with OAuth (your LP Dash account) to see and tune your bots");
  return bots;
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
    name: "screen_memecoins",
    title: "Memecoin screener",
    description:
      "Screen Solana memecoins from Jupiter's live trending, most traded, top organic and new lists. Each token has " +
      "a safety score and a momentum score (0-100, rankings of current data, not buy signals), risk flags (mint/freeze " +
      "authority, concentrated holders, thin liquidity, young, sell pressure, dumping...) and its best Meteora SOL " +
      "pool if any. `filters` takes mcap_min/max, liquidity_min, age_h_min/max, holders_min, top10_max, organic_min, " +
      "volume_1h_min, volume_24h_min, buy_ratio_1h_min, chg_5m/1h/24h_min/max (%), holder_chg_1h_min, safety_min, " +
      "momentum_min, safe_only (no red flags), meteora_only.",
    input: {
      preset: z.enum(["safe_runners", "dip_recovery", "established", "new_momentum", "lp_candidates"]).optional(),
      q: z.string().max(64).optional(),
      filters: z.record(z.union([z.number(), z.boolean()])).optional(),
      sort: z
        .enum(["momentum", "safety", "mcap", "liquidity", "volume_1h", "volume_24h", "chg_5m", "chg_1h", "chg_6h",
          "chg_24h", "holders", "holder_chg_1h", "age_h", "organic_score", "buy_ratio_1h", "traders_1h"])
        .optional(),
      order: z.enum(["asc", "desc"]).optional(),
      limit: z.number().int().min(1).max(50).default(20),
    },
    run: (api, a) =>
      api.get("/v1/memes", { ...(a.filters ?? {}), preset: a.preset, q: a.q, sort: a.sort, order: a.order, limit: a.limit }),
  },
  {
    name: "bot_status",
    title: "Paper bot status",
    description:
      "Paper LP bot (virtual $100 positions on live pool data, no wallet): heartbeat and, per strategy " +
      "(S1–S6), open/closed counts, win rate, realized and unrealized P&L, fees, costs " +
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
      strategy: z
        .enum([
          "v2_wide", "v2_calm", "v2_bid_flip", "auto", "pro_spot", "pro_spot_wide", "pro_bid",
          "topped_bid", "meridian", "chop_spot", "topped_bid_v2", "evil_panda", "grid", "fee_leader",
          "meridian_trail", "rabbit",
        ])
        .optional(),
      limit: z.number().int().min(1).max(200).default(30),
    },
    run: (api, a) => api.get("/v1/bot/positions", { status: a.status, strategy: a.strategy, limit: a.limit }),
  },
  {
    name: "analyze_pools",
    title: "Pool analyzer",
    description:
      "Which Meteora LP shape to open in each active pool, or skip (read-only, refreshed every 5 min). Per pool: " +
      "regime (sideways calm/volatile, rising, topped, falling), vetoes (token safety, real buy+sell-back cost, " +
      "pool price vs market, falling token), replays of 5 shapes (spot +/-10/25/40, SOL bids, bids with flip) on the " +
      "pool's last 6 h of real minute data with average, worst case, win rate and fees/h, and the choice. Past " +
      "replays are evidence, not a guarantee. Pass `pool` for one pool.",
    input: {
      pool: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/).optional(),
      limit: z.number().int().min(1).max(100).default(20),
    },
    run: (api, a) => (a.pool ? api.get(`/v1/analyze/${a.pool}`) : api.get("/v1/analyze", { limit: a.limit })),
  },
  {
    name: "bot_strategies",
    title: "Strategies a bot can follow",
    description:
      "The house LP strategies a user's bot can follow (paper or real money), with their last 7 days of closed " +
      "positions: count, win rate, average / median win / median loss / best / worst net %, P&L. Use it to propose " +
      "a take profit (e.g. near the median win) and a stop loss (beyond the usual losses, before the worst).",
    input: {},
    run: (api) => api.get("/v1/lpbots/strategies"),
  },
  {
    name: "my_bots",
    title: "My bots",
    description:
      "The signed-in user's LP bots: strategy followed, paper or real money, status, take profit / stop loss, daily " +
      "limits, results (closed, wins, P&L in SOL, today), the latest positions with their exits, the last reason a " +
      "trade was skipped, any pending suggestion, and for real-money bots the wallet address and balance.",
    input: {},
    run: async (_api, _a, bots) => {
      const d = (await signedIn(bots).get("/v1/lpbots")) as { bots?: { positions?: unknown[] }[] };
      return { bots: (d.bots ?? []).map((b) => ({ ...b, positions: (b.positions ?? []).slice(0, 10) })) };
    },
  },
  {
    name: "suggest_bot_settings",
    title: "Suggest take profit / stop loss",
    description:
      "Suggest a take profit and/or stop loss (net % of a position) for one of the user's bots, with a short reason. " +
      "A paper bot applies it at once; a real-money bot shows it on the dashboard and changes only when the user " +
      "clicks Apply. Base it on bot_strategies and my_bots, and say why. Cannot trade, fund, withdraw or close.",
    write: true,
    input: {
      bot_id: z.number().int().min(1),
      take_profit_pct: z.number().min(1).max(100).optional().describe("close a position at +this % net"),
      stop_loss_pct: z.number().min(1).max(50).optional().describe("close a position at -this % net"),
      reason: z.string().max(400).default(""),
    },
    run: async (_api, a, bots) =>
      signedIn(bots).suggest(a.bot_id, {
        take_profit_pct: a.take_profit_pct, stop_loss_pct: a.stop_loss_pct, reason: a.reason,
      }),
  },
];
