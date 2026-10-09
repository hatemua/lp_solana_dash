"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, Bin, PoolRow, SOL_MINT } from "@/lib/api";
import { age, num, pct, plainPct, price as fmtPrice, shortAddr, usd } from "@/lib/format";
import { BinsChart, Candle, CandleChart, FeeTvlChart } from "./Charts";
import { AddLiquidity } from "./AddLiquidity";

interface Token {
  mint: string; symbol: string | null; price_usd: number | null; decimals: number | null;
  audit?: Record<string, unknown> | null;
}
interface PoolDetail extends PoolRow {
  tokens: Token[];
  signal: { ts: string; lp_score: number | null; metrics: Record<string, unknown> } | null;
}
interface Bins { source: string; active_bin_id: number; bin_step: number; active_price: number | null; bins: Bin[] }

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="card px-3 py-2">
      <div className="label">{label}</div>
      <div className={`text-base font-medium num ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

function Flag({ ok, label }: { ok: boolean | null; label: string }) {
  return (
    <div className="flex items-center justify-between border-b border-line/60 py-1.5 text-sm">
      <span className="text-mut">{label}</span>
      <span className={ok === null ? "text-mut" : ok ? "text-pos" : "text-neg"}>{ok === null ? "unknown" : ok ? "OK" : "risk"}</span>
    </div>
  );
}

export function PoolView({ address }: { address: string }) {
  const [pool, setPool] = useState<PoolDetail | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [tf, setTf] = useState<"5m" | "1h">("5m");
  const [bins, setBins] = useState<Bins | null>(null);
  const [feeTvl, setFeeTvl] = useState<{ ts: string; fee_tvl_1h: number | null; tvl: number | null }[]>([]);
  const [range, setRange] = useState<{ min: number; max: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, c, f] = await Promise.all([
        api<PoolDetail>(`/v1/pools/${address}`),
        api<{ data: Candle[] }>(`/v1/pools/${address}/ohlcv?timeframe=${tf}&hours=${tf === "5m" ? 24 : 72}`),
        api<{ data: typeof feeTvl }>(`/v1/pools/${address}/fee-tvl?hours=48`),
      ]);
      setPool(p);
      setCandles(c.data);
      setFeeTvl(f.data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    try {
      setBins(await api<Bins>(`/v1/pools/${address}/bins?each_side=60`));
    } catch {
      /* bins are optional (executor / RPC) */
    }
  }, [address, tf]);

  useEffect(() => {
    void load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  if (error && !pool) return <div className="card p-4 text-neg">Could not load this pool: {error}</div>;
  if (!pool) return <div className="p-4 text-mut">Loading…</div>;

  const tx = pool.tokens.find((t) => t.mint === pool.token_x);
  const ty = pool.tokens.find((t) => t.mint === pool.token_y);
  const tokenIsX = pool.token_y === SOL_MINT;
  const audit = pool.tokens.find((t) => t.mint === pool.token_mint)?.audit ?? {};

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <Link href="/" className="text-sm text-mut hover:text-slate-100">← Pools</Link>
        <h1 className="text-xl font-semibold">{pool.name}</h1>
        <span className="text-xs text-mut">{shortAddr(address, 6)}</span>
        <a className="text-xs text-acc" href={`https://app.meteora.ag/dlmm/${address}`} target="_blank" rel="noreferrer">Meteora ↗</a>
        <span className="ml-auto text-xs text-mut">price {fmtPrice(pool.price)} · bin step {pool.bin_step} · base fee {plainPct(pool.base_fee_pct)}</span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="TVL" value={usd(pool.tvl)} />
        <Stat label="Volume 1h / 24h" value={`${usd(pool.volume_1h)} / ${usd(pool.volume_24h)}`} />
        <Stat label="Fees 1h / 24h" value={`${usd(pool.fees_1h)} / ${usd(pool.fees_24h)}`} />
        <Stat label="Fee/TVL 1h / 24h" value={`${plainPct(pool.fee_tvl_1h, 2, true)} / ${plainPct(pool.fee_tvl_24h, 1, true)}`} />
        <Stat label="Dynamic fee" value={plainPct(pool.dynamic_fee_pct, 3)} />
        <Stat label="Token vol 5m (all venues)" value={usd(pool.token_volume_5m)} />
        <Stat label="Price 5m / 1h" value={`${pct(pool.price_change_5m)} / ${pct(pool.price_change_1h)}`}
          tone={(pool.price_change_1h ?? 0) >= 0 ? "text-pos" : "text-neg"} />
        <Stat label="Pool / token age" value={`${age(pool.pool_age_h)} / ${age(pool.token_age_h)}`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_340px]">
        <div className="space-y-4">
          <div className="card p-2">
            <div className="flex items-center gap-2 px-2 pb-2 text-sm">
              <span className="font-medium">Price, volume and LP fees</span>
              <div className="ml-auto flex gap-1">
                {(["5m", "1h"] as const).map((x) => (
                  <button key={x} className={`btn px-2 py-0.5 ${tf === x ? "border-acc text-acc" : ""}`} onClick={() => setTf(x)}>{x}</button>
                ))}
              </div>
            </div>
            {candles.length ? <CandleChart data={candles} /> : <p className="p-4 text-sm text-mut">No candles yet (backfill pending).</p>}
          </div>
          <div className="card p-2">
            <div className="px-2 pb-1 text-sm font-medium">
              Liquidity by bin <span className="text-xs font-normal text-mut">({bins?.source ?? "–"}, active bin {bins?.active_bin_id ?? "–"})</span>
            </div>
            {bins ? (
              <BinsChart bins={bins.bins} activeBin={bins.active_bin_id} usdX={tx?.price_usd ?? null}
                usdY={ty?.price_usd ?? null} range={range} />
            ) : <p className="p-4 text-sm text-mut">Bins unavailable right now.</p>}
          </div>
          <div className="card p-2">
            <div className="px-2 pb-1 text-sm font-medium">Fee/TVL history (48 h)</div>
            {feeTvl.length ? <FeeTvlChart data={feeTvl} /> : <p className="p-4 text-sm text-mut">No history yet.</p>}
          </div>
        </div>

        <div className="space-y-4">
          <div className="card p-3">
            <div className="mb-2 text-sm font-medium">Signal</div>
            {pool.signal ? (
              <div className="space-y-1 text-sm">
                <div className="text-2xl font-semibold num">{num(pool.signal.lp_score)}</div>
                <pre className="max-h-48 overflow-auto text-xs text-mut">{JSON.stringify(pool.signal.metrics, null, 1)}</pre>
              </div>
            ) : (
              <p className="text-sm text-mut">LP score, entry/exit status, suggested range and shape arrive with the
                signal engine (milestone M4).</p>
            )}
          </div>

          <div className="card p-3">
            <div className="mb-1 text-sm font-medium">Token safety · {pool.token_symbol ?? "–"}</div>
            <Flag ok={pool.mint_disabled} label="Mint authority off" />
            <Flag ok={pool.freeze_disabled} label="Freeze authority off" />
            <Flag ok={pool.top10_pct === null ? null : pool.top10_pct <= 30} label={`Top-10 holders ${plainPct(pool.top10_pct, 1)}`} />
            <Flag ok={pool.dev_pct === null ? null : pool.dev_pct <= 5} label={`Dev holdings ${plainPct(pool.dev_pct, 2)}`} />
            <Flag ok={(audit as { isSus?: boolean }).isSus === undefined ? null : !(audit as { isSus?: boolean }).isSus} label="Not flagged suspicious" />
            <div className="mt-2 grid grid-cols-2 gap-1 text-xs text-mut num">
              <span>Mcap {usd(pool.mcap)}</span><span>Holders {num(pool.holders)}</span>
              <span>Organic score {num(pool.organic_score)}</span><span>Launchpad {pool.token_launchpad ?? "–"}</span>
            </div>
          </div>

          {bins && pool.bin_step && (
            <AddLiquidity pool={address} activeBin={bins.active_bin_id} binStep={pool.bin_step} price={pool.price ?? 0}
              tokenIsX={tokenIsX} tokenSymbol={pool.token_symbol ?? "token"} decimalsX={tx?.decimals ?? 9}
              onRange={setRange} />
          )}
        </div>
      </div>
    </div>
  );
}
