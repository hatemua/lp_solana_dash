"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, qs } from "@/lib/api";
import { age, num, pct, price, usd } from "@/lib/format";

interface Flag {
  level: "red" | "warn" | "info";
  code: string;
  text: string;
}

interface MemeRow {
  mint: string;
  symbol: string | null;
  name: string | null;
  launchpad: string | null;
  verified: boolean;
  price: number | null;
  mcap: number | null;
  liquidity: number | null;
  holders: number | null;
  age_h: number | null;
  organic_score: number | null;
  top10_pct: number | null;
  chg_5m: number | null;
  chg_1h: number | null;
  chg_6h: number | null;
  chg_24h: number | null;
  volume_1h: number | null;
  volume_24h: number | null;
  buy_ratio_1h: number | null;
  traders_1h: number | null;
  holder_chg_1h: number | null;
  safety: number;
  momentum: number;
  narrative: string;
  tier: string | null;
  ds_txn_ratio_1h: number | null;
  has_website?: boolean;
  has_twitter?: boolean;
  has_telegram?: boolean;
  flags: Flag[];
  lists: string[];
  meteora_pool: { address: string; name: string | null; tvl: number | null; bin_step: number | null } | null;
}

interface MemesResponse {
  total: number;
  universe: number;
  preset: string | null;
  sort: string;
  updated_at: number;
  data: MemeRow[];
  heat: { narrative: string; label: string; rank: number; median_chg_1h: number; tokens: number }[];
}

const NARRATIVES: Record<string, string> = { ai: "AI", political: "Political", celebrity: "Celebrity", dog: "Dog",
  cat: "Cat", frog: "Frog", animal: "Other animals", gaming: "Gaming", culture: "Culture / other" };
const TIERS: Record<string, string> = { blue: "Blue ≥ $100M", large: "Large $10–100M", mid: "Mid $1–10M",
  small: "Small $100k–1M", micro: "Micro < $100k" };

interface MemePreset {
  id: string;
  label: string;
  description: string;
  sort: string;
}

type Filters = Record<string, string>;

// [param, label, hint]
const FILTER_FIELDS: [string, string, string][] = [
  ["mcap_min", "Mcap ≥", "$"],
  ["mcap_max", "Mcap ≤", "$"],
  ["liquidity_min", "Liquidity ≥", "$"],
  ["volume_1h_min", "Volume 1h ≥", "$"],
  ["age_h_min", "Age ≥", "hours"],
  ["age_h_max", "Age ≤", "hours"],
  ["holders_min", "Holders ≥", ""],
  ["top10_max", "Top-10 ≤", "%"],
  ["organic_min", "Organic ≥", "0–100"],
  ["buy_ratio_1h_min", "Buy/sell 1h ≥", "×"],
  ["chg_1h_min", "Price 1h ≥", "%"],
  ["chg_1h_max", "Price 1h ≤", "%"],
  ["chg_24h_min", "Price 24h ≥", "%"],
  ["chg_24h_max", "Price 24h ≤", "%"],
  ["safety_min", "Safety ≥", "0–100"],
  ["momentum_min", "Momentum ≥", "0–100"],
];

// [sort key, label, render]
const COLUMNS: [string, string, (r: MemeRow) => string][] = [
  ["price", "Price", (r) => (r.price === null ? "–" : `$${price(r.price)}`)],
  ["mcap", "Mcap", (r) => usd(r.mcap)],
  ["liquidity", "Liquidity", (r) => usd(r.liquidity)],
  ["volume_1h", "Vol 1h", (r) => usd(r.volume_1h)],
  ["volume_24h", "Vol 24h", (r) => usd(r.volume_24h)],
  ["chg_5m", "5m", (r) => pct(r.chg_5m)],
  ["chg_1h", "1h", (r) => pct(r.chg_1h)],
  ["chg_6h", "6h", (r) => pct(r.chg_6h)],
  ["chg_24h", "24h", (r) => pct(r.chg_24h)],
  ["buy_ratio_1h", "Buy/sell vol", (r) => (r.buy_ratio_1h === null ? "–" : `${r.buy_ratio_1h.toFixed(2)}×`)],
  ["ds_txn_ratio_1h", "Buy/sell txns", (r) => (r.ds_txn_ratio_1h == null ? "–" : `${r.ds_txn_ratio_1h.toFixed(2)}×`)],
  ["traders_1h", "Traders 1h", (r) => num(r.traders_1h)],
  ["holders", "Holders", (r) => num(r.holders)],
  ["holder_chg_1h", "Holders 1h", (r) => pct(r.holder_chg_1h, 2)],
  ["age_h", "Age", (r) => age(r.age_h)],
  ["organic_score", "Organic", (r) => num(r.organic_score)],
];
const SORTABLE = new Set(["mcap", "liquidity", "volume_1h", "volume_24h", "chg_5m", "chg_1h", "chg_6h", "chg_24h",
  "buy_ratio_1h", "traders_1h", "holders", "holder_chg_1h", "age_h", "organic_score", "safety", "momentum",
  "ds_txn_ratio_1h"]);

function tone(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "";
  return v > 0 ? "text-pos" : v < 0 ? "text-neg" : "";
}

function Score({ v, kind }: { v: number; kind: "safety" | "momentum" }) {
  const c = v >= 70 ? "bg-pos/15 text-pos" : v >= 45 ? "bg-yellow-400/15 text-yellow-300" : "bg-neg/15 text-neg";
  return <span title={kind} className={`rounded px-1.5 py-0.5 text-xs ${c}`}>{Math.round(v)}</span>;
}

function Flags({ flags }: { flags: Flag[] }) {
  if (!flags.length) return <span className="rounded bg-pos/15 px-1 text-[10px] text-pos">clean</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {flags.map((f) => (
        <span key={f.code} title={f.text}
          className={`rounded px-1 text-[10px] ${f.level === "red" ? "bg-neg/15 text-neg" : f.level === "warn" ? "bg-yellow-400/15 text-yellow-300" : "bg-line text-mut"}`}>
          {f.code.replace(/_/g, " ")}
        </span>
      ))}
    </span>
  );
}

export function MemesView() {
  const [presets, setPresets] = useState<MemePreset[]>([]);
  const [preset, setPreset] = useState("safe_runners");
  const [filters, setFilters] = useState<Filters>({});
  const [sort, setSort] = useState("momentum");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [q, setQ] = useState("");
  const [safeOnly, setSafeOnly] = useState(false);
  const [meteoraOnly, setMeteoraOnly] = useState(false);
  const [narrative, setNarrative] = useState("");
  const [tier, setTier] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [data, setData] = useState<MemesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ presets: MemePreset[] }>("/v1/memes/presets").then((r) => setPresets(r.presets)).catch(() => undefined);
  }, []);

  const params = useMemo(() => ({ ...filters, preset, sort, order, q, limit: 200,
    safe_only: safeOnly ? "true" : "", meteora_only: meteoraOnly ? "true" : "", narrative, tier }),
  [filters, preset, sort, order, q, safeOnly, meteoraOnly, narrative, tier]);
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const load = useCallback(async () => {
    try {
      setData(await api<MemesResponse>(`/v1/memes${qs(paramsRef.current)}`));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [params, load]);

  useEffect(() => {
    const t = setInterval(() => void load(), 60_000);
    return () => clearInterval(t);
  }, [load]);

  const choosePreset = (id: string) => {
    const next = id === preset ? "" : id;
    setPreset(next);
    const p = presets.find((x) => x.id === next);
    setSort(p ? p.sort : "momentum");
    setOrder("desc");
  };

  const clickSort = (key: string) => {
    if (!SORTABLE.has(key)) return;
    if (sort === key) setOrder(order === "desc" ? "asc" : "desc");
    else {
      setSort(key);
      setOrder("desc");
    }
  };

  const th = (key: string, label: string, left = false) => (
    <th key={key} onClick={() => clickSort(key)}
      className={`whitespace-nowrap px-2 py-2 font-medium ${left ? "text-left" : "text-right"} ${SORTABLE.has(key) ? "cursor-pointer hover:text-slate-200" : ""}`}>
      {label}{sort === key ? (order === "desc" ? " ↓" : " ↑") : ""}
    </th>
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-2 text-lg font-semibold">Memecoin screener</h1>
        {presets.map((p) => (
          <button key={p.id} title={p.description} onClick={() => choosePreset(p.id)}
            className={`btn ${preset === p.id ? "border-acc text-acc" : ""}`}>
            {p.label}
          </button>
        ))}
        <button className="btn" onClick={() => setShowFilters(!showFilters)}>Filters {showFilters ? "▴" : "▾"}</button>
        <input className="input max-w-[200px]" placeholder="Search symbol, name or mint" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <select className="input w-auto" value={narrative} onChange={(e) => setNarrative(e.target.value)}>
          <option value="">All narratives</option>
          {Object.entries(NARRATIVES).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select className="input w-auto" value={tier} onChange={(e) => setTier(e.target.value)}>
          <option value="">All sizes</option>
          {Object.entries(TIERS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <label className="flex items-center gap-1 text-sm text-mut">
          <input type="checkbox" checked={safeOnly} onChange={(e) => setSafeOnly(e.target.checked)} /> no red flags
        </label>
        <label className="flex items-center gap-1 text-sm text-mut">
          <input type="checkbox" checked={meteoraOnly} onChange={(e) => setMeteoraOnly(e.target.checked)} /> has Meteora pool
        </label>
        <span className="ml-auto text-xs text-mut num">
          {data ? `${data.total} of ${data.universe} memecoins` : "loading…"}
          {data ? ` · data ${new Date(data.updated_at * 1000).toLocaleTimeString()}` : ""}
        </span>
      </div>
      <p className="text-xs text-mut">
        {preset ? presets.find((p) => p.id === preset)?.description : "All memecoins in Jupiter's trending, most traded, top organic and new lists."}
        {" "}Safety and momentum are 0–100 rankings of the current data, not buy signals. Memecoins can go to zero.
        {" "}Data: Jupiter + DexScreener.
      </p>
      {data?.heat?.length ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-mut">Narrative heat:</span>
          {data.heat.map((h) => (
            <button key={h.narrative} onClick={() => setNarrative(narrative === h.narrative ? "" : h.narrative)}
              className={`rounded border px-2 py-0.5 num ${narrative === h.narrative ? "border-acc text-acc" : "border-line"}`}
              title={`${h.tokens} liquid tokens`}>
              #{h.rank} {h.label} <span className={tone(h.median_chg_1h)}>{pct(h.median_chg_1h)}</span>
            </button>
          ))}
        </div>
      ) : null}

      {showFilters && (
        <div className="card grid grid-cols-2 gap-3 p-3 sm:grid-cols-4 lg:grid-cols-8">
          {FILTER_FIELDS.map(([key, label, hint]) => (
            <label key={key} className="space-y-1">
              <span className="label">{label} <span className="opacity-60">{hint}</span></span>
              <input className="input num" inputMode="decimal" value={filters[key] ?? ""}
                onChange={(e) => setFilters({ ...filters, [key]: e.target.value })} />
            </label>
          ))}
          <div className="flex items-end">
            <button className="btn" onClick={() => { setFilters({}); setPreset(""); setSafeOnly(false); setMeteoraOnly(false); }}>
              Reset
            </button>
          </div>
        </div>
      )}

      {error && <div className="card border-neg/50 p-3 text-sm text-neg">Could not load memecoins: {error}</div>}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm num">
          <thead className="text-xs text-mut">
            <tr className="border-b border-line">
              {th("symbol", "Token", true)}
              {th("safety", "Safety")}
              {th("momentum", "Momentum")}
              {COLUMNS.map(([key, label]) => th(key, label))}
              {th("flags", "Flags", true)}
              {th("links", "Links", true)}
            </tr>
          </thead>
          <tbody>
            {data?.data.map((r) => (
              <tr key={r.mint} className="border-b border-line/60 hover:bg-white/[0.03]">
                <td className="whitespace-nowrap px-2 py-1.5">
                  <span className="font-medium text-slate-100">{r.symbol ?? r.mint.slice(0, 6)}</span>
                  {r.verified && <span className="ml-1 text-[10px] text-acc" title="Jupiter verified">✓</span>}
                  <div className="max-w-[180px] truncate text-[11px] text-mut" title={r.name ?? ""}>
                    {NARRATIVES[r.narrative] ?? r.narrative}{r.tier ? ` · ${r.tier}` : ""} · {r.name}
                  </div>
                </td>
                <td className="px-2 py-1.5 text-right"><Score v={r.safety} kind="safety" /></td>
                <td className="px-2 py-1.5 text-right"><Score v={r.momentum} kind="momentum" /></td>
                {COLUMNS.map(([key, , render]) => (
                  <td key={key}
                    className={`whitespace-nowrap px-2 py-1.5 text-right ${key.startsWith("chg") || key === "holder_chg_1h" ? tone(r[key as keyof MemeRow] as number | null) : ""}`}>
                    {render(r)}
                  </td>
                ))}
                <td className="min-w-[140px] px-2 py-1.5"><Flags flags={r.flags} /></td>
                <td className="whitespace-nowrap px-2 py-1.5 text-xs">
                  {r.meteora_pool && (
                    <Link href={`/pools/${r.meteora_pool.address}`} className="mr-2 text-acc hover:underline"
                      title={`Meteora ${r.meteora_pool.name ?? ""} · TVL ${usd(r.meteora_pool.tvl)}`}>
                      LP pool
                    </Link>
                  )}
                  <a href={`https://jup.ag/tokens/${r.mint}`} target="_blank" rel="noopener noreferrer"
                    className="mr-2 text-mut hover:text-slate-100">Jupiter</a>
                  <a href={`https://dexscreener.com/solana/${r.mint}`} target="_blank" rel="noopener noreferrer"
                    className="text-mut hover:text-slate-100">Chart</a>
                </td>
              </tr>
            ))}
            {data && data.data.length === 0 && (
              <tr><td colSpan={COLUMNS.length + 5} className="p-6 text-center text-mut">
                No memecoin matches these filters right now. Try another preset or loosen a filter.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
