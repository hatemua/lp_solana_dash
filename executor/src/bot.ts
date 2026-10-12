/**
 * Live bot wallet. Its key comes only from BOT_WALLET_SECRET in the server .env (a JSON byte array); it is read once
 * at start, removed from the environment and never logged or returned. Without it every /v1/bot route answers 503.
 *
 * Reachable on the internal network only (the bot service, and the API for status / fund / owner-signed control).
 * Money can leave this wallet only into Meteora positions and Jupiter swaps it owns, or to BOT_OWNER_WALLET.
 */
import { createPublicKey, verify } from "node:crypto";
import { createRequire } from "node:module";
import { ComputeBudgetProgram, Connection, Keypair, LAMPORTS_PER_SOL, PublicKey, SystemProgram, Transaction,
  VersionedTransaction } from "@solana/web3.js";
import { SHAPES } from "./lp.js";
import { HttpError } from "./wallet.js";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Dlmm = any;
const BN: any = createRequire(import.meta.url)("bn.js");

export const SOL_MINT = "So11111111111111111111111111111111111111112";
const JUP = process.env.JUPITER_URL ?? "https://api.jup.ag/swap/v1";
const MAX_SIDE = 34;                   // 69 bins = one position account
const PRIORITY_MICROLAMPORTS = 50_000;
const MAX_FUND_SOL = 5;

let bot: Keypair | null = null;
let owner: PublicKey | null = null;

export function loadBotKey(): void {
  const raw = process.env.BOT_WALLET_SECRET;
  delete process.env.BOT_WALLET_SECRET;
  if (raw) {
    try {
      bot = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(raw) as number[]));
    } catch {
      console.error("BOT_WALLET_SECRET is not a valid JSON byte array: live bot disabled");
    }
  }
  const o = process.env.BOT_OWNER_WALLET;
  if (o) {
    try {
      owner = new PublicKey(o);
    } catch {
      console.error("BOT_OWNER_WALLET is not a valid address");
    }
  }
}

function key(): Keypair {
  if (!bot) throw new HttpError(503, "no bot wallet configured");
  return bot;
}

// one wallet operation at a time: balances measured around an operation are only its own
let chain: Promise<unknown> = Promise.resolve();
function locked<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}

async function lamports(conn: Connection): Promise<number> {
  return conn.getBalance(key().publicKey, "confirmed");
}

async function tokenRaw(conn: Connection, mint: PublicKey): Promise<bigint> {
  const r = await conn.getParsedTokenAccountsByOwner(key().publicKey, { mint }, "confirmed");
  return r.value.reduce((s, a) => s + BigInt(a.account.data.parsed.info.tokenAmount.amount as string), 0n);
}

function withPriority(tx: Transaction): Transaction {
  const has = tx.instructions.some((ix) => ix.programId.equals(ComputeBudgetProgram.programId) && ix.data[0] === 3);
  if (!has) tx.instructions.unshift(ComputeBudgetProgram.setComputeUnitPrice({ microLamports: PRIORITY_MICROLAMPORTS }));
  return tx;
}

async function confirm(conn: Connection, sig: string, blockhash: string, lastValidBlockHeight: number) {
  const c = await conn.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight }, "confirmed");
  if (c.value.err) throw new HttpError(502, `transaction ${sig} failed: ${JSON.stringify(c.value.err)}`);
}

async function sendLegacy(conn: Connection, tx: Transaction, extra: Keypair[] = []): Promise<string> {
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  withPriority(tx);
  tx.feePayer = key().publicKey;
  tx.recentBlockhash = blockhash;
  tx.sign(key(), ...extra.filter((k) => tx.signatures.some((s) => s.publicKey.equals(k.publicKey))));
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 5 });
  await confirm(conn, sig, blockhash, lastValidBlockHeight);
  return sig;
}

/** Jupiter swap, signed by the bot. Returns the signature, or null when there is nothing to swap. */
async function swap(conn: Connection, input: string, output: string, amount: bigint, slippageBps: number) {
  if (amount <= 0n) return null;
  const q = await fetch(`${JUP}/quote?inputMint=${input}&outputMint=${output}&amount=${amount}` +
    `&slippageBps=${slippageBps}&restrictIntermediateTokens=true`);
  if (!q.ok) throw new HttpError(502, `jupiter quote ${q.status}: ${(await q.text()).slice(0, 200)}`);
  const quoteResponse = await q.json();
  const s = await fetch(`${JUP}/swap`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      quoteResponse, userPublicKey: key().publicKey.toBase58(), wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
      prioritizationFeeLamports: { priorityLevelWithMaxLamports: { maxLamports: 300_000, priorityLevel: "high" } },
    }),
  });
  if (!s.ok) throw new HttpError(502, `jupiter swap ${s.status}: ${(await s.text()).slice(0, 200)}`);
  const { swapTransaction, lastValidBlockHeight } = await s.json() as { swapTransaction: string; lastValidBlockHeight: number };
  const tx = VersionedTransaction.deserialize(Buffer.from(swapTransaction, "base64"));
  tx.sign([key()]);
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 5 });
  await confirm(conn, sig, tx.message.recentBlockhash, lastValidBlockHeight);
  return { sig, outAmount: String(quoteResponse.outAmount) };
}

function solPool(dlmm: Dlmm): PublicKey {
  if (dlmm.tokenY.publicKey.toBase58() !== SOL_MINT) throw new HttpError(400, "live bot trades TOKEN-SOL pools only");
  return dlmm.tokenX.publicKey as PublicKey;
}

async function addPosition(conn: Connection, dlmm: Dlmm, x: bigint, y: bigint, minBin: number, maxBin: number) {
  const position = Keypair.generate();
  const tx: Transaction = await dlmm.initializePositionAndAddLiquidityByStrategy({
    positionPubKey: position.publicKey, user: key().publicKey,
    totalXAmount: new BN(x.toString()), totalYAmount: new BN(y.toString()),
    strategy: { minBinId: minBin, maxBinId: maxBin, strategyType: SHAPES.spot }, slippage: 1,
  });
  const sig = await sendLegacy(conn, tx, [position]);
  return { position: position.publicKey.toBase58(), sig };
}

/** Remove everything, claim the fees and close the position account (the rent comes back). */
async function removeAll(conn: Connection, dlmm: Dlmm, position: string): Promise<string[]> {
  let pos: any;
  try {
    pos = await dlmm.getPosition(new PublicKey(position));
  } catch {
    return [];                                        // already closed
  }
  const own = pos.positionData.owner as PublicKey | undefined;
  if (own && !own.equals(key().publicKey)) throw new HttpError(403, "not a bot position");
  const txs: Transaction[] = await dlmm.removeLiquidity({
    user: key().publicKey, position: pos.publicKey, fromBinId: pos.positionData.lowerBinId,
    toBinId: pos.positionData.upperBinId, bps: new BN(10_000), shouldClaimAndClose: true,
  });
  const sigs = [];
  for (const tx of Array.isArray(txs) ? txs : [txs]) sigs.push(await sendLegacy(conn, tx));
  return sigs;
}

async function measured<T extends object>(conn: Connection, fn: () => Promise<T>) {
  return locked(async () => {
    const before = await lamports(conn);
    const out = await fn();
    const after = await lamports(conn);
    return { ...out, sol_before: before / LAMPORTS_PER_SOL, sol_after: after / LAMPORTS_PER_SOL,
      sol_delta: (after - before) / LAMPORTS_PER_SOL };
  });
}

/** Spot position of `sol` SOL, +/- `each_side` bins around the active bin: half is swapped into the token first. */
export function open(conn: Connection, dlmm: Dlmm, body: { sol: number; each_side: number; slippage_bps?: number }) {
  const sol = Number(body.sol);
  if (!(sol > 0 && sol <= 2)) throw new HttpError(400, "sol must be in (0, 2]");
  const n = Math.max(1, Math.min(MAX_SIDE, Math.floor(Number(body.each_side) || 0)));
  return measured(conn, async () => {
    const mint = solPool(dlmm);
    const half = BigInt(Math.floor(sol * LAMPORTS_PER_SOL / 2));
    const had = await tokenRaw(conn, mint);
    const bought = await swap(conn, SOL_MINT, mint.toBase58(), half, Math.min(body.slippage_bps ?? 300, 500));
    const x = (await tokenRaw(conn, mint)) - had;
    if (x <= 0n) throw new HttpError(502, "the swap returned no tokens");
    await dlmm.refetchStates();
    const active = (await dlmm.getActiveBin()).binId as number;
    try {
      const { position, sig } = await addPosition(conn, dlmm, x, half, active - n, active + n);
      return { position, mint: mint.toBase58(), active_bin: active, min_bin: active - n, max_bin: active + n,
        tokens_raw: x.toString(), txs: [bought?.sig, sig] };
    } catch (e) {                                       // don't keep the tokens of a position that failed
      await swap(conn, mint.toBase58(), SOL_MINT, await tokenRaw(conn, mint), 800).catch(() => null);
      throw e;
    }
  });
}

/** Soft exit: close the position and offer all its tokens in `bins` ask bins right above the price. */
export function unwind(conn: Connection, dlmm: Dlmm, body: { position: string; bins: number }) {
  const k = Math.max(1, Math.min(10, Math.floor(Number(body.bins) || 5)));
  return measured(conn, async () => {
    const mint = solPool(dlmm);
    const txs = await removeAll(conn, dlmm, body.position);
    const x = await tokenRaw(conn, mint);
    if (x <= 0n) return { position: null, txs };
    await dlmm.refetchStates();
    const active = (await dlmm.getActiveBin()).binId as number;
    const { position, sig } = await addPosition(conn, dlmm, x, 0n, active + 1, active + k);
    return { position, tokens_raw: x.toString(), min_bin: active + 1, max_bin: active + k, txs: [...txs, sig] };
  });
}

/** Close the given positions of this pool, then swap every token of the pool back to SOL. */
export function close(conn: Connection, dlmm: Dlmm, body: { positions: string[]; slippage_bps?: number }) {
  const list = (body.positions ?? []).filter((p) => typeof p === "string").slice(0, 4);
  return measured(conn, async () => {
    const mint = solPool(dlmm);
    const txs: (string | undefined)[] = [];
    for (const p of list) txs.push(...await removeAll(conn, dlmm, p));
    const x = await tokenRaw(conn, mint);
    const sold = await swap(conn, mint.toBase58(), SOL_MINT, x, Math.min(body.slippage_bps ?? 800, 1500));
    txs.push(sold?.sig);
    return { tokens_sold_raw: x.toString(), txs: txs.filter(Boolean) };
  });
}

export async function wallet(conn: Connection) {
  if (!bot) return { enabled: false, address: null, sol: null, owner: owner?.toBase58() ?? null };
  return { enabled: true, address: bot.publicKey.toBase58(), sol: (await lamports(conn)) / LAMPORTS_PER_SOL,
    owner: owner?.toBase58() ?? null };
}

/** Unsigned transfer from the user's wallet to the bot wallet: the user signs it in their own wallet. */
export async function fundTx(conn: Connection, body: { user: string; sol: number }) {
  const user = new PublicKey(body.user);
  const sol = Number(body.sol);
  if (!(sol > 0 && sol <= MAX_FUND_SOL)) throw new HttpError(400, `sol must be in (0, ${MAX_FUND_SOL}]`);
  const tx = new Transaction().add(SystemProgram.transfer({
    fromPubkey: user, toPubkey: key().publicKey, lamports: Math.round(sol * LAMPORTS_PER_SOL) }));
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  tx.feePayer = user;
  tx.recentBlockhash = blockhash;
  return { to: key().publicKey.toBase58(), lastValidBlockHeight, transactions: [{
    transaction: tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString("base64"),
    simulation: null }] };
}

/** Send SOL back to the owner wallet (the only destination). `sol` omitted = everything except the fee. */
export function withdraw(conn: Connection, body: { sol?: number }) {
  if (!owner) throw new HttpError(503, "BOT_OWNER_WALLET is not set");
  const to = owner;
  return locked(async () => {
    const bal = await lamports(conn);
    const max = bal - 10_000;
    const amt = body.sol ? Math.min(Math.round(Number(body.sol) * LAMPORTS_PER_SOL), max) : max;
    if (amt <= 0) throw new HttpError(400, "nothing to withdraw");
    const sig = await sendLegacy(conn, new Transaction().add(SystemProgram.transfer({
      fromPubkey: key().publicKey, toPubkey: to, lamports: amt })));
    return { to: to.toBase58(), sol: amt / LAMPORTS_PER_SOL, txs: [sig] };
  });
}

const ED25519_SPKI = Buffer.from("302a300506032b6570032100", "hex");

/** Is `signature` (base64) the owner wallet's signature of `message`? */
export function verifyOwner(body: { pubkey: string; message: string; signature: string }) {
  if (!owner) return { ok: false, error: "BOT_OWNER_WALLET is not set" };
  let pk: PublicKey;
  try {
    pk = new PublicKey(body.pubkey);
  } catch {
    return { ok: false, error: "bad pubkey" };
  }
  if (!pk.equals(owner)) return { ok: false, error: "only the owner wallet controls the bot" };
  const keyObj = createPublicKey({ key: Buffer.concat([ED25519_SPKI, pk.toBuffer()]), format: "der", type: "spki" });
  const ok = verify(null, Buffer.from(String(body.message), "utf8"), keyObj, Buffer.from(String(body.signature), "base64"));
  return ok ? { ok: true } : { ok: false, error: "bad signature" };
}

