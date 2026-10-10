"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { pct, plainPct, price, usd } from "@/lib/format";

interface StrategyStats {
  strategy: string;
  label: string;
  open: number;
  closed: number;
  win_rate: number | null;
  realized_usd: number;
  unrealized_usd: number;
  realized_24h_usd: number;
  fees_usd: number;
  costs_usd: number;
  avg_net_pct: number | null;
  since: string | null;
  equity: { ts: string; usd: number }[];
}

interface Status {
  mode: string;
  position_usd: number;
  max_open_per_strategy: number;
  alive: boolean;
  heartbeat: { ts: number; ok?: boolean; error?: string; took_s?: number } | null;
  strategies: StrategyStats[];
  note: string;
}

interface BotPosition {
  id: number;
  strategy: string;
  pool: string;
  name: string | null;
  status: "open" | "closed";
  opened_at: string;
  closed_at: string | null;
  entry_price: number;
  last_price: number | null;
  net_pct: number | null;
  pnl_usd: number | null;
  fees_usd: number | null;
  costs_usd: number | null;
  flipped: boolean;
  exit_reason: string | null;
  range_low: number | null;
  range_high: number | null;
  legs: string | null;
}

const SHORT: Record<string, string> = {
  topped_bid: "S1", meridian: "S2", chop_spot: "S3", topped_bid_v2: "S1b", evil_panda: "S4", grid: "S5",
  fee_leader: "S6", meridian_trail: "S2b", rabbit: "S7",
};

function since(ts: string, until?: string | null): string {
  const ms = (until ? new Date(until).getTime() : Date.now()) - new Date(ts).getTime();
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
}

function color(v: number | null | undefined): string {
  return v === null || v === undefined || v === 0 ? "" : v > 0 ? "text-pos" : "text-neg";
}

function Equity({ points }: { points: { ts: string; usd: number }[] }) {
  if (points.length < 2) return <div className="h-12 text-xs text-mut">equity curve after 2 closed positions</div>;
  const ys = [0, ...points.map((p) => p.usd)];
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const span = hi - lo || 1;
  const w = 240;
  const h = 48;
  const d = ys.map((y, i) => `${i ? "L" : "M"}${(i / (ys.length - 1)) * w},${h - ((y - lo) / span) * h}`).join(" ");
  const zero = h - ((0 - lo) / span) * h;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-12 w-full" preserveAspectRatio="none" aria-label="equity curve">
      <line x1="0" x2={w} y1={zero} y2={zero} stroke="#232b3a" strokeDasharray="3 3" />
      <path d={d} fill="none" stroke={ys[ys.length - 1] >= 0 ? "#3ecf8e" : "#f2645a"} strokeWidth="1.5" />
    </svg>
  );
}

export function BotView() {
  const [status, setStatus] = useState<Status | null>(null);
  const [positions, setPositions] = useState<BotPosition[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        api<Status>("/v1/bot/status"),
        api<{ positions: BotPosition[] }>(`/v1/bot/positions?limit=200${filter ? `&strategy=${filter}` : ""}`),
      ]);
      setStatus(s);
      setPositions(p.positions);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [filter]);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 30_000);
    return () => clearInterval(id);
  }, [load]);

  const open = positions.filter((p) => p.status === "open");
  const closed = positions.filter((p) => p.status === "closed");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Paper LP bot</h1>
        <span className="rounded border border-line px-2 py-0.5 text-xs text-mut">PAPER · no wallet · no real orders</span>
        {status && (
          <span className={`text-xs ${status.alive ? "text-pos" : "text-neg"}`}>
            {status.alive ? "● running" : "● stopped"}
            {status.heartbeat?.ts ? ` · last tick ${Math.round(Date.now() / 1000 - status.heartbeat.ts)}s ago` : ""}
            {status.heartbeat?.error ? ` · ${status.heartbeat.error}` : ""}
          </span>
        )}
      </div>
      {error && <p className="text-sm text-neg">{error}</p>}
      <p className="text-sm text-mut">
        The strategies run side by side on live Meteora data, ${status?.position_usd ?? 100} per position, up to{" "}
        {status?.max_open_per_strategy ?? 3} open each. Fees come from each pool&apos;s real per-minute fees and our share
        of the bin liquidity. Rules: <Link className="text-acc hover:underline"
          href="https://github.com/hatemua/lp_solana_dash/blob/m3-bot/docs/research/strategy-playbook.md">playbook</Link>.
      </p>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {status?.strategies.map((s) => (
          <button
            key={s.strategy}
            onClick={() => setFilter(filter === s.strategy ? "" : s.strategy)}
            className={`card p-4 text-left ${filter === s.strategy ? "border-acc" : ""}`}
          >
            <div className="mb-2 flex items-baseline gap-2">
              <span className="font-semibold">{s.label}</span>
              <span className="ml-auto text-xs text-mut">{s.open} open</span>
            </div>
            <div className={`text-2xl font-semibold ${color(s.realized_usd + s.unrealized_usd)}`}>
              {usd(s.realized_usd + s.unrealized_usd, 2)}
            </div>
            <div className="mb-2 text-xs text-mut">
              total · closed <span className={color(s.realized_usd)}>{usd(s.realized_usd, 2)}</span> · open ({s.open}){" "}
              <span className={color(s.unrealized_usd)}>{usd(s.unrealized_usd, 2)}</span>
            </div>
            <Equity points={s.equity} />
            <div className="mt-2 grid grid-cols-2 gap-x-3 text-xs">
              <span className="text-mut">closed</span><span>{s.closed}</span>
              <span className="text-mut">win rate</span><span>{s.win_rate === null ? "–" : plainPct(s.win_rate, 0, true)}</span>
              <span className="text-mut">avg / position</span><span className={color(s.avg_net_pct)}>{pct(s.avg_net_pct, 2)}</span>
              <span className="text-mut">fees / costs</span><span>{usd(s.fees_usd, 2)} / {usd(s.costs_usd, 2)}</span>
            </div>
          </button>
        ))}
      </div>

      <PositionsTable title={`Open (${open.length})`} rows={open} />
      <PositionsTable title={`Closed (${closed.length})`} rows={closed} />
    </div>
  );
}

function PositionsTable({ title, rows }: { title: string; rows: BotPosition[] }) {
  return (
    <section className="card overflow-x-auto p-3">
      <h2 className="mb-2 font-semibold">{title}</h2>
      {rows.length === 0 ? (
        <p className="text-sm text-mut">None.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-mut">
            <tr>
              <th className="py-1 pr-3">Strat</th>
              <th className="pr-3">Pool</th>
              <th className="pr-3">Held</th>
              <th className="pr-3">Range</th>
              <th className="pr-3">Entry → now</th>
              <th className="pr-3 text-right">Fees</th>
              <th className="pr-3 text-right">Net</th>
              <th className="pr-3 text-right">P&amp;L</th>
              <th>Exit</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id} className="border-t border-line">
                <td className="py-1 pr-3 text-mut">{SHORT[p.strategy] ?? p.strategy}</td>
                <td className="pr-3">
                  <Link href={`/pools/${p.pool}`} className="hover:text-acc">{p.name ?? p.pool.slice(0, 6)}</Link>
                  {p.flipped && <span className="ml-1 rounded bg-acc/20 px-1 text-xs text-acc">flip</span>}
                </td>
                <td className="pr-3 text-mut">{since(p.opened_at, p.closed_at)}</td>
                <td className="pr-3 font-mono text-xs">
                  {p.range_low ? `${pct((p.range_low / p.entry_price - 1) * 100, 0)}…${pct(((p.range_high ?? 0) / p.entry_price - 1) * 100, 0)}` : "–"}
                  <span className="ml-1 text-mut">{p.legs}</span>
                </td>
                <td className="pr-3 font-mono text-xs">
                  {price(p.entry_price)} → {price(p.last_price)}{" "}
                  <span className={color((p.last_price ?? 0) - p.entry_price)}>
                    {pct(((p.last_price ?? p.entry_price) / p.entry_price - 1) * 100, 1)}
                  </span>
                </td>
                <td className="pr-3 text-right">{usd(p.fees_usd, 2)}</td>
                <td className={`pr-3 text-right ${color(p.net_pct)}`}>{pct(p.net_pct, 2)}</td>
                <td className={`pr-3 text-right ${color(p.pnl_usd)}`}>{usd(p.pnl_usd, 2)}</td>
                <td className="text-xs text-mut">{p.exit_reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
