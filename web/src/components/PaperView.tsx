"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { me, User } from "@/lib/auth";
import { age, pct, price, usd } from "@/lib/format";
import * as P from "@/lib/paper";

const RISK_TONE: Record<string, string> = {
  low: "bg-pos/15 text-pos",
  medium: "bg-yellow-400/15 text-yellow-300",
  high: "bg-neg/15 text-neg",
};

function tone(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "";
  return v > 0 ? "text-pos" : v < 0 ? "text-neg" : "";
}

function hoursSince(iso: string): number {
  return (Date.now() - new Date(iso).getTime()) / 3_600_000;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ------------------------------------------------------------------ narrative heat
function HeatStrip({ heat }: { heat: P.Heat[] }) {
  if (!heat.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-mut">Narrative heat now:</span>
      {heat.map((h) => (
        <span key={h.narrative} className="rounded border border-line px-2 py-0.5 num"
          title={`${h.tokens} liquid tokens · median 1h ${h.median_chg_1h.toFixed(1)}% · 6h ${h.median_chg_6h.toFixed(1)}%`}>
          #{h.rank} {h.label} <span className={tone(h.median_chg_1h)}>{pct(h.median_chg_1h)}</span>
        </span>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ positions of one bot
function Positions({ botId }: { botId: number }) {
  const [rows, setRows] = useState<P.PaperPosition[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => P.botPositions(botId).then((r) => alive && setRows(r.positions), (e) => alive && setError(errText(e)));
    load();
    const t = setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [botId]);
  if (error) return <p className="p-3 text-sm text-neg">{error}</p>;
  if (!rows) return <p className="p-3 text-sm text-mut">Loading positions…</p>;
  if (!rows.length) return <p className="p-3 text-sm text-mut">No positions yet: the bot buys when a token passes its strategy.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs num">
        <thead className="text-mut">
          <tr className="border-b border-line">
            {["Token", "Narrative", "Status", "Opened", "Held", "Entry", "Last", "Move", "In", "P&L", "Cost in", "Exit"].map((h) => (
              <th key={h} className="whitespace-nowrap px-2 py-1.5 text-left font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.id} className="border-b border-line/50">
              <td className="whitespace-nowrap px-2 py-1">
                <a href={`https://dexscreener.com/solana/${p.mint}`} target="_blank" rel="noopener noreferrer"
                  className="font-medium text-slate-100 hover:text-acc">{p.symbol ?? p.mint.slice(0, 6)}</a>
              </td>
              <td className="px-2 py-1 text-mut">{p.narrative}</td>
              <td className={`px-2 py-1 ${p.status === "open" ? "text-acc" : "text-mut"}`}>
                {p.status}{p.tp_done === "true" ? " · half sold" : ""}
              </td>
              <td className="whitespace-nowrap px-2 py-1 text-mut">{new Date(p.opened_at).toLocaleString()}</td>
              <td className="px-2 py-1">{age(((p.closed_at ? new Date(p.closed_at).getTime() : Date.now()) - new Date(p.opened_at).getTime()) / 3_600_000)}</td>
              <td className="px-2 py-1">${price(p.entry_price)}</td>
              <td className="px-2 py-1">${price(p.last_price)}</td>
              <td className={`px-2 py-1 ${tone(p.price_chg_pct)}`}>{pct(p.price_chg_pct)}</td>
              <td className="px-2 py-1">{usd(p.usd_in, 0)}</td>
              <td className={`whitespace-nowrap px-2 py-1 ${tone(p.pnl_usd)}`}>
                {p.pnl_usd === null ? "–" : `${p.pnl_usd >= 0 ? "+" : ""}$${p.pnl_usd.toFixed(2)}`} <span className="text-mut">{pct(p.pnl_pct)}</span>
              </td>
              <td className="px-2 py-1 text-mut" title="round-trip swap cost measured with Jupiter quotes at entry">
                {p.entry_cost_pct === null ? "–" : `${p.entry_cost_pct.toFixed(2)}%`}
              </td>
              <td className="px-2 py-1 text-mut">{p.exit_reason?.replace(/_/g, " ") ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ one bot
function BotCard({ b, onChange, owned }: { b: P.Bot; onChange?: () => void; owned: boolean }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChange?.();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const statusTone = b.status === "running" ? "text-pos" : b.status === "stopped" ? "text-mut" : "text-yellow-300";
  return (
    <div className="card">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 p-3">
        <div className="min-w-[180px]">
          <div className="font-medium text-slate-100">{b.name}</div>
          <div className="text-xs text-mut">{b.strategy_name} · <span className={statusTone}>{b.status}</span> · {age(hoursSince(b.created_at))}</div>
        </div>
        <Stat label="Equity" value={usd(b.equity_usd, 2)} />
        <Stat label="P&L" value={`${b.pnl_usd >= 0 ? "+" : ""}$${b.pnl_usd.toFixed(2)}`} sub={pct(b.pnl_pct, 2)} cls={tone(b.pnl_usd)} />
        <Stat label="Realized" value={`$${b.realized_usd.toFixed(2)}`} cls={tone(b.realized_usd)} />
        <Stat label="Open" value={`${b.open_positions}/${b.max_open}`} sub={b.open_positions ? `$${b.unrealized_usd.toFixed(2)}` : undefined} />
        <Stat label="Closed" value={String(b.closed_positions)} sub={b.win_rate === null ? undefined : `${b.win_rate}% win`} />
        <Stat label="Budget" value={usd(b.budget_usd)} sub={`${usd(b.position_usd)} / trade`} />
        <div className="ml-auto flex flex-wrap gap-2">
          <button className="btn" onClick={() => setOpen(!open)}>{open ? "Hide" : "Positions"}</button>
          {owned && b.status === "running" && <button className="btn" disabled={busy} onClick={() => act(() => P.botAction(b.id, "pause"))}>Pause</button>}
          {owned && b.status === "paused" && <button className="btn" disabled={busy} onClick={() => act(() => P.botAction(b.id, "resume"))}>Resume</button>}
          {owned && (b.status === "running" || b.status === "paused") && (
            <button className="btn" disabled={busy} title="Sells every open position, then returns the cash to your account"
              onClick={() => act(() => P.botAction(b.id, "stop"))}>Stop</button>
          )}
          {owned && b.status === "stopped" && <button className="btn" disabled={busy} onClick={() => act(() => P.deleteBot(b.id))}>Remove</button>}
        </div>
      </div>
      {error && <p className="px-3 pb-2 text-sm text-neg">{error}</p>}
      {open && <div className="border-t border-line"><Positions botId={b.id} /></div>}
    </div>
  );
}

function Stat({ label, value, sub, cls = "" }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="num">
      <div className="label">{label}</div>
      <div className={`text-sm ${cls}`}>{value}{sub && <span className="ml-1 text-xs text-mut">{sub}</span>}</div>
    </div>
  );
}

// ------------------------------------------------------------------ strategy builder
const ENTRY_FIELDS: [string, string][] = [
  ["mcap_min", "Mcap ≥ $"], ["mcap_max", "Mcap ≤ $"], ["liquidity_min", "Liquidity ≥ $"], ["age_h_min", "Age ≥ h"],
  ["age_h_max", "Age ≤ h"], ["holders_min", "Holders ≥"], ["top10_max", "Top-10 ≤ %"], ["organic_min", "Organic ≥"],
  ["volume_1h_min", "Volume 1h ≥ $"], ["buy_ratio_1h_min", "Buy/sell vol 1h ≥"], ["ds_txn_ratio_1h_min", "Buy/sell txns 1h ≥"],
  ["chg_5m_min", "Price 5m ≥ %"], ["chg_1h_min", "Price 1h ≥ %"], ["chg_1h_max", "Price 1h ≤ %"], ["chg_6h_min", "Price 6h ≥ %"],
  ["chg_24h_min", "Price 24h ≥ %"], ["chg_24h_max", "Price 24h ≤ %"], ["momentum_min", "Momentum ≥"], ["safety_min", "Safety ≥"],
];
const EXIT_FIELDS: [string, string][] = [
  ["stop_loss_pct", "Stop loss %"], ["take_profit_pct", "Take profit %"], ["take_profit_part", "Sell at TP (0.1–1)"],
  ["trail_trigger_pct", "Trail from +%"], ["trail_drop_pct", "Trail drop %"], ["max_hours", "Max hours"],
  ["fast_drop_pct", "Fast drop % (2 min)"], ["rug_liquidity_drop_pct", "Liquidity drop %"], ["narrative_cool_rank", "Sell if narrative rank >"],
];
const RANKS = ["momentum", "safety", "chg_1h", "volume_1h", "buy_ratio_1h", "liquidity", "holder_chg_1h"];

function numOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function StrategyBuilder({ tpls, labels, tiers, onSaved }: {
  tpls: P.Template[]; labels: Record<string, string>; tiers: Record<string, string>; onSaved: () => void;
}) {
  const [base, setBase] = useState(tpls[0]?.id ?? "");
  const [params, setParams] = useState<P.Params | null>(tpls[0] ? structuredClone(tpls[0].params) : null);
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = (id: string) => {
    setBase(id);
    const t = tpls.find((x) => x.id === id);
    if (t) setParams(structuredClone(t.params));
  };
  if (!params) return null;
  const setE = (k: string, v: P.EntryParams[string]) => setParams({ ...params, entry: { ...params.entry, [k]: v } });
  const setX = (k: string, v: P.ExitParams[string]) => setParams({ ...params, exit: { ...params.exit, [k]: v } });
  const toggle = (k: "narratives" | "tiers", v: string) => {
    const cur = params.entry[k];
    setE(k, cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]);
  };
  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await P.createStrategy(name || "My strategy", "", base, params);
      setMsg("Saved. Pick it in “New bot”.");
      onSaved();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card space-y-3 p-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1">
          <span className="label">Start from</span>
          <select className="input" value={base} onChange={(e) => pick(e.target.value)}>
            {tpls.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
        <label className="space-y-1">
          <span className="label">Name</span>
          <input className="input" maxLength={60} value={name} placeholder="My strategy" onChange={(e) => setName(e.target.value)} />
        </label>
        <button className="btn-primary" disabled={busy} onClick={save}>Save strategy</button>
        {msg && <span className="text-sm text-mut">{msg}</span>}
      </div>

      <div>
        <div className="label mb-1">Narratives (none = all)</div>
        <div className="flex flex-wrap gap-1">
          {Object.entries(labels).map(([k, l]) => (
            <button key={k} onClick={() => toggle("narratives", k)}
              className={`btn py-0.5 text-xs ${params.entry.narratives.includes(k) ? "border-acc text-acc" : ""}`}>{l}</button>
          ))}
        </div>
      </div>
      <div>
        <div className="label mb-1">Size tiers (none = all)</div>
        <div className="flex flex-wrap gap-1">
          {Object.entries(tiers).map(([k, l]) => (
            <button key={k} onClick={() => toggle("tiers", k)}
              className={`btn py-0.5 text-xs ${params.entry.tiers.includes(k) ? "border-acc text-acc" : ""}`}>{l}</button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-4 text-sm text-mut">
        {(["safe_only", "socials_only", "exclude_boosted"] as const).map((k) => (
          <label key={k} className="flex items-center gap-1">
            <input type="checkbox" checked={params.entry[k]} onChange={(e) => setE(k, e.target.checked)} />
            {{ safe_only: "no red flags", socials_only: "has website / X / Telegram", exclude_boosted: "skip paid boosts" }[k]}
          </label>
        ))}
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={params.exit.sell_pressure_exit} onChange={(e) => setX("sell_pressure_exit", e.target.checked)} />
          sell on sell pressure
        </label>
        <label className="flex items-center gap-1">
          only top
          <input className="input w-14 num" inputMode="numeric" value={params.entry.hot_narratives || ""}
            onChange={(e) => setE("hot_narratives", Number(e.target.value) || 0)} />
          hottest narratives
        </label>
        <label className="flex items-center gap-1">
          rank by
          <select className="input w-36" value={params.entry.rank_by} onChange={(e) => setE("rank_by", e.target.value)}>
            {RANKS.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
      </div>

      <div className="label">Entry filters (empty = not checked)</div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        {ENTRY_FIELDS.map(([k, l]) => (
          <label key={k} className="space-y-1">
            <span className="label">{l}</span>
            <input className="input num" inputMode="decimal" value={(params.entry[k] as number | null) ?? ""}
              onChange={(e) => setE(k, numOrNull(e.target.value))} />
          </label>
        ))}
      </div>
      <div className="label">Exit rules</div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        {EXIT_FIELDS.map(([k, l]) => (
          <label key={k} className="space-y-1">
            <span className="label">{l}</span>
            <input className="input num" inputMode="decimal" value={(params.exit[k] as number | null) ?? ""}
              onChange={(e) => setX(k, numOrNull(e.target.value))} />
          </label>
        ))}
        <label className="space-y-1">
          <span className="label">Cooldown h</span>
          <input className="input num" inputMode="decimal" value={params.cooldown_h}
            onChange={(e) => setParams({ ...params, cooldown_h: Number(e.target.value) || 0 })} />
        </label>
        <label className="space-y-1">
          <span className="label">Max swap cost %</span>
          <input className="input num" inputMode="decimal" value={params.max_cost_pct}
            onChange={(e) => setParams({ ...params, max_cost_pct: Number(e.target.value) || 0 })} />
        </label>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ new bot form
function NewBot({ tpls, mine, free, onMade }: { tpls: P.Template[]; mine: P.MyStrategy[]; free: number; onMade: () => void }) {
  const [strategy, setStrategy] = useState(tpls[0] ? `tpl:${tpls[0].id}` : "");
  const [name, setName] = useState("");
  const [budget, setBudget] = useState("1000");
  const [size, setSize] = useState("100");
  const [maxOpen, setMaxOpen] = useState("3");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const tpl = strategy.startsWith("tpl:") ? tpls.find((t) => `tpl:${t.id}` === strategy) : null;
  const my = strategy.startsWith("my:") ? mine.find((s) => `my:${s.id}` === strategy) : null;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await P.createBot({ name: name || tpl?.name || my?.name || "Bot", strategy, budget_usd: Number(budget),
        position_usd: Number(size), max_open: Number(maxOpen) });
      setName("");
      onMade();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card space-y-3 p-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <label className="col-span-2 space-y-1">
          <span className="label">Strategy</span>
          <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <optgroup label="Built-in">
              {tpls.map((t) => <option key={t.id} value={`tpl:${t.id}`}>{t.name} ({t.risk} risk)</option>)}
            </optgroup>
            {mine.length > 0 && (
              <optgroup label="Mine">
                {mine.map((s) => <option key={s.id} value={`my:${s.id}`}>{s.name}</option>)}
              </optgroup>
            )}
          </select>
        </label>
        <label className="space-y-1">
          <span className="label">Bot name</span>
          <input className="input" maxLength={40} value={name} placeholder={tpl?.name ?? my?.name ?? ""} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="space-y-1">
          <span className="label">Budget $ (free {usd(free)})</span>
          <input className="input num" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} />
        </label>
        <label className="space-y-1">
          <span className="label">Per trade $</span>
          <input className="input num" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} />
        </label>
        <label className="space-y-1">
          <span className="label">Max open trades</span>
          <input className="input num" inputMode="numeric" value={maxOpen} onChange={(e) => setMaxOpen(e.target.value)} />
        </label>
      </div>
      {tpl && (
        <p className="text-sm text-mut">
          <span className={`mr-2 rounded px-1.5 py-0.5 text-xs ${RISK_TONE[tpl.risk]}`}>{tpl.risk} risk</span>
          {tpl.description}
        </p>
      )}
      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={busy || !strategy} onClick={submit}>Start bot</button>
        {error && <span className="text-sm text-neg">{error}</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ page
export function PaperView() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [tpl, setTpl] = useState<{ templates: P.Template[]; narratives: Record<string, string>; tiers: Record<string, string> } | null>(null);
  const [acc, setAcc] = useState<{ account: P.Account | null; bots: P.Bot[]; max_usd: number } | null>(null);
  const [mine, setMine] = useState<P.MyStrategy[]>([]);
  const [houseBots, setHouseBots] = useState<P.Bot[]>([]);
  const [hb, setHb] = useState<{ ts: number; tokens: number; sol_usd: number; paused: string | null } | null>(null);
  const [heat, setHeat] = useState<P.Heat[]>([]);
  const [startUsd, setStartUsd] = useState("10000");
  const [confirmReset, setConfirmReset] = useState(false);
  const [showBuilder, setShowBuilder] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadMine = useCallback(async () => {
    try {
      const [a, s] = await Promise.all([P.account(), P.strategies()]);
      setAcc(a);
      setMine(s.mine);
    } catch (e) {
      if ((e as { status?: number }).status !== 401) setError(errText(e));
    }
  }, []);

  const loadPublic = useCallback(async () => {
    P.house().then((h) => { setHouseBots(h.bots); setHb(h.heartbeat); }, () => undefined);
    P.categories().then((c) => setHeat(c.heat), () => undefined);
  }, []);

  useEffect(() => {
    me().then(setUser, () => setUser(null));
    P.templates().then(setTpl, (e) => setError(errText(e)));
    void loadPublic();
  }, [loadPublic]);

  useEffect(() => {
    if (user) void loadMine();
  }, [user, loadMine]);

  useEffect(() => {
    const t = setInterval(() => {
      void loadPublic();
      if (user) void loadMine();
    }, 30_000);
    return () => clearInterval(t);
  }, [user, loadMine, loadPublic]);

  const makeAccount = async (reset: boolean) => {
    setError(null);
    try {
      setAcc(await P.createAccount(Number(startUsd), reset).then(() => P.account()));
      setConfirmReset(false);
    } catch (e) {
      setError(errText(e));
    }
  };

  const sortedHouse = useMemo(() => [...houseBots].sort((a, b) => b.pnl_usd - a.pnl_usd), [houseBots]);
  const a = acc?.account;

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold">Demo trading: memecoin bots</h1>
          <span className="rounded bg-acc/15 px-2 py-0.5 text-xs text-acc">virtual money</span>
          {hb && (
            <span className="ml-auto text-xs text-mut num">
              engine {Date.now() / 1000 - hb.ts < 120 ? "live" : "late"} · {hb.tokens} tokens watched · SOL {usd(hb.sol_usd, 2)}
              {hb.paused ? ` · entries paused: ${hb.paused}` : ""}
            </span>
          )}
        </div>
        <p className="text-xs text-mut">
          Like a forex demo account: up to $10,000 of virtual money, bots that buy and sell memecoins on live prices
          (Jupiter + DexScreener), with real swap costs from Jupiter quotes. No wallet, nothing is ever traded.
          Results on paper are not a promise of real results.
        </p>
        <HeatStrip heat={heat} />
      </div>

      {error && <div className="card border-neg/50 p-3 text-sm text-neg">{error}</div>}

      {user === null && (
        <div className="card p-4 text-sm">
          <Link href="/login?next=/paper" className="text-acc hover:underline">Log in</Link> or{" "}
          <Link href="/signup?next=/paper" className="text-acc hover:underline">sign up</Link> to open your demo account and create bots.
          The house bots below run every built-in strategy for everyone to compare.
        </div>
      )}

      {user && acc && !a && (
        <div className="card flex flex-wrap items-end gap-3 p-4">
          <label className="space-y-1">
            <span className="label">Starting balance (max {usd(acc.max_usd)})</span>
            <input className="input num w-40" inputMode="decimal" value={startUsd} onChange={(e) => setStartUsd(e.target.value)} />
          </label>
          <button className="btn-primary" onClick={() => makeAccount(false)}>Open demo account</button>
        </div>
      )}

      {user && a && (
        <section className="space-y-3">
          <div className="card flex flex-wrap items-center gap-x-8 gap-y-2 p-4">
            <Stat label="Equity" value={usd(a.equity_usd, 2)} />
            <Stat label="P&L" value={`${a.pnl_usd >= 0 ? "+" : ""}$${a.pnl_usd.toFixed(2)}`} sub={pct(a.pnl_pct, 2)} cls={tone(a.pnl_usd)} />
            <Stat label="Free cash" value={usd(a.cash_usd, 2)} />
            <Stat label="In bots" value={usd(a.in_bots_usd, 2)} />
            <Stat label="Started with" value={usd(a.start_usd)} sub={`since ${new Date(a.reset_at).toLocaleDateString()}`} />
            <div className="ml-auto flex items-end gap-2">
              {confirmReset ? (
                <>
                  <input className="input num w-28" value={startUsd} onChange={(e) => setStartUsd(e.target.value)} />
                  <button className="btn border-neg text-neg" onClick={() => makeAccount(true)}>Delete all bots and reset</button>
                  <button className="btn" onClick={() => setConfirmReset(false)}>Cancel</button>
                </>
              ) : (
                <button className="btn" onClick={() => setConfirmReset(true)}>Reset account</button>
              )}
            </div>
          </div>

          {tpl && (
            <>
              <div className="flex items-center gap-3">
                <h2 className="font-semibold">New bot</h2>
                <button className="btn text-xs" onClick={() => setShowBuilder(!showBuilder)}>
                  {showBuilder ? "Hide strategy builder" : "Build your own strategy"}
                </button>
              </div>
              {showBuilder && (
                <StrategyBuilder tpls={tpl.templates} labels={tpl.narratives} tiers={tpl.tiers} onSaved={loadMine} />
              )}
              {mine.length > 0 && (
                <div className="flex flex-wrap gap-2 text-xs text-mut">
                  My strategies:
                  {mine.map((s) => (
                    <span key={s.id} className="rounded border border-line px-2 py-0.5">
                      {s.name}
                      <button className="ml-2 text-neg hover:underline" onClick={() => P.deleteStrategy(s.id).then(loadMine)}>×</button>
                    </span>
                  ))}
                </div>
              )}
              <NewBot tpls={tpl.templates} mine={mine} free={a.cash_usd} onMade={loadMine} />
            </>
          )}

          <h2 className="font-semibold">My bots</h2>
          {acc.bots.length === 0 && <p className="text-sm text-mut">No bots yet.</p>}
          <div className="space-y-2">
            {acc.bots.map((b) => <BotCard key={b.id} b={b} owned onChange={loadMine} />)}
          </div>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="font-semibold">House bots: every built-in strategy, $1,000 each</h2>
        <p className="text-xs text-mut">Same engine, same live data, running for everyone to compare. Sorted by P&L.</p>
        {sortedHouse.length === 0 && <p className="text-sm text-mut">Starting…</p>}
        <div className="space-y-2">
          {sortedHouse.map((b) => <BotCard key={b.id} b={b} owned={false} />)}
        </div>
      </section>
    </div>
  );
}
