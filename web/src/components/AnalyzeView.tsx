"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { age, pct, usd } from "@/lib/format";

interface ShapeStats {
  n: number;
  mean_pct?: number;
  median_pct?: number;
  win_rate?: number;
  worst_pct?: number;
  best_pct?: number;
  lower_pct?: number;
  fees_pct?: number;
  fees_pct_h?: number;
  hold_min?: number;
  exits?: Record<string, number>;
}

interface ShapeResult {
  key: string;
  label: string;
  fits: boolean;
  stats: ShapeStats;
}

interface Choice {
  shape: string;
  label: string;
  expected_pct: number;
  lower_pct: number;
  win_rate: number;
  confidence: "high" | "medium" | "low";
}

interface Analysis {
  pool: string;
  name: string;
  mint: string;
  at: number;
  tvl: number;
  fees_1h: number;
  fee_tvl_1h: number;
  bin_step: number;
  age_h: number | null;
  mcap: number;
  change_1h_pct: number | null;
  vol_5m_pct: number;
  regime: string;
  regime_text: string;
  round_trip_pct: number | null;
  price_gap_pct: number | null;
  buy_5m: number | null;
  sell_5m: number | null;
  chg_5m_pct: number | null;
  vetoes: string[];
  shapes: ShapeResult[];
  choice: Choice | null;
  why_not: string[];
}

const REGIME: Record<string, { label: string; cls: string }> = {
  sideways_calm: { label: "sideways · calm", cls: "bg-acc/15 text-acc" },
  sideways_volatile: { label: "sideways · volatile", cls: "bg-acc/15 text-acc" },
  rising: { label: "rising", cls: "bg-pos/15 text-pos" },
  topped: { label: "topped after pump", cls: "bg-yellow-500/15 text-yellow-400" },
  falling: { label: "falling / sellers", cls: "bg-neg/15 text-neg" },
};

function color(v: number | null | undefined): string {
  return v === null || v === undefined || v === 0 ? "" : v > 0 ? "text-pos" : "text-neg";
}

function ago(ts: number): string {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 1 ? "just now" : `${m} min ago`;
}

export function AnalyzeView() {
  const [pools, setPools] = useState<Analysis[] | null>(null);
  const [onlyEnter, setOnlyEnter] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ pools: Analysis[] }>("/v1/analyze?limit=100");
      setPools(r.pools);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load]);

  const shown = (pools ?? []).filter((p) => !onlyEnter || p.choice);
  const enter = (pools ?? []).filter((p) => p.choice).length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Pool analyzer</h1>
        <span className="rounded border border-line px-2 py-0.5 text-xs text-mut">updated every 5 min · $100 per position</span>
        {pools && (
          <span className="text-xs text-mut">
            {pools.length} pools analyzed · <span className={enter ? "text-pos" : ""}>{enter} worth entering</span>
          </span>
        )}
        <label className="ml-auto flex items-center gap-2 text-sm text-mut">
          <input type="checkbox" checked={onlyEnter} onChange={(e) => setOnlyEnter(e.target.checked)} />
          only pools worth entering
        </label>
      </div>
      {error && <p className="text-sm text-neg">{error}</p>}
      <details className="card p-3 text-sm text-mut">
        <summary className="cursor-pointer text-slate-200">How it decides</summary>
        <ol className="mt-2 list-decimal space-y-1 pl-5">
          <li>
            <b>Vetoes.</b> Token safety (mint/freeze off, top 10 ≤ 30%, ≥ 500 holders, mcap ≥ $250k, ≥ 24 h old), the
            real cost to buy and sell the token back (Jupiter quotes, ≤ 3%), the pool price vs the market, and a falling
            token or sellers ahead. Any veto = skip: every LP shape ends up holding a falling token.
          </li>
          <li>
            <b>Regime</b> from the pool&apos;s own 5-min bars: sideways calm, sideways volatile, rising, topped after a
            pump. Only the shapes that fit it can be chosen.
          </li>
          <li>
            <b>Replays.</b> Each shape is opened every 30 min over the pool&apos;s last 6 hours of real per-minute data
            (price, fees, TVL) and run with the bot&apos;s own engine and exit rules, swap costs included. Other LPs&apos;
            liquidity is today&apos;s.
          </li>
          <li>
            <b>Choice.</b> The shape with the best worst case (average − uncertainty), only if its average is at least
            +1% after costs, its worst case is above 0 and it won most replays. Otherwise skip. The past 6 hours don&apos;t
            guarantee the next ones: the <Link href="/bot" className="text-acc hover:underline">Auto bot</Link> trades
            these choices on paper to measure them.
          </li>
        </ol>
      </details>

      {pools === null ? (
        <p className="text-sm text-mut">Loading…</p>
      ) : shown.length === 0 ? (
        <p className="card p-4 text-sm text-mut">
          {pools.length === 0
            ? "No analysis yet: the analyzer covers pools earning at least $300/h in fees and runs every 5 minutes."
            : "No pool is worth entering right now. That's the analyzer doing its job: it skips instead of losing."}
        </p>
      ) : (
        <div className="grid gap-3 xl:grid-cols-2">
          {shown.map((p) => <PoolCard key={p.pool} a={p} />)}
        </div>
      )}
    </div>
  );
}

function PoolCard({ a }: { a: Analysis }) {
  const reg = REGIME[a.regime] ?? { label: a.regime, cls: "" };
  const flow = a.buy_5m !== null && a.sell_5m !== null && a.buy_5m + a.sell_5m > 0
    ? a.buy_5m / (a.buy_5m + a.sell_5m) : null;
  return (
    <section className={`card p-4 ${a.choice ? "border-pos/60" : ""}`}>
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <Link href={`/pools/${a.pool}`} className="font-semibold hover:text-acc">{a.name}</Link>
        <span className={`rounded px-1.5 py-0.5 text-xs ${reg.cls}`}>{reg.label}</span>
        <span className="ml-auto text-xs text-mut">{ago(a.at)}</span>
      </div>

      <div className="mb-3 grid grid-cols-3 gap-x-3 gap-y-1 text-xs sm:grid-cols-6">
        <Fact k="fee/TVL" v={`${a.fee_tvl_1h.toFixed(2)}%/h`} />
        <Fact k="fees 1h" v={usd(a.fees_1h)} />
        <Fact k="TVL" v={usd(a.tvl)} />
        <Fact k="1h move" v={pct(a.change_1h_pct, 1)} cls={color(a.change_1h_pct)} />
        <Fact k="round trip" v={a.round_trip_pct === null ? "–" : `${a.round_trip_pct.toFixed(2)}%`}
          cls={a.round_trip_pct !== null && a.round_trip_pct > 3 ? "text-neg" : ""} />
        <Fact k="buyers 5m" v={flow === null ? "–" : `${Math.round(flow * 100)}%`}
          cls={flow === null ? "" : flow >= 0.5 ? "text-pos" : "text-neg"} />
        <Fact k="token age" v={age(a.age_h)} />
        <Fact k="mcap" v={usd(a.mcap)} />
        <Fact k="bin step" v={String(a.bin_step)} />
        <Fact k="volatility" v={`${a.vol_5m_pct.toFixed(1)}%/5m`} />
        <Fact k="price vs mkt" v={pct(a.price_gap_pct, 1)} />
        <Fact k="5m move" v={pct(a.chg_5m_pct, 1)} cls={color(a.chg_5m_pct)} />
      </div>

      {a.choice ? (
        <div className="mb-3 rounded border border-pos/40 bg-pos/10 p-3 text-sm">
          <div className="font-semibold text-pos">ENTER · {a.choice.label}</div>
          <div className="text-xs">
            expected {pct(a.choice.expected_pct, 2)} (≈ {usd(a.choice.expected_pct, 2)} per $100), worst case{" "}
            {pct(a.choice.lower_pct, 2)}, won {Math.round(a.choice.win_rate * 100)}% of replays · confidence{" "}
            <b>{a.choice.confidence}</b>
          </div>
          <div className="mt-1 text-xs text-mut">{a.regime_text}</div>
        </div>
      ) : (
        <div className="mb-3 rounded border border-line p-3 text-sm">
          <div className="font-semibold text-mut">SKIP</div>
          <ul className="list-disc pl-5 text-xs text-mut">
            {(a.vetoes.length ? a.vetoes : a.why_not).slice(0, 5).map((w) => <li key={w}>{w}</li>)}
            {!a.vetoes.length && !a.why_not.length && <li>no shape fits this regime</li>}
          </ul>
        </div>
      )}

      {a.shapes.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-left text-mut">
            <tr>
              <th className="py-1 pr-2">Shape (6 h replays)</th>
              <th className="pr-2 text-right">avg</th>
              <th className="pr-2 text-right">worst case</th>
              <th className="pr-2 text-right">won</th>
              <th className="pr-2 text-right">fees/h</th>
              <th className="text-right">hold</th>
            </tr>
          </thead>
          <tbody>
            {a.shapes.map((s) => (
              <tr key={s.key} className={`border-t border-line ${s.fits ? "" : "opacity-50"}`}
                title={s.stats.exits ? Object.entries(s.stats.exits).map(([k, v]) => `${k} ${v}`).join(", ") : ""}>
                <td className="py-1 pr-2">
                  {a.choice?.shape === s.key && <span className="mr-1 text-pos">▶</span>}
                  {s.label}{!s.fits && <span className="ml-1 text-mut">(doesn&apos;t fit)</span>}
                </td>
                <td className={`pr-2 text-right ${color(s.stats.mean_pct)}`}>{pct(s.stats.mean_pct, 2)}</td>
                <td className={`pr-2 text-right ${color(s.stats.lower_pct)}`}>{pct(s.stats.lower_pct, 2)}</td>
                <td className="pr-2 text-right">{s.stats.win_rate === undefined ? "–" : `${Math.round(s.stats.win_rate * 100)}%`}</td>
                <td className="pr-2 text-right">{s.stats.fees_pct_h === undefined ? "–" : usd(s.stats.fees_pct_h, 2)}</td>
                <td className="text-right text-mut">{s.stats.hold_min === undefined ? "–" : `${Math.round(s.stats.hold_min)}m`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Fact({ k, v, cls = "" }: { k: string; v: string; cls?: string }) {
  return (
    <div>
      <div className="text-mut">{k}</div>
      <div className={cls}>{v}</div>
    </div>
  );
}
