"use client";

import { useCallback, useEffect, useState } from "react";
import { useWallet } from "@solana/wallet-adapter-react";
import { api } from "@/lib/api";
import { pct, shortAddr } from "@/lib/format";
import { BuiltTx, signAndSend } from "@/lib/tx";
import { WalletButton } from "./WalletButton";

interface LivePosition {
  id: number;
  paper_id: number;
  pool: string;
  name: string;
  status: string;
  opened_at: string;
  closed_at: string | null;
  sol_size: number;
  pnl_sol: number | null;
  exit_reason: string | null;
  txs: string[];
  error: string | null;
  paper_net_pct: number | null;
  shape: string | null;
}

interface LiveStatus {
  wallet: { enabled: boolean; address?: string | null; sol?: number | null; owner?: string | null; error?: string };
  control: { paused?: boolean; close_all?: boolean };
  positions: LivePosition[];
  totals: { closed: number; wins: number; pnl_sol: number; open: number };
}

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

const ACTIONS = {
  pause: "Pause new trades",
  resume: "Resume",
  close_all: "Close all now",
  withdraw: "Withdraw all to my wallet",
} as const;

export function LiveBot() {
  const { publicKey, signTransaction, signMessage } = useWallet();
  const [s, setS] = useState<LiveStatus | null>(null);
  const [amount, setAmount] = useState("0.3");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setS(await api<LiveStatus>("/v1/live/status"));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 30_000);
    return () => clearInterval(id);
  }, [load]);

  async function fund() {
    if (!publicKey || !signTransaction) return;
    setBusy(true);
    try {
      const built = await api<{ transactions: BuiltTx[] }>("/v1/live/fund-tx", {
        method: "POST", body: JSON.stringify({ user: publicKey.toBase58(), sol: Number(amount) }),
      });
      const sigs = await signAndSend(built.transactions, signTransaction, setMsg);
      setMsg(`Sent ${amount} SOL to the bot wallet. Tx ${sigs[0]?.slice(0, 10)}…`);
      await load();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function control(action: keyof typeof ACTIONS) {
    if (!publicKey || !signMessage) return;
    setBusy(true);
    try {
      const ts = Math.floor(Date.now() / 1000);
      setMsg("Sign the message in your wallet (it costs nothing and sends no transaction)…");
      const sig = await signMessage(new TextEncoder().encode(`LP Dash bot: ${action} at ${ts}`));
      const r = await api<{ ok?: boolean; sol?: number; txs?: string[] }>("/v1/live/control", {
        method: "POST",
        body: JSON.stringify({ action, ts, pubkey: publicKey.toBase58(), signature: toBase64(sig) }),
      });
      setMsg(action === "withdraw" ? `Withdrew ${r.sol?.toFixed(4)} SOL to your wallet.` : `${ACTIONS[action]}: done.`);
      await load();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  if (!s) return null;
  const w = s.wallet;
  const isOwner = Boolean(publicKey && w.owner && publicKey.toBase58() === w.owner);
  const state = !w.enabled ? "no wallet yet" : s.control.paused ? "paused" : "on";
  const pnl = s.totals.pnl_sol;

  return (
    <section className="card space-y-3 border-acc/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">Live bot (real money)</h2>
        <span className={`rounded px-1.5 py-0.5 text-xs ${state === "on" ? "bg-pos/15 text-pos" : "bg-yellow-500/15 text-yellow-400"}`}>
          {state}
        </span>
        <span className="text-xs text-mut">mirrors the Auto strategy · 0.1 SOL per trade · 1 at a time · stops for the day at −0.05 SOL</span>
        <div className="ml-auto"><WalletButton /></div>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <div>
          <div className="text-xs text-mut">bot wallet</div>
          {w.address ? (
            <a className="font-mono hover:text-acc" href={`https://solscan.io/account/${w.address}`} target="_blank"
              rel="noreferrer" title={w.address}>{shortAddr(w.address, 6)}</a>
          ) : "–"}
        </div>
        <div><div className="text-xs text-mut">balance</div>{w.sol == null ? "–" : `${w.sol.toFixed(4)} SOL`}</div>
        <div>
          <div className="text-xs text-mut">result ({s.totals.closed} closed, {s.totals.wins} won)</div>
          <span className={pnl > 0 ? "text-pos" : pnl < 0 ? "text-neg" : ""}>{pnl >= 0 ? "+" : ""}{pnl.toFixed(4)} SOL</span>
        </div>
        <div><div className="text-xs text-mut">owner</div><span className="font-mono">{shortAddr(w.owner, 6)}</span></div>
      </div>

      {w.enabled && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-mut">Fund the bot from your connected wallet:</span>
          <input className="w-20 rounded border border-line bg-transparent px-2 py-1" value={amount}
            onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
          <span>SOL</span>
          <button className="rounded bg-acc px-3 py-1 text-black disabled:opacity-40"
            disabled={busy || !publicKey || !(Number(amount) > 0)} onClick={() => void fund()}>
            {publicKey ? "Send to bot wallet" : "Connect a wallet first"}
          </button>
        </div>
      )}

      {isOwner && (
        <div className="flex flex-wrap gap-2 text-sm">
          {(Object.keys(ACTIONS) as (keyof typeof ACTIONS)[])
            .filter((a) => (a === "pause" ? !s.control.paused : a === "resume" ? s.control.paused : true))
            .map((a) => (
              <button key={a} disabled={busy} onClick={() => void control(a)}
                className={`rounded border px-3 py-1 disabled:opacity-40 ${a === "close_all" ? "border-neg text-neg" : "border-line"}`}>
                {ACTIONS[a]}
              </button>
            ))}
        </div>
      )}
      {publicKey && w.owner && !isOwner && (
        <p className="text-xs text-mut">Only the owner wallet ({shortAddr(w.owner)}) can pause, close or withdraw.</p>
      )}
      {msg && <p className="text-xs">{msg}</p>}

      {s.positions.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-left text-mut">
            <tr><th className="py-1">Pool</th><th>shape</th><th>status</th><th className="text-right">real</th>
              <th className="text-right">paper twin</th><th className="text-right">txs</th></tr>
          </thead>
          <tbody>
            {s.positions.map((p) => (
              <tr key={p.id} className="border-t border-line" title={p.error ?? ""}>
                <td className="py-1">{p.name}</td>
                <td>{p.shape ?? "–"}</td>
                <td className={p.error && p.status !== "closed" ? "text-neg" : ""}>{p.status}{p.exit_reason ? ` · ${p.exit_reason}` : ""}</td>
                <td className={`text-right ${(p.pnl_sol ?? 0) > 0 ? "text-pos" : (p.pnl_sol ?? 0) < 0 ? "text-neg" : ""}`}>
                  {p.pnl_sol == null ? "–" : `${p.pnl_sol >= 0 ? "+" : ""}${p.pnl_sol.toFixed(4)} SOL (${pct(p.pnl_sol / p.sol_size * 100, 1)})`}
                </td>
                <td className="text-right">{pct(p.paper_net_pct, 1)}</td>
                <td className="text-right">
                  {p.txs.slice(-1).map((t) => (
                    <a key={t} className="text-acc hover:underline" href={`https://solscan.io/tx/${t}`} target="_blank" rel="noreferrer">
                      {p.txs.length} ↗
                    </a>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
