import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { TOOLS, type ToolDef } from "../src/tools.js";
import { queryString, type Api, type Query } from "../src/api.js";
import { bearerOk } from "../src/auth.js";

const POOL = "57JmG2uos4wGFtBuerGS2azHEPBBnp8FJRgeLmnx8uXV";

function recorder(): { api: Api; calls: { path: string; q?: Query }[] } {
  const calls: { path: string; q?: Query }[] = [];
  const api: Api = {
    get: async (path, q) => {
      calls.push({ path, q });
      return { pools: [], total: 0 };
    },
  };
  return { api, calls };
}

function tool(name: string): ToolDef {
  const t = TOOLS.find((x) => x.name === name);
  assert.ok(t, name);
  return t;
}

test("the spec's tools are all present", () => {
  assert.deepEqual(TOOLS.map((t) => t.name).sort(), [
    "backtest_results", "best_pools_now", "bot_positions", "bot_status", "exit_check", "get_pool", "get_pool_bins",
    "get_token", "pool_history", "pool_signal", "search_pools", "signal_stats",
  ]);
});

test("no tool can reach a transaction or wallet route", async () => {
  const { api, calls } = recorder();
  const sample = {
    address: POOL, mint: POOL, range_low: 1, range_high: 2, entry_time: 1_700_000_000, amount_usd: 100,
    strategy: "any", limit: 5, kind: "ohlcv", timeframe: "5m", hours: 24, live: false, each_side: 20,
  };
  for (const t of TOOLS) await t.run(api, sample);
  for (const kind of ["fee_tvl", "flow"]) await tool("pool_history").run(api, { ...sample, kind });
  assert.ok(calls.length >= 9);
  for (const c of calls) assert.doesNotMatch(c.path, /\/v1\/(tx|wallet)\b/);
  // and the sources never mention those routes or a non-GET method
  for (const f of ["src/tools.ts", "src/api.ts", "src/server.ts"]) {
    const src = readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
    assert.doesNotMatch(src, /\/v1\/tx|\/v1\/wallet|method:\s*"(POST|PUT|DELETE|PATCH)"/);
  }
});

test("search_pools passes filters and trims rows", async () => {
  const calls: Query[] = [];
  const api: Api = {
    get: async (_p, q) => {
      calls.push(q ?? {});
      return { total: 1, data: [{ address: POOL, tvl: 5, bins: [1, 2], name: "A-SOL", token_x: "x" }] };
    },
  };
  const out = (await tool("search_pools").run(api, {
    q: "bonk", limit: 5, filters: { tvl_min: 10_000, sol_pair: true },
  })) as { pools: Record<string, unknown>[] };
  assert.equal(calls[0].tvl_min, 10_000);
  assert.equal(calls[0].sol_pair, true);
  assert.equal(calls[0].q, "bonk");
  assert.deepEqual(out.pools[0], { address: POOL, name: "A-SOL", tvl: 5 });
});

test("exit_check accepts ISO dates and pool_history caps ohlcv at 72 h", async () => {
  const { api, calls } = recorder();
  await tool("exit_check").run(api, { address: POOL, range_low: 1, range_high: 2, entry_time: "2026-10-10T08:00:00Z" });
  assert.equal(calls[0].q?.entry_time, Date.parse("2026-10-10T08:00:00Z") / 1000);
  await assert.rejects(tool("exit_check").run(api, { address: POOL, range_low: 1, range_high: 2, entry_time: "x" }));
  await tool("pool_history").run(api, { address: POOL, kind: "ohlcv", timeframe: "1h", hours: 168 });
  assert.equal(calls[1].q?.hours, 72);
});

test("query strings skip empty values", () => {
  assert.equal(queryString({ a: 1, b: undefined, c: "", d: false }), "?a=1&d=false");
  assert.equal(queryString({}), "");
});

test("bearer auth", () => {
  const tok = "x".repeat(32);
  assert.ok(bearerOk(`Bearer ${tok}`, tok));
  assert.ok(!bearerOk(`Bearer ${tok}y`, tok));
  assert.ok(!bearerOk(undefined, tok));
  assert.ok(!bearerOk(`Basic ${tok}`, tok));
  assert.ok(!bearerOk("Bearer ", ""));
});
