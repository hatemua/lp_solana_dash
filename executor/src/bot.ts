/**
 * Users' real-money bots. Each bot has its own wallet, derived when needed from BOT_MASTER_SEED (32 bytes, hex, in the
 * server .env only) and from the bot id and its owner's wallet:
 *   seed = HMAC-SHA256(master, "lp-dash-bot:{bot_id}:{owner}")
 * Keys are never stored, logged or returned. A wallet is bound to its owner by construction: a request naming another
 * owner derives another (empty) wallet. Without the master seed every /v1/bot route answers 503.
 *
 * Reachable on the internal network only (the bot service, and the API for status / fund / owner-signed withdraw).
 * Money can leave a bot wallet only into Meteora positions and Jupiter swaps it owns, or to its owner wallet.
 * New positions and funding need LIVE_ENABLED=true and the owner in LIVE_WALLETS; closing always works.
 */
import { createHmac, createPublicKey, verify } from "node:crypto";
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
const MAX_SIGNED_AGE_S = 120;

let master: Buffer | null = null;
let liveEnabled = false;
let allowed = new Set<string>();

export function loadBotKey(): void {
  const raw = (process.env.BOT_MASTER_SEED ?? "").trim();
  delete process.env.BOT_MASTER_SEED;
  if (raw) {
    if (/^[0-9a-f]{64}$/i.test(raw)) master = Buffer.from(raw, "hex");
    else console.error("BOT_MASTER_SEED must be 64 hex characters: real-money bots disabled");
  }
  liveEnabled = /^(1|true|yes)$/i.test(process.env.LIVE_ENABLED ?? "");
  allowed = new Set((process.env.LIVE_WALLETS ?? "").split(",").map((s) => s.trim()).filter(Boolean));
}

/** The bot's keypair: a pure function of the master seed, the bot id and the owner wallet. */
export function deriveKeypair(seed: Buffer, botId: number, owner: string): Keypair {
  const d = createHmac("sha256", seed).update(`lp-dash-bot:${botId}:${owner}`).digest();
  return Keypair.fromSeed(new Uint8Array(d));
}

export interface Who { bot_id: number; owner: string }

function who(body: Partial<Who>): { id: number; owner: PublicKey } {
  const id = Number(body.bot_id);
  if (!Number.isInteger(id) || id < 1) throw new HttpError(400, "bot_id must be a positive integer");
  let owner: PublicKey;
  try {
    owner = new PublicKey(String(body.owner));
  } catch {
    throw new HttpError(400, "owner must be a wallet address");
  }
  return { id, owner };
}

function key(body: Partial<Who>): Keypair {
  if (!master) throw new HttpError(503, "real-money bots are not configured (BOT_MASTER_SEED)");
  const w = who(body);
  return deriveKeypair(master, w.id, w.owner.toBase58());
}

function mayTrade(body: Partial<Who>): void {
  if (!liveEnabled) throw new HttpError(403, "real-money trading is switched off (LIVE_ENABLED)");
  if (!allowed.has(who(body).owner.toBase58())) throw new HttpError(403, "this wallet is not allowed real-money bots");
}

export function access(owner: string) {
  return { configured: master !== null, live_enabled: liveEnabled, allowed: allowed.has(owner) };
}

// one wallet operation at a time (all bots): balances measured around an operation are only its own
let chain: Promise<unknown> = Promise.resolve();
function locked<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}

async function lamports(conn: Connection, kp: Keypair): Promise<number> {
  return conn.getBalance(kp.publicKey, "confirmed");
}

async function tokenRaw(conn: Connection, kp: Keypair, mint: PublicKey): Promise<bigint> {
  const r = await conn.getParsedTokenAccountsByOwner(kp.publicKey, { mint }, "confirmed");
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

async function sendLegacy(conn: Connection, kp: Keypair, tx: Transaction, extra: Keypair[] = []): Promise<string> {
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  withPriority(tx);
  tx.feePayer = kp.publicKey;
  tx.recentBlockhash = blockhash;
  tx.sign(kp, ...extra.filter((k) => tx.signatures.some((s) => s.publicKey.equals(k.publicKey))));
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 5 });
  await confirm(conn, sig, blockhash, lastValidBlockHeight);
  return sig;
}

/** Jupiter swap, signed by the bot. Returns the signature, or null when there is nothing to swap. */
async function swap(conn: Connection, kp: Keypair, input: string, output: string, amount: bigint, slippageBps: number) {
  if (amount <= 0n) return null;
  const q = await fetch(`${JUP}/quote?inputMint=${input}&outputMint=${output}&amount=${amount}` +
    `&slippageBps=${slippageBps}&restrictIntermediateTokens=true`);
  if (!q.ok) throw new HttpError(502, `jupiter quote ${q.status}: ${(await q.text()).slice(0, 200)}`);
  const quoteResponse = await q.json();
  const s = await fetch(`${JUP}/swap`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      quoteResponse, userPublicKey: kp.publicKey.toBase58(), wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
      prioritizationFeeLamports: { priorityLevelWithMaxLamports: { maxLamports: 300_000, priorityLevel: "high" } },
    }),
  });
  if (!s.ok) throw new HttpError(502, `jupiter swap ${s.status}: ${(await s.text()).slice(0, 200)}`);
  const { swapTransaction, lastValidBlockHeight } = await s.json() as { swapTransaction: string; lastValidBlockHeight: number };
  const tx = VersionedTransaction.deserialize(Buffer.from(swapTransaction, "base64"));
  tx.sign([kp]);
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 5 });
  await confirm(conn, sig, tx.message.recentBlockhash, lastValidBlockHeight);
  return { sig, outAmount: String(quoteResponse.outAmount) };
}

function solPool(dlmm: Dlmm): PublicKey {
  if (dlmm.tokenY.publicKey.toBase58() !== SOL_MINT) throw new HttpError(400, "live bot trades TOKEN-SOL pools only");
  return dlmm.tokenX.publicKey as PublicKey;
}

async function addPosition(conn: Connection, kp: Keypair, dlmm: Dlmm, x: bigint, y: bigint, minBin: number,
  maxBin: number) {
  const position = Keypair.generate();
  const tx: Transaction = await dlmm.initializePositionAndAddLiquidityByStrategy({
    positionPubKey: position.publicKey, user: kp.publicKey,
    totalXAmount: new BN(x.toString()), totalYAmount: new BN(y.toString()),
    strategy: { minBinId: minBin, maxBinId: maxBin, strategyType: SHAPES.spot }, slippage: 1,
  });
  const sig = await sendLegacy(conn, kp, tx, [position]);
  return { position: position.publicKey.toBase58(), sig };
}

/** Remove everything, claim the fees and close the position account (the rent comes back). */
async function removeAll(conn: Connection, kp: Keypair, dlmm: Dlmm, position: string): Promise<string[]> {
  let pos: any;
  try {
    pos = await dlmm.getPosition(new PublicKey(position));
  } catch {
    return [];                                        // already closed
  }
  const own = pos.positionData.owner as PublicKey | undefined;
  if (own && !own.equals(kp.publicKey)) throw new HttpError(403, "not a bot position");
  const txs: Transaction[] = await dlmm.removeLiquidity({
    user: kp.publicKey, position: pos.publicKey, fromBinId: pos.positionData.lowerBinId,
    toBinId: pos.positionData.upperBinId, bps: new BN(10_000), shouldClaimAndClose: true,
  });
  const sigs = [];
  for (const tx of Array.isArray(txs) ? txs : [txs]) sigs.push(await sendLegacy(conn, kp, tx));
  return sigs;
}

async function measured<T extends object>(conn: Connection, kp: Keypair, fn: () => Promise<T>) {
  return locked(async () => {
    const before = await lamports(conn, kp);
    const out = await fn();
    const after = await lamports(conn, kp);
    return { ...out, sol_before: before / LAMPORTS_PER_SOL, sol_after: after / LAMPORTS_PER_SOL,
      sol_delta: (after - before) / LAMPORTS_PER_SOL };
  });
}

/** Spot position of `sol` SOL, +/- `each_side` bins around the active bin: half is swapped into the token first. */
export function open(conn: Connection, dlmm: Dlmm, body: Who & { sol: number; each_side: number; slippage_bps?: number }) {
  mayTrade(body);
  const kp = key(body);
  const sol = Number(body.sol);
  if (!(sol > 0 && sol <= 2)) throw new HttpError(400, "sol must be in (0, 2]");
  const n = Math.max(1, Math.min(MAX_SIDE, Math.floor(Number(body.each_side) || 0)));
  return measured(conn, kp, async () => {
    const mint = solPool(dlmm);
    const half = BigInt(Math.floor(sol * LAMPORTS_PER_SOL / 2));
    const had = await tokenRaw(conn, kp, mint);
    const bought = await swap(conn, kp, SOL_MINT, mint.toBase58(), half, Math.min(body.slippage_bps ?? 300, 500));
    const x = (await tokenRaw(conn, kp, mint)) - had;
    if (x <= 0n) throw new HttpError(502, "the swap returned no tokens");
    await dlmm.refetchStates();
    const active = (await dlmm.getActiveBin()).binId as number;
    try {
      const { position, sig } = await addPosition(conn, kp, dlmm, x, half, active - n, active + n);
      return { position, mint: mint.toBase58(), active_bin: active, min_bin: active - n, max_bin: active + n,
        tokens_raw: x.toString(), txs: [bought?.sig, sig] };
    } catch (e) {                                       // don't keep the tokens of a position that failed
      await swap(conn, kp, mint.toBase58(), SOL_MINT, await tokenRaw(conn, kp, mint), 800).catch(() => null);
      throw e;
    }
  });
}

/** Soft exit: close the position and offer all its tokens in `bins` ask bins right above the price. */
export function unwind(conn: Connection, dlmm: Dlmm, body: Who & { position: string; bins: number }) {
  const kp = key(body);
  const k = Math.max(1, Math.min(10, Math.floor(Number(body.bins) || 5)));
  return measured(conn, kp, async () => {
    const mint = solPool(dlmm);
    const txs = await removeAll(conn, kp, dlmm, body.position);
    const x = await tokenRaw(conn, kp, mint);
    if (x <= 0n) return { position: null, txs };
    await dlmm.refetchStates();
    const active = (await dlmm.getActiveBin()).binId as number;
    const { position, sig } = await addPosition(conn, kp, dlmm, x, 0n, active + 1, active + k);
    return { position, tokens_raw: x.toString(), min_bin: active + 1, max_bin: active + k, txs: [...txs, sig] };
  });
}

/** Close the given positions of this pool, then swap every token of the pool back to SOL. */
export function close(conn: Connection, dlmm: Dlmm, body: Who & { positions: string[]; slippage_bps?: number }) {
  const kp = key(body);
  const list = (body.positions ?? []).filter((p) => typeof p === "string").slice(0, 4);
  return measured(conn, kp, async () => {
    const mint = solPool(dlmm);
    const txs: (string | undefined)[] = [];
    for (const p of list) txs.push(...await removeAll(conn, kp, dlmm, p));
    const x = await tokenRaw(conn, kp, mint);
    const sold = await swap(conn, kp, mint.toBase58(), SOL_MINT, x, Math.min(body.slippage_bps ?? 800, 1500));
    txs.push(sold?.sig);
    return { tokens_sold_raw: x.toString(), txs: txs.filter(Boolean) };
  });
}

/** The bot wallet's address and SOL balance, and whether its owner may trade real money. */
export async function wallet(conn: Connection, q: Partial<Who>) {
  const kp = key(q);
  return { address: kp.publicKey.toBase58(), sol: (await lamports(conn, kp)) / LAMPORTS_PER_SOL,
    ...access(who(q).owner.toBase58()) };
}

/** Unsigned transfer from the owner's wallet to the bot wallet: the owner signs it in their own wallet. */
export async function fundTx(conn: Connection, body: Who & { sol: number }) {
  mayTrade(body);
  const to = key(body).publicKey;
  const user = who(body).owner;
  const sol = Number(body.sol);
  if (!(sol > 0 && sol <= MAX_FUND_SOL)) throw new HttpError(400, `sol must be in (0, ${MAX_FUND_SOL}]`);
  const tx = new Transaction().add(SystemProgram.transfer({
    fromPubkey: user, toPubkey: to, lamports: Math.round(sol * LAMPORTS_PER_SOL) }));
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  tx.feePayer = user;
  tx.recentBlockhash = blockhash;
  return { to: to.toBase58(), lastValidBlockHeight, transactions: [{
    transaction: tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString("base64"),
    simulation: null }] };
}

export function withdrawMessage(botId: number, ts: number): string {
  return `LP Dash bot ${botId}: withdraw at ${ts}`;
}

/**
 * Send everything (minus the fee) back to the owner, the only destination. The owner must have signed
 * withdrawMessage(bot_id, ts) within the last 2 minutes; the API also makes each signature single-use.
 */
export function withdraw(conn: Connection, body: Who & { ts: number; signature: string }) {
  const kp = key(body);
  const { id, owner } = who(body);
  const ts = Number(body.ts);
  if (!(Math.abs(Date.now() / 1000 - ts) <= MAX_SIGNED_AGE_S)) throw new HttpError(400, "request expired: sign again");
  const v = verifySigned({ pubkey: owner.toBase58(), message: withdrawMessage(id, ts), signature: body.signature });
  if (!v.ok) throw new HttpError(403, v.error ?? "bad signature");
  return locked(async () => {
    const amt = (await lamports(conn, kp)) - 10_000;
    if (amt <= 0) throw new HttpError(400, "nothing to withdraw");
    const sig = await sendLegacy(conn, kp, new Transaction().add(SystemProgram.transfer({
      fromPubkey: kp.publicKey, toPubkey: owner, lamports: amt })));
    return { to: owner.toBase58(), sol: amt / LAMPORTS_PER_SOL, txs: [sig] };
  });
}

const ED25519_SPKI = Buffer.from("302a300506032b6570032100", "hex");

/** Is `signature` (base64) the signature of `message` by the wallet `pubkey`? */
export function verifySigned(body: { pubkey: string; message: string; signature: string }) {
  let pk: PublicKey;
  try {
    pk = new PublicKey(body.pubkey);
  } catch {
    return { ok: false, error: "bad pubkey" };
  }
  try {
    const keyObj = createPublicKey({ key: Buffer.concat([ED25519_SPKI, pk.toBuffer()]), format: "der", type: "spki" });
    const ok = verify(null, Buffer.from(String(body.message), "utf8"), keyObj,
      Buffer.from(String(body.signature), "base64"));
    return ok ? { ok: true } : { ok: false, error: "bad signature" };
  } catch {
    return { ok: false, error: "bad signature" };
  }
}
