"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, API_URL, PoolRow, PoolsResponse, Preset, qs } from "@/lib/api";
import { age, num, pct, plainPct, usd } from "@/lib/format";

type Filters = Record<string, string>;

// Filter inputs shown in the panel: [param, label, unit hint]
const FILTER_FIELDS: [string, string, string][] = [
  ["tvl_min", "TVL ≥", "$"],
  ["volume_1h_min", "Volume 1h ≥", "$"],
  ["volume_24h_min", "Volume 24h ≥", "$"],
  ["fees_1h_min", "Fees 1h ≥", "$"],
  ["fee_tvl_24h_min", "Fee/TVL 24h ≥", "ratio, 0.05 = 5%"],
  ["token_volume_5m_min", "Token vol 5m ≥", "$, all venues"],
  ["volume_burst_min", "Volume burst ≥", "× (1h vs 24h avg)"],
  ["bin_step_min", "Bin step ≥", "bps"],
  ["base_fee_pct_min", "Base fee ≥", "%"],
  ["token_age_h_min", "Token age ≥", "hours"],
  ["pool_age_h_max", "Pool age ≤", "hours"],
  ["mcap_min", "Mcap ≥", "$"],
  ["holders_min", "Holders ≥", ""],
  ["organic_score_min", "Organic score ≥", "0–100"],
  ["top10_pct_max", "Top-10 holders ≤", "%"],
  ["dev_pct_max", "Dev holdings ≤", "%"],
  ["price_change_1h_min", "Price 1h ≥", "%"],
  ["price_change_1h_max", "Price 1h ≤", "%"],
  ["lp_score_min", "LP score ≥", "0–100 (M4)"],
];

// Columns: [key, label, render, sortable]
const COLUMNS: [keyof PoolRow, string, (r: PoolRow) => string, boolean][] = [
  ["tvl", "TVL", (r) => usd(r.tvl), true],
  ["volume_5m", "Vol 5m", (r) => usd(r.volume_5m), true],
  ["volume_1h", "Vol 1h", (r) => usd(r.volume_1h), true],
  ["volume_24h", "Vol 24h", (r) => usd(r.volume_24h), true],
  ["fees_1h", "Fees 1h", (r) => usd(r.fees_1h), true],
  ["fees_24h", "Fees 24h", (r) => usd(r.fees_24h), true],
  ["fee_tvl_1h", "Fee/TVL 1h", (r) => plainPct(r.fee_tvl_1h, 2, true), true],
  ["fee_tvl_24h", "Fee/TVL 24h", (r) => plainPct(r.fee_tvl_24h, 1, true), true],
  ["dynamic_fee_pct", "Dyn fee", (r) => plainPct(r.dynamic_fee_pct, 2), true],
  ["bin_step", "Bin", (r) => num(r.bin_step), true],
  ["base_fee_pct", "Base fee", (r) => plainPct(r.base_fee_pct, 2), true],
  ["token_volume_5m", "Token vol 5m", (r) => usd(r.token_volume_5m), true],
  ["volume_burst", "Burst", (r) => (r.volume_burst ? `${r.volume_burst.toFixed(1)}×` : "–"), true],
  ["price_change_5m", "5m", (r) => pct(r.price_change_5m), true],
  ["price_change_1h", "1h", (r) => pct(r.price_change_1h), true],
  ["price_change_24h", "24h", (r) => pct(r.price_change_24h), true],
  ["pool_age_h", "Pool age", (r) => age(r.pool_age_h), true],
  ["token_age_h", "Token age", (r) => age(r.token_age_h), true],
  ["mcap", "Mcap", (r) => usd(r.mcap), true],
  ["holders", "Holders", (r) => num(r.holders), true],
  ["organic_score", "Organic", (r) => num(r.organic_score), true],
  ["lp_score", "LP score", (r) => num(r.lp_score), true],
];

function tone(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "";
  return v > 0 ? "text-pos" : v < 0 ? "text-neg" : "";
}

function Safety({ r }: { r: PoolRow }) {
  const flags: [boolean | null, string][] = [
    [r.mint_disabled, "mint"],
    [r.freeze_disabled, "freeze"],
    [r.top10_pct === null ? null : r.top10_pct <= 30, `top10 ${r.top10_pct?.toFixed(0) ?? "?"}%`],
  ];
  return (
    <span className="flex gap-1">
      {flags.map(([ok, label]) => (
        <span key={label} title={label}
          className={`rounded px-1 text-[10px] ${ok === null ? "bg-line text-mut" : ok ? "bg-pos/15 text-pos" : "bg-neg/15 text-neg"}`}>
          {label}
        </span>
      ))}
    </span>
  );
}

export function PoolsTable() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [preset, setPreset] = useState<string>("");
  const [filters, setFilters] = useState<Filters>({ sol_pair: "true" });
  const [sort, setSort] = useState<string>("fee_tvl_1h");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [q, setQ] = useState("");
  const [data, setData] = useState<PoolsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [page, setPage] = useState(0);
  const pageSize = 100;

  useEffect(() => {
    api<{ presets: Preset[] }>("/v1/presets").then((r) => setPresets(r.presets)).catch(() => undefined);
  }, []);

  const params = useMemo(() => ({ ...filters, preset, sort, order, q, limit: pageSize, offset: page * pageSize }),
    [filters, preset, sort, order, q, page]);
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const load = useCallback(async () => {
    try {
      const r = await api<PoolsResponse>(`/v1/pools${qs(paramsRef.current)}`);
      setData(r);
      setError(null);
      setUpdated(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [params, load]);

  // live: refresh when the indexer publishes new hot-pool stats (SSE), and every 30 s as a fallback
  useEffect(() => {
    let es: EventSource | null = null;
    let last = 0;
    try {
      es = new EventSource(`${API_URL}/v1/stream`);
      es.addEventListener("hot", () => {
        if (Date.now() - last > 20_000) {
          last = Date.now();
          void load();
        }
      });
    } catch {
      es = null;
    }
    const t = setInterval(() => {
      if (Date.now() - last > 30_000) {
        last = Date.now();
        void load();
      }
    }, 30_000);
    return () => {
      clearInterval(t);
      es?.close();
    };
  }, [load]);

  const choosePreset = (id: string) => {
    setPage(0);
    setPreset(id === preset ? "" : id);
    const p = presets.find((x) => x.id === id);
    if (p && id !== preset) setSort(p.sort);
  };

  const clickSort = (key: string) => {
    setPage(0);
    if (sort === key) setOrder(order === "desc" ? "asc" : "desc");
    else {
      setSort(key);
      setOrder("desc");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-2 text-lg font-semibold">Meteora DLMM pools</h1>
        {presets.map((p) => (
          <button key={p.id} title={p.description} onClick={() => choosePreset(p.id)}
            className={`btn ${preset === p.id ? "border-acc text-acc" : ""}`}>
            {p.label}
          </button>
        ))}
        <button className="btn" onClick={() => setShowFilters(!showFilters)}>Filters {showFilters ? "▴" : "▾"}</button>
        <input className="input max-w-[220px]" placeholder="Search token or pool address" value={q}
          onChange={(e) => { setQ(e.target.value); setPage(0); }} />
        <label className="flex items-center gap-1 text-sm text-mut">
          <input type="checkbox" checked={filters.sol_pair === "true"}
            onChange={(e) => setFilters({ ...filters, sol_pair: e.target.checked ? "true" : "" })} /> SOL pairs
        </label>
        <span className="ml-auto text-xs text-mut num">
          {data ? `${data.total} pools · ${data.took_ms} ms` : "loading…"}
          {updated ? ` · updated ${updated.toLocaleTimeString()}` : ""}
        </span>
      </div>
      {preset && <p className="text-xs text-mut">{presets.find((p) => p.id === preset)?.description}</p>}

      {showFilters && (
        <div className="card grid grid-cols-2 gap-3 p-3 sm:grid-cols-4 lg:grid-cols-7">
          {FILTER_FIELDS.map(([key, label, hint]) => (
            <label key={key} className="space-y-1">
              <span className="label">{label} <span className="opacity-60">{hint}</span></span>
              <input className="input num" inputMode="decimal" value={filters[key] ?? ""}
                onChange={(e) => { setFilters({ ...filters, [key]: e.target.value }); setPage(0); }} />
            </label>
          ))}
          <div className="flex items-end gap-2">
            <label className="flex items-center gap-1 text-xs text-mut">
              <input type="checkbox" checked={filters.mint_disabled === "true"}
                onChange={(e) => setFilters({ ...filters, mint_disabled: e.target.checked ? "true" : "" })} />
              mint off
            </label>
            <label className="flex items-center gap-1 text-xs text-mut">
              <input type="checkbox" checked={filters.freeze_disabled === "true"}
                onChange={(e) => setFilters({ ...filters, freeze_disabled: e.target.checked ? "true" : "" })} />
              freeze off
            </label>
          </div>
          <div className="flex items-end">
            <button className="btn" onClick={() => { setFilters({ sol_pair: "true" }); setPreset(""); }}>Reset</button>
          </div>
        </div>
      )}

      {error && <div className="card border-neg/50 p-3 text-sm text-neg">Could not load pools: {error}</div>}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm num">
          <thead className="text-xs text-mut">
            <tr className="border-b border-line">
              <th className="px-2 py-2 text-left font-medium">Pool</th>
              <th className="px-2 py-2 text-left font-medium">Safety</th>
              {COLUMNS.map(([key, label, , sortable]) => (
                <th key={key} className="cursor-pointer whitespace-nowrap px-2 py-2 text-right font-medium hover:text-slate-200"
                  onClick={() => sortable && clickSort(String(key))}>
                  {label}{sort === key ? (order === "desc" ? " ↓" : " ↑") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.data.map((r) => (
              <tr key={r.address} className="border-b border-line/60 hover:bg-white/[0.03]">
                <td className="whitespace-nowrap px-2 py-1.5">
                  <Link href={`/pools/${r.address}`} className="font-medium text-slate-100 hover:text-acc">
                    {r.name ?? r.address.slice(0, 6)}
                  </Link>
                  {r.is_hot && <span className="ml-1 text-[10px] text-acc">HOT</span>}
                </td>
                <td className="px-2 py-1.5"><Safety r={r} /></td>
                {COLUMNS.map(([key, , render]) => (
                  <td key={key}
                    className={`whitespace-nowrap px-2 py-1.5 text-right ${String(key).startsWith("price_change") ? tone(r[key] as number | null) : ""}`}>
                    {render(r)}
                  </td>
                ))}
              </tr>
            ))}
            {data && data.data.length === 0 && (
              <tr><td colSpan={COLUMNS.length + 2} className="p-6 text-center text-mut">No pool matches these filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {data && data.total > pageSize && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="text-mut num">{page * pageSize + 1}–{Math.min((page + 1) * pageSize, data.total)} of {data.total}</span>
          <button className="btn" disabled={(page + 1) * pageSize >= data.total} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}
