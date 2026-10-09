"use client";

import { useEffect, useMemo, useState } from "react";
import { useWallet } from "@solana/wallet-adapter-react";
import { api } from "@/lib/api";
import { amountsFor, priceAt, rangeFor, Side } from "@/lib/range";
import { BuiltTx, signAndSend } from "@/lib/tx";
import { price as fmtPrice } from "@/lib/format";

interface Quote {
  activeBinId: number;
  positionRentSol: number;
  newBinArrays: number;
  binArrayRentSol: number;
  transactions: number;
  estNetworkFeeSol: number;
}

const SHAPES = [
  ["spot", "Spot", "equal liquidity in every bin"],
  ["curve", "Curve", "more liquidity near the price"],
  ["bidask", "Bid-ask", "more liquidity at the edges"],
] as const;

export function AddLiquidity({ pool, activeBin, binStep, price, tokenIsX, tokenSymbol, onRange }: {
  pool: string; activeBin: number; binStep: number; price: number; tokenIsX: boolean; tokenSymbol: string;
  decimalsX: number; onRange: (r: { min: number; max: number } | null) => void;
}) {
  const { publicKey, signTransaction, connected } = useWallet();
  const [side, setSide] = useState<Side>("sol");
  const [width, setWidth] = useState(20);
  const [shape, setShape] = useState<"spot" | "curve" | "bidask">("spot");
  const [amount, setAmount] = useState("0.1");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const range = useMemo(() => rangeFor(side, width, activeBin, binStep, tokenIsX), [side, width, activeBin, binStep, tokenIsX]);
  const solPerToken = tokenIsX ? price : price > 0 ? 1 / price : 0;
  const amt = Number(amount) || 0;
  const { x, y } = amountsFor(side, amt, tokenIsX, solPerToken || 1);

  useEffect(() => {
    onRange(range);
    return () => onRange(null);
  }, [range, onRange]);

  useEffect(() => {
    if (!publicKey) return;
    const t = setTimeout(() => {
      api<Quote>(`/v1/tx/${pool}/quote?user=${publicKey.toBase58()}&min_bin_id=${range.min}&max_bin_id=${range.max}`)
        .then(setQuote).catch(() => setQuote(null));
    }, 400);
    return () => clearTimeout(t);
  }, [pool, publicKey, range]);

  const low = priceAt(price, activeBin, range.min, binStep);
  const high = priceAt(price, activeBin, range.max, binStep);

  async function submit() {
    if (!publicKey || !signTransaction) return;
    setBusy(true);
    setStatus("Building the transaction…");
    try {
      const built = await api<{ position: string; transactions: BuiltTx[] }>(`/v1/tx/${pool}/add-liquidity`, {
        method: "POST",
        body: JSON.stringify({ user: publicKey.toBase58(), amount_x: x, amount_y: y, min_bin_id: range.min,
          max_bin_id: range.max, shape }),
      });
      const bad = built.transactions.find((t) => t.simulation && !t.simulation.ok);
      if (bad) throw new Error(`simulation failed: ${JSON.stringify(bad.simulation?.error)} ${(bad.simulation?.logs ?? []).join(" ")}`);
      const sigs = await signAndSend(built.transactions, signTransaction, setStatus);
      setStatus(`Done. Position ${built.position.slice(0, 6)}… opened. Tx ${sigs.at(-1)?.slice(0, 10)}…`);
    } catch (e) {
      setStatus(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  const solCost = (quote?.positionRentSol ?? 0.0574) + (quote?.binArrayRentSol ?? 0) + (quote?.estNetworkFeeSol ?? 0);
  return (
    <div className="card space-y-3 p-3">
      <div className="text-sm font-medium">Add liquidity <span className="text-xs font-normal text-mut">(signed in your wallet)</span></div>
      <div className="grid grid-cols-3 gap-1">
        {([["sol", "SOL only"], ["token", `${tokenSymbol} only`], ["both", "50/50"]] as const).map(([k, l]) => (
          <button key={k} className={`btn px-1 text-xs ${side === k ? "border-acc text-acc" : ""}`} onClick={() => setSide(k)}>{l}</button>
        ))}
      </div>
      <label className="block space-y-1">
        <span className="label">Range width: {side === "both" ? "±" : side === "sol" ? "−" : "+"}{width}%</span>
        <input type="range" min={2} max={side === "both" ? 30 : 90} value={width} onChange={(e) => setWidth(Number(e.target.value))} className="w-full" />
      </label>
      <div className="text-xs text-mut num">
        Bins {range.min} → {range.max} ({range.max - range.min + 1} bins) · price {fmtPrice(low)} → {fmtPrice(high)}
      </div>
      <div className="grid grid-cols-3 gap-1">
        {SHAPES.map(([k, l, d]) => (
          <button key={k} title={d} className={`btn px-1 text-xs ${shape === k ? "border-acc text-acc" : ""}`} onClick={() => setShape(k)}>{l}</button>
        ))}
      </div>
      <label className="block space-y-1">
        <span className="label">Amount ({side === "token" ? tokenSymbol : "SOL"}{side === "both" ? ", half is swapped value" : ""})</span>
        <input className="input num" value={amount} inputMode="decimal" onChange={(e) => setAmount(e.target.value)} />
      </label>
      <div className="rounded border border-line p-2 text-xs text-mut num">
        <div className="flex justify-between"><span>Deposit X / Y</span><span>{x.toPrecision(4)} / {y.toPrecision(4)}</span></div>
        <div className="flex justify-between"><span>Position rent (refunded on close)</span><span>{(quote?.positionRentSol ?? 0.0574).toFixed(4)} SOL</span></div>
        <div className="flex justify-between"><span>New bin arrays (not refunded)</span><span>{quote ? `${quote.newBinArrays} · ${quote.binArrayRentSol.toFixed(4)} SOL` : "connect wallet"}</span></div>
        <div className="flex justify-between"><span>Network fees</span><span>{(quote?.estNetworkFeeSol ?? 0.00001).toFixed(5)} SOL</span></div>
        {side === "both" && <div className="mt-1 text-[11px]">50/50 needs both tokens in your wallet; swap first if you only hold SOL (swap cost not included).</div>}
        <div className="mt-1 flex justify-between text-slate-200"><span>SOL needed besides the deposit</span><span>{solCost.toFixed(4)} SOL</span></div>
      </div>
      <button className="btn-primary w-full" disabled={!connected || busy || amt <= 0} onClick={submit}>
        {connected ? (busy ? "Working…" : "Add liquidity") : "Connect a wallet first"}
      </button>
      {status && <p className="break-words text-xs text-mut">{status}</p>}
    </div>
  );
}
