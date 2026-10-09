/** Pure helpers: turn the SDK's bin objects into plain JSON (unit-tested, no network). */

export interface SdkBin {
  binId: number;
  price: string;            // price per lamport unit (SDK raw)
  pricePerToken?: string;   // human price: token Y per token X
  xAmount: { toString(): string };
  yAmount: { toString(): string };
  supply: { toString(): string };
}

export interface BinOut {
  bin_id: number;
  price: number;            // token Y per token X (human units)
  x: number;                // token X in the bin (human units)
  y: number;                // token Y in the bin (human units)
  supply: string;           // liquidity shares (raw, as string)
}

export function toHuman(raw: { toString(): string }, decimals: number): number {
  const s = raw.toString();
  if (!/^\d+$/.test(s)) return Number.NaN;
  if (decimals <= 0) return Number(s);
  const pad = s.padStart(decimals + 1, "0");
  return Number(`${pad.slice(0, -decimals)}.${pad.slice(-decimals)}`);
}

export function humanPrice(bin: SdkBin, decimalsX: number, decimalsY: number): number {
  if (bin.pricePerToken !== undefined) return Number(bin.pricePerToken);
  // raw price is per smallest unit: scale by 10^(decimalsX - decimalsY)
  return Number(bin.price) * 10 ** (decimalsX - decimalsY);
}

export function serializeBins(bins: SdkBin[], decimalsX: number, decimalsY: number): BinOut[] {
  return [...bins]
    .sort((a, b) => a.binId - b.binId)
    .map((b) => ({
      bin_id: b.binId,
      price: humanPrice(b, decimalsX, decimalsY),
      x: toHuman(b.xAmount, decimalsX),
      y: toHuman(b.yAmount, decimalsY),
      supply: b.supply.toString(),
    }));
}

/** Number of bins for a +pct range at a given bin step (bps): ln(1 + pct) / ln(1 + step/10000). */
export function binsForRange(pct: number, binStepBps: number): number {
  return Math.ceil(Math.log(1 + pct) / Math.log(1 + binStepBps / 10_000));
}
