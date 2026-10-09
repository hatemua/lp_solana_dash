"use client";

import { Transaction } from "@solana/web3.js";
import { api } from "./api";

export interface BuiltTx { transaction: string; simulation: { ok: boolean; error: unknown; logs: string[] | null } | null }

type SignFn = (tx: Transaction) => Promise<Transaction>;

function fromBase64(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

/** Sign each server-built transaction in the user's wallet, then relay it. Returns the signatures. */
export async function signAndSend(txs: BuiltTx[], sign: SignFn, onStatus: (s: string) => void): Promise<string[]> {
  const sigs: string[] = [];
  for (const [i, t] of txs.entries()) {
    const tx = Transaction.from(fromBase64(t.transaction));
    onStatus(`Sign transaction ${i + 1} of ${txs.length} in your wallet…`);
    const signed = await sign(tx);
    onStatus(`Sending transaction ${i + 1} of ${txs.length}…`);
    const r = await api<{ signature: string; confirmed: boolean; error: unknown }>("/v1/tx/send", {
      method: "POST",
      body: JSON.stringify({ transaction: toBase64(signed.serialize()) }),
    });
    if (!r.confirmed) throw new Error(`transaction failed: ${JSON.stringify(r.error)}`);
    sigs.push(r.signature);
  }
  return sigs;
}
