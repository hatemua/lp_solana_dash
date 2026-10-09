"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useWallet } from "@solana/wallet-adapter-react";
import { api } from "@/lib/api";
import { BuiltTx, signAndSend } from "@/lib/tx";
import { num, shortAddr, usd } from "@/lib/format";

interface Position {
  pool: string;
  position: string;
  tokenX: string | null;
  tokenY: string | null;
  lowerBinId: number;
  upperBinId: number;
  activeBinId: number;
  inRange: boolean;
  amountX: number;
  amountY: number;
  feeX: number;
  feeY: number;
  valueUsd: number | null;
  feesUsd: number | null;
  name: string | null;
}

export function Positions() {
  const { publicKey, signTransaction, connected } = useWallet();
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!publicKey) return;
    try {
      const r = await api<{ positions: Position[] }>(`/v1/wallet/${publicKey.toBase58()}/positions`);
      setPositions(r.positions);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [publicKey]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(p: Position, kind: "remove" | "claim") {
    if (!publicKey || !signTransaction) return;
    const say = (s: string) => setStatus((x) => ({ ...x, [p.position]: s }));
    try {
      say("Building…");
      const path = kind === "claim" ? "claim-fees" : "remove-liquidity";
      const body = kind === "claim"
        ? { user: publicKey.toBase58(), position: p.position }
        : { user: publicKey.toBase58(), position: p.position, bps: 10_000, claim_and_close: true };
      const built = await api<{ transactions: BuiltTx[] }>(`/v1/tx/${p.pool}/${path}`, { method: "POST", body: JSON.stringify(body) });
      const sigs = await signAndSend(built.transactions, signTransaction, say);
      say(`Done (${sigs.length} tx).`);
      await load();
    } catch (e) {
      say(`Error: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  if (!connected) return <div className="card p-6 text-mut">Connect a wallet to see your Meteora DLMM positions.</div>;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">My DLMM positions</h1>
        <span className="text-xs text-mut">{shortAddr(publicKey?.toBase58(), 6)}</span>
        <button className="btn ml-auto" onClick={load}>Refresh</button>
      </div>
      {error && <div className="card p-3 text-sm text-neg">{error}</div>}
      {positions === null ? <p className="text-mut">Loading…</p> : positions.length === 0 ? (
        <div className="card p-6 text-mut">No DLMM positions in this wallet. Open one from a <Link href="/" className="text-acc">pool page</Link>.</div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-xs text-mut">
              <tr className="border-b border-line text-right">
                <th className="px-2 py-2 text-left">Pool</th><th className="px-2">Range (bins)</th><th className="px-2">Status</th>
                <th className="px-2">Amount X / Y</th><th className="px-2">Value</th><th className="px-2">Unclaimed fees</th><th className="px-2" />
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.position} className="border-b border-line/60 text-right">
                  <td className="px-2 py-2 text-left">
                    <Link className="text-slate-100 hover:text-acc" href={`/pools/${p.pool}`}>{p.name ?? shortAddr(p.pool)}</Link>
                    <div className="text-[11px] text-mut">{shortAddr(p.position)}</div>
                  </td>
                  <td className="px-2">{p.lowerBinId} → {p.upperBinId}<div className="text-[11px] text-mut">active {p.activeBinId}</div></td>
                  <td className={`px-2 ${p.inRange ? "text-pos" : "text-neg"}`}>{p.inRange ? "in range" : "out of range"}</td>
                  <td className="px-2">{num(p.amountX, 2)} / {num(p.amountY, 4)}</td>
                  <td className="px-2">{usd(p.valueUsd, 2)}</td>
                  <td className="px-2">{usd(p.feesUsd, 2)}<div className="text-[11px] text-mut">{num(p.feeX, 3)} / {num(p.feeY, 5)}</div></td>
                  <td className="space-x-1 whitespace-nowrap px-2">
                    <button className="btn px-2 text-xs" onClick={() => act(p, "claim")}>Claim fees</button>
                    <button className="btn px-2 text-xs" onClick={() => act(p, "remove")}>Remove all</button>
                    {status[p.position] && <div className="mt-1 max-w-[260px] text-left text-[11px] text-mut">{status[p.position]}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
