"use client";

import { useCallback, useEffect, useState } from "react";
import { useWallet } from "@solana/wallet-adapter-react";
import { call, me, User } from "@/lib/auth";
import { pct, shortAddr } from "@/lib/format";
import { BuiltTx, signAndSend } from "@/lib/tx";
import { WalletButton } from "./WalletButton";

interface Stats {
  closed: number;
  win_rate: number | null;
  avg_net_pct: number | null;
  median_win_pct: number | null;
  median_loss_pct: number | null;
  worst_pct: number | null;
}

interface Strategy { name: string; label: string; live_ok: boolean; last_7d: Stats }

interface Position {
  id: number;
  name: string;
  shape: string | null;
  status: string;
  net_pct: number | null;
  pnl_sol: number | null;
  sol_size: number;
  exit_reason: string | null;
  txs: string[];
  error: string | null;
}

interface Bot {
  id: number;
  name: string;
  strategy: string;
  mode: "paper" | "live";
  owner_wallet: string | null;
  position_sol: number;
  max_open: number;
  take_profit_pct: number | null;
  stop_loss_pct: number | null;
  day_loss_sol: number | null;
  day_target_sol: number | null;
  status: string;
  pending: { take_profit_pct?: number; stop_loss_pct?: number; reason?: string; applied?: boolean } | null;
  positions: Position[];
  totals: { closed?: number; wins?: number; pnl_sol?: number; open?: number; day_sol?: number };
  last_skip: { ts: string; why: string } | null;
  wallet: { address?: string; sol?: number; live_enabled?: boolean; error?: string } | null;
}

interface Access { configured: boolean; live_enabled: boolean; allowed: boolean }

const LIMIT_FIELDS = [
  ["take_profit_pct", "Take profit %", "e.g. 5"],
  ["stop_loss_pct", "Stop loss %", "e.g. 8"],
  ["position_sol", "Size per trade (SOL)", "0.1"],
  ["max_open", "Max open", "1"],
  ["day_loss_sol", "Daily loss limit (SOL)", "none"],
  ["day_target_sol", "Daily target (SOL)", "none"],
] as const;

type Limits = Record<(typeof LIMIT_FIELDS)[number][0], string>;

const OWNER_ACTIONS = { close_all: "Close all now", withdraw: "Withdraw all to my wallet", delete: "Delete bot" } as const;

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function limitsOf(b?: Bot): Limits {
  const v = (x: number | null | undefined) => (x == null ? "" : String(x));
  return {
    take_profit_pct: v(b?.take_profit_pct), stop_loss_pct: v(b?.stop_loss_pct), position_sol: v(b?.position_sol ?? 0.1),
    max_open: v(b?.max_open ?? 1), day_loss_sol: v(b?.day_loss_sol), day_target_sol: v(b?.day_target_sol),
  };
}

/** Form strings -> API body: empty = no limit (TP / SL / daily), numbers otherwise. */
function limitsBody(l: Limits): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const [k] of LIMIT_FIELDS) out[k] = l[k].trim() === "" ? null : Number(l[k]);
  return out;
}

function sol(v: number | null | undefined, digits = 4): string {
  if (v == null) return "–";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)} SOL`;
}

function tone(v: number | null | undefined): string {
  return (v ?? 0) > 0 ? "text-pos" : (v ?? 0) < 0 ? "text-neg" : "";
}

function hint(s?: Stats): string {
  if (!s || !s.closed) return "no closed positions in the last 7 days";
  return `last 7 days: ${s.closed} closed, ${s.win_rate}% won, median win ${pct(s.median_win_pct, 1)}, ` +
    `median loss ${pct(s.median_loss_pct, 1)}, worst ${pct(s.worst_pct, 1)}`;
}

function LimitInputs({ value, onChange }: { value: Limits; onChange: (l: Limits) => void }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      {LIMIT_FIELDS.map(([k, label, ph]) => (
        <label key={k} className="text-xs text-mut">
          {label}
          <input className="input mt-0.5 w-full"
            inputMode="decimal" placeholder={ph} value={value[k]}
            onChange={(e) => onChange({ ...value, [k]: e.target.value })} />
        </label>
      ))}
    </div>
  );
}

export function MyBots() {
  const { publicKey, signTransaction, signMessage } = useWallet();
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [bots, setBots] = useState<Bot[]>([]);
  const [access, setAccess] = useState<Access | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "My bot", strategy: "auto", mode: "paper" as "paper" | "live" });
  const [limits, setLimits] = useState<Limits>(limitsOf());

  const load = useCallback(async () => {
    try {
      setStrategies((await call<{ strategies: Strategy[] }>("/v1/lpbots/strategies")).strategies);
      const u = await me();
      setUser(u);
      if (u) setBots((await call<{ bots: Bot[] }>("/v1/lpbots")).bots);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 30_000);
    return () => clearInterval(id);
  }, [load]);

  const wallet = publicKey?.toBase58();
  useEffect(() => {
    if (!wallet) return setAccess(null);
    call<Access>(`/v1/lpbots/access?owner=${wallet}`).then(setAccess, () => setAccess(null));
  }, [wallet]);

  async function run(what: () => Promise<string | void>) {
    setBusy(true);
    try {
      const done = await what();
      if (done) setMsg(done);
      await load();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function sign(message: string) {
    if (!publicKey || !signMessage) throw new Error("connect your wallet first");
    setMsg("Sign the message in your wallet (free, no transaction)…");
    return { pubkey: publicKey.toBase58(), signature: toBase64(await signMessage(new TextEncoder().encode(message))) };
  }

  const create = () => run(async () => {
    const body: Record<string, unknown> = { ...form, ...limitsBody(limits) };
    for (const k of ["position_sol", "max_open"]) if (body[k] == null) delete body[k];
    if (form.mode === "live") {
      const ts = Math.floor(Date.now() / 1000);
      body.wallet = { ts, ...(await sign(`LP Dash: create real-money bot "${form.name}" at ${ts}`)) };
    }
    const b = await call<Bot>("/v1/lpbots", { method: "POST", body: JSON.stringify(body) });
    setCreating(false);
    return `Bot "${b.name}" created${b.mode === "live" ? ": fund its wallet to start trading" : ""}.`;
  });

  const strat = strategies.find((s) => s.name === form.strategy);
  const realOk = Boolean(access?.configured && access.allowed && strat?.live_ok);
  const realWhy = !wallet ? "connect your wallet" : !access?.allowed ? "invite-only for now"
    : !strat?.live_ok ? "real money follows Auto only for now" : null;

  return (
    <section className="card space-y-3 border-acc/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">My bots</h2>
        <span className="text-xs text-mut">follow a strategy · paper or real money · your take profit and stop loss</span>
        <div className="ml-auto flex items-center gap-2">
          {user && <button className="rounded bg-acc px-3 py-1 text-sm text-black" onClick={() => setCreating(!creating)}>
            {creating ? "Cancel" : "New bot"}</button>}
          <WalletButton />
        </div>
      </div>

      {user === null && (
        <p className="text-sm">
          <a className="text-acc hover:underline" href="/login?next=/bot">Sign in</a> to create your own bots.
          Claude can also tune them through the MCP connector.
        </p>
      )}

      {creating && (
        <div className="space-y-2 rounded border border-line p-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="text-xs text-mut">Name
              <input className="input mt-0.5 w-full"
                value={form.name} maxLength={40} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label className="text-xs text-mut">Strategy
              <select className="input mt-0.5 w-full"
                value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value })}>
                {strategies.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}
              </select>
            </label>
            <div className="text-xs text-mut">Money
              <div className="mt-1 flex gap-3 text-sm text-white">
                <label><input type="radio" checked={form.mode === "paper"}
                  onChange={() => setForm({ ...form, mode: "paper" })} /> Paper</label>
                <label className={realOk ? "" : "opacity-40"} title={realWhy ?? ""}>
                  <input type="radio" disabled={!realOk} checked={form.mode === "live"}
                    onChange={() => setForm({ ...form, mode: "live" })} /> Real money</label>
              </div>
              {realWhy && <div className="mt-0.5">real money: {realWhy}</div>}
            </div>
          </div>
          <p className="text-xs text-mut">{hint(strat?.last_7d)}</p>
          <LimitInputs value={limits} onChange={setLimits} />
          <p className="text-xs text-mut">
            A position closes at your take profit or stop loss (net %), or when the strategy closes it. Empty = no
            limit. {form.mode === "live" && "Real money: you sign once with your wallet, then fund the bot's own wallet."}
          </p>
          <button className="rounded bg-acc px-3 py-1 text-sm text-black disabled:opacity-40"
            disabled={busy || !form.name.trim() || (form.mode === "live" && !realOk)} onClick={() => void create()}>
            Create {form.mode === "live" ? "real-money" : "paper"} bot
          </button>
        </div>
      )}

      {msg && <p className="text-xs">{msg}</p>}

      {bots.map((b) => (
        <BotCard key={b.id} b={b} busy={busy} run={run} sign={sign} wallet={wallet}
          label={strategies.find((s) => s.name === b.strategy)?.label ?? b.strategy}
          fund={async (amount) => {
            if (!signTransaction) throw new Error("connect your wallet first");
            const built = await call<{ transactions: BuiltTx[] }>(`/v1/lpbots/${b.id}/fund-tx`, {
              method: "POST", body: JSON.stringify({ sol: amount }) });
            const sigs = await signAndSend(built.transactions, signTransaction, setMsg);
            return `Sent ${amount} SOL to "${b.name}". Tx ${sigs[0]?.slice(0, 10)}…`;
          }} />
      ))}
    </section>
  );
}

function BotCard({ b, label, busy, run, sign, wallet, fund }: {
  b: Bot;
  label: string;
  busy: boolean;
  wallet?: string;
  run: (what: () => Promise<string | void>) => Promise<void>;
  sign: (message: string) => Promise<{ pubkey: string; signature: string }>;
  fund: (amount: number) => Promise<string>;
}) {
  const [editing, setEditing] = useState(false);
  const [limits, setLimits] = useState<Limits>(limitsOf(b));
  const [amount, setAmount] = useState("0.3");
  const t = b.totals;
  const live = b.mode === "live";
  const isOwner = live && wallet === b.owner_wallet;
  const post = (path: string, body: unknown) =>
    call(`/v1/lpbots/${b.id}/${path}`, { method: "POST", body: JSON.stringify(body) });
  const act = (action: string) => run(async () => void (await post("action", { action })));
  const owner = (action: keyof typeof OWNER_ACTIONS) => run(async () => {
    const ts = Math.floor(Date.now() / 1000);
    const r = await post("owner", { action, ts, ...(await sign(`LP Dash bot ${b.id}: ${action} at ${ts}`)) }) as
      { sol?: number };
    return action === "withdraw" ? `Withdrew ${r.sol?.toFixed(4)} SOL to your wallet.` : `${OWNER_ACTIONS[action]}: done.`;
  });
  const p = b.pending;

  return (
    <div className="space-y-2 rounded border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{b.name}</span>
        <span className={`rounded px-1.5 py-0.5 text-xs ${live ? "bg-acc/20 text-acc" : "border border-line text-mut"}`}>
          {live ? "REAL MONEY" : "PAPER"}</span>
        <span className="text-xs text-mut">follows {label}</span>
        <span className={`text-xs ${b.status === "running" ? "text-pos" : "text-yellow-400"}`}>● {b.status}</span>
        <div className="ml-auto flex flex-wrap gap-1.5 text-xs">
          {b.status === "running" && <button disabled={busy} className="rounded border border-line px-2 py-0.5"
            onClick={() => void act("pause")}>Pause</button>}
          {b.status === "paused" && <button disabled={busy} className="rounded border border-line px-2 py-0.5"
            onClick={() => void act("resume")}>Resume</button>}
          {!live && ["running", "paused"].includes(b.status) && <button disabled={busy}
            className="rounded border border-neg px-2 py-0.5 text-neg" onClick={() => void act("stop")}>Stop</button>}
          {!live && b.status === "stopped" && <button disabled={busy} className="rounded border border-line px-2 py-0.5"
            onClick={() => void act("delete")}>Delete</button>}
          <button className="rounded border border-line px-2 py-0.5" onClick={() => setEditing(!editing)}>
            {editing ? "Close" : "Settings"}</button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <div><div className="text-xs text-mut">result ({t.closed ?? 0} closed, {t.wins ?? 0} won)</div>
          <span className={tone(t.pnl_sol)}>{sol(t.pnl_sol)}</span></div>
        <div><div className="text-xs text-mut">today · open</div>
          <span className={tone(t.day_sol)}>{sol(t.day_sol)}</span> · {t.open ?? 0}</div>
        <div><div className="text-xs text-mut">take profit · stop loss</div>
          {b.take_profit_pct ? `+${b.take_profit_pct}%` : "–"} · {b.stop_loss_pct ? `−${b.stop_loss_pct}%` : "–"}</div>
        <div><div className="text-xs text-mut">size · max open · daily loss</div>
          {b.position_sol} SOL · {b.max_open} · {b.day_loss_sol ? `${b.day_loss_sol} SOL` : "–"}</div>
      </div>

      {p && !p.applied && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-acc/50 bg-acc/10 px-2 py-1 text-xs">
          <span>Claude suggests {p.take_profit_pct ? `take profit +${p.take_profit_pct}%` : ""}
            {p.take_profit_pct && p.stop_loss_pct ? " · " : ""}{p.stop_loss_pct ? `stop loss −${p.stop_loss_pct}%` : ""}
            {p.reason ? `: ${p.reason}` : ""}</span>
          <button disabled={busy} className="ml-auto rounded bg-acc px-2 py-0.5 text-black"
            onClick={() => void run(async () => { await post("settings", { apply_suggestion: true }); return "Applied."; })}>
            Apply</button>
        </div>
      )}
      {p?.applied && p.reason && <p className="text-xs text-mut">Claude set these limits: {p.reason}</p>}
      {b.last_skip && <p className="text-xs text-mut">last skipped trade: {b.last_skip.why}</p>}

      {editing && (
        <div className="space-y-2">
          <LimitInputs value={limits} onChange={setLimits} />
          <button disabled={busy} className="rounded bg-acc px-3 py-1 text-sm text-black" onClick={() => void run(async () => {
            const body = limitsBody(limits);
            for (const k of ["position_sol", "max_open"]) if (body[k] == null) delete body[k];
            await post("settings", body);
            setEditing(false);
            return "Saved.";
          })}>Save</button>
        </div>
      )}

      {live && (
        <div className="space-y-2 rounded border border-line p-2 text-sm">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span><span className="text-xs text-mut">bot wallet </span>
              {b.wallet?.address ? <a className="font-mono hover:text-acc" target="_blank" rel="noreferrer"
                href={`https://solscan.io/account/${b.wallet.address}`}>{shortAddr(b.wallet.address, 6)}</a> : "–"}</span>
            <span><span className="text-xs text-mut">balance </span>
              {b.wallet?.sol == null ? "–" : `${b.wallet.sol.toFixed(4)} SOL`}</span>
            <span><span className="text-xs text-mut">owner </span><span className="font-mono">{shortAddr(b.owner_wallet, 6)}</span></span>
            {b.wallet?.live_enabled === false && <span className="text-xs text-yellow-400">real-money trading is off on the server</span>}
            {b.wallet?.error && <span className="text-xs text-neg">{b.wallet.error}</span>}
          </div>
          {isOwner ? (
            <div className="flex flex-wrap items-center gap-2">
              <input className="w-20 rounded border border-line bg-transparent px-2 py-1" value={amount}
                onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
              <span>SOL</span>
              <button className="rounded bg-acc px-3 py-1 text-black disabled:opacity-40"
                disabled={busy || !(Number(amount) > 0)} onClick={() => void run(() => fund(Number(amount)))}>
                Send to bot wallet</button>
              {(Object.keys(OWNER_ACTIONS) as (keyof typeof OWNER_ACTIONS)[]).map((a) => (
                <button key={a} disabled={busy} onClick={() => void owner(a)}
                  className={`rounded border px-3 py-1 disabled:opacity-40 ${a === "close_all" ? "border-neg text-neg" : "border-line"}`}>
                  {OWNER_ACTIONS[a]}</button>
              ))}
            </div>
          ) : (
            <p className="text-xs text-mut">Connect the owner wallet ({shortAddr(b.owner_wallet)}) to fund, close or withdraw.</p>
          )}
        </div>
      )}

      {b.positions.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-left text-mut">
            <tr><th className="py-1">Pool</th><th>shape</th><th>status</th><th className="text-right">net</th>
              <th className="text-right">P&amp;L</th>{live && <th className="text-right">txs</th>}</tr>
          </thead>
          <tbody>
            {b.positions.map((x) => (
              <tr key={x.id} className="border-t border-line" title={x.error ?? ""}>
                <td className="py-1">{x.name}</td>
                <td>{x.shape ?? "–"}</td>
                <td className={x.error && x.status !== "closed" ? "text-neg" : ""}>
                  {x.status}{x.exit_reason ? ` · ${x.exit_reason}` : ""}</td>
                <td className={`text-right ${tone(x.net_pct)}`}>{pct(x.net_pct, 1)}</td>
                <td className={`text-right ${tone(x.pnl_sol)}`}>
                  {x.pnl_sol == null ? (x.net_pct == null ? "–" : `~${sol(x.sol_size * x.net_pct / 100)}`) : sol(x.pnl_sol)}</td>
                {live && <td className="text-right">{x.txs.slice(-1).map((tx) => (
                  <a key={tx} className="text-acc hover:underline" href={`https://solscan.io/tx/${tx}`} target="_blank"
                    rel="noreferrer">{x.txs.length} ↗</a>))}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
