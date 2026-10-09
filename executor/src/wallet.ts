/**
 * Non-custodial wallet LP. Builds UNSIGNED transactions for the user's wallet to sign (the user is fee payer and
 * position owner). The only key this service ever creates is the throwaway keypair of a NEW position account,
 * required by the program as a co-signer; its secret is used once to co-sign and then dropped. No user key, no bot key.
 */
import { createRequire } from "node:module";
import { Connection, Keypair, PublicKey, Transaction } from "@solana/web3.js";
import { binArrayIndexes, BIN_ARRAY_RENT_SOL, POSITION_RENT_SOL, requiredSides, Shape, SHAPES, toRaw,
  validateRange } from "./lp.js";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Dlmm = any;
const BN: any = createRequire(import.meta.url)("bn.js");

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function decimalsOf(token: any): number {
  return Number(token?.mint?.decimals ?? token?.decimal ?? token?.decimals ?? 0);
}

function human(raw: { toString(): string } | string | undefined, decimals: number): number {
  if (raw === undefined || raw === null) return 0;
  const s = raw.toString();
  return /^\d+$/.test(s) ? Number(s) / 10 ** decimals : Number(s);
}

async function finalize(conn: Connection, txs: Transaction[], user: PublicKey, extraSigner?: Keypair) {
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  const out = [];
  for (const tx of txs) {
    tx.feePayer = user;
    tx.recentBlockhash = blockhash;
    if (extraSigner && tx.signatures.some((s) => s.publicKey.equals(extraSigner.publicKey))) {
      tx.partialSign(extraSigner);
    }
    let simulation: { ok: boolean; error: unknown; logs: string[] | null } | null = null;
    try {
      const sim = await conn.simulateTransaction(tx);
      simulation = { ok: !sim.value.err, error: sim.value.err, logs: sim.value.logs?.slice(-8) ?? null };
    } catch (e) {
      simulation = { ok: false, error: String(e).slice(0, 200), logs: null };
    }
    out.push({
      transaction: tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString("base64"),
      simulation,
    });
  }
  return { transactions: out, lastValidBlockHeight };
}

export async function quote(conn: Connection, dlmm: Dlmm, minBinId: number, maxBinId: number) {
  const active = (await dlmm.getActiveBin()).binId as number;
  const err = validateRange(minBinId, maxBinId, active);
  if (err) throw new HttpError(400, err);
  const q = await dlmm.quoteCreatePosition({ strategy: { minBinId, maxBinId, strategyType: SHAPES.spot } });
  const indexes = binArrayIndexes(minBinId, maxBinId);
  return {
    activeBinId: active,
    sides: requiredSides(minBinId, maxBinId, active),
    positionRentSol: Number(q.positionCost ?? POSITION_RENT_SOL),
    positionRentRefundable: true,
    newBinArrays: Number(q.binArraysCount ?? 0),
    binArrayRentSol: Number(q.binArrayCost ?? 0),
    binArrayRentRefundable: false,
    binArrayRentPerArraySol: BIN_ARRAY_RENT_SOL,
    binArraysTouched: indexes.length,
    transactions: Number(q.transactionCount ?? 1),
    estNetworkFeeSol: 0.00001 * Number(q.transactionCount ?? 1),
  };
}

export async function addLiquidity(conn: Connection, dlmm: Dlmm, body: {
  user: string; amount_x: number; amount_y: number; min_bin_id: number; max_bin_id: number; shape: Shape;
}) {
  const user = new PublicKey(body.user);
  const active = (await dlmm.getActiveBin()).binId as number;
  const err = validateRange(body.min_bin_id, body.max_bin_id, active);
  if (err) throw new HttpError(400, err);
  if (!(body.shape in SHAPES)) throw new HttpError(400, "shape must be spot, curve or bidask");
  const sides = requiredSides(body.min_bin_id, body.max_bin_id, active);
  const x = sides.x ? body.amount_x : 0;
  const y = sides.y ? body.amount_y : 0;
  if (x <= 0 && y <= 0) throw new HttpError(400, "nothing to deposit for this range (check which side it needs)");
  const position = Keypair.generate();          // throwaway: co-signs only the new position account
  const tx: Transaction = await dlmm.initializePositionAndAddLiquidityByStrategy({
    positionPubKey: position.publicKey,
    user,
    totalXAmount: new BN(toRaw(x, decimalsOf(dlmm.tokenX))),
    totalYAmount: new BN(toRaw(y, decimalsOf(dlmm.tokenY))),
    strategy: { minBinId: body.min_bin_id, maxBinId: body.max_bin_id, strategyType: SHAPES[body.shape] },
    slippage: 1,
  });
  const built = await finalize(conn, [tx], user, position);
  return { position: position.publicKey.toBase58(), activeBinId: active, depositX: x, depositY: y,
    cost: await quote(conn, dlmm, body.min_bin_id, body.max_bin_id), ...built };
}

async function ownedPosition(dlmm: Dlmm, user: PublicKey, position: string) {
  let pos: any;
  try {
    pos = await dlmm.getPosition(new PublicKey(position));
  } catch {
    throw new HttpError(404, "position not found in this pool");
  }
  const owner = pos?.positionData?.owner as PublicKey | undefined;
  if (owner && !owner.equals(user)) throw new HttpError(403, "this position belongs to another wallet");
  return pos;
}

export async function removeLiquidity(conn: Connection, dlmm: Dlmm, body: {
  user: string; position: string; bps: number; claim_and_close: boolean;
}) {
  const user = new PublicKey(body.user);
  const pos = await ownedPosition(dlmm, user, body.position);
  const txs: Transaction[] = await dlmm.removeLiquidity({
    user,
    position: pos.publicKey,
    fromBinId: pos.positionData.lowerBinId,
    toBinId: pos.positionData.upperBinId,
    bps: new BN(body.bps),
    shouldClaimAndClose: body.claim_and_close,
  });
  return { position: body.position, ...(await finalize(conn, Array.isArray(txs) ? txs : [txs], user)) };
}

export async function claimFees(conn: Connection, dlmm: Dlmm, body: { user: string; position: string }) {
  const user = new PublicKey(body.user);
  const pos = await ownedPosition(dlmm, user, body.position);
  const txs: Transaction[] = await dlmm.claimSwapFee({ owner: user, position: pos });
  return { position: body.position, ...(await finalize(conn, Array.isArray(txs) ? txs : [txs], user)) };
}

/** Broadcast a transaction that the USER's wallet already signed (we never sign it). */
export async function sendSigned(conn: Connection, body: { transaction: string }) {
  if (typeof body.transaction !== "string" || body.transaction.length > 3000) {
    throw new HttpError(400, "transaction must be a base64 string");
  }
  const raw = Buffer.from(body.transaction, "base64");
  const tx = Transaction.from(raw);
  if (!tx.signatures.length || tx.signatures.some((s) => s.signature === null)) {
    throw new HttpError(400, "transaction is not fully signed by the wallet");
  }
  const signature = await conn.sendRawTransaction(raw, { skipPreflight: false, maxRetries: 3 });
  const latest = await conn.getLatestBlockhash("confirmed");
  const conf = await conn.confirmTransaction({ signature, ...latest }, "confirmed");
  return { signature, confirmed: !conf.value.err, error: conf.value.err ?? null };
}

export async function userPositions(conn: Connection, DLMM: any, owner: string) {
  const map: Map<string, any> = await DLMM.getAllLbPairPositionsByUser(conn, new PublicKey(owner));
  const out = [];
  for (const [pool, info] of map.entries()) {
    const dx = decimalsOf(info.tokenX);
    const dy = decimalsOf(info.tokenY);
    const active = Number(info.lbPair?.activeId);
    for (const p of info.lbPairPositionsData ?? []) {
      const d = p.positionData;
      out.push({
        pool,
        position: p.publicKey.toBase58(),
        tokenX: info.tokenX?.publicKey?.toBase58?.() ?? null,
        tokenY: info.tokenY?.publicKey?.toBase58?.() ?? null,
        lowerBinId: d.lowerBinId,
        upperBinId: d.upperBinId,
        activeBinId: active,
        inRange: active >= d.lowerBinId && active <= d.upperBinId,
        amountX: human(d.totalXAmount, dx),
        amountY: human(d.totalYAmount, dy),
        feeX: human(d.feeX, dx),
        feeY: human(d.feeY, dy),
        claimedFeeX: human(d.totalClaimedFeeXAmount, dx),
        claimedFeeY: human(d.totalClaimedFeeYAmount, dy),
        binStep: Number(info.lbPair?.binStep),
      });
    }
  }
  return { owner, positions: out };
}
