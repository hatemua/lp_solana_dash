/** Pure helpers for wallet LP (unit-tested): amounts, shapes, ranges, cost preview. */

export const SHAPES = { spot: 0, curve: 1, bidask: 2 } as const; // = SDK StrategyType
export type Shape = keyof typeof SHAPES;

export const MAX_BINS_ONE_POSITION = 69;   // wider ranges need extra (refundable) rent and more transactions
export const POSITION_RENT_SOL = 0.05740608; // SDK POSITION_FEE: refunded when the position is closed
export const BIN_ARRAY_RENT_SOL = 0.07143744; // SDK BIN_ARRAY_FEE: paid once per new bin array, not refunded
export const BINS_PER_ARRAY = 70;

/** Human amount -> raw integer string (no float rounding past `decimals`). */
export function toRaw(amount: number | string, decimals: number): string {
  const s = typeof amount === "number" ? amount.toFixed(Math.min(decimals, 20)) : amount.trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error(`invalid amount ${amount}`);
  const [whole, frac = ""] = s.split(".");
  const raw = (whole + frac.padEnd(decimals, "0").slice(0, decimals)).replace(/^0+(?=\d)/, "");
  return raw === "" ? "0" : raw;
}

export function validateRange(minBinId: number, maxBinId: number, activeBinId: number): string | null {
  if (!Number.isInteger(minBinId) || !Number.isInteger(maxBinId)) return "bin ids must be integers";
  if (maxBinId < minBinId) return "max_bin_id must be >= min_bin_id";
  if (maxBinId - minBinId + 1 > MAX_BINS_ONE_POSITION) {
    return `range is ${maxBinId - minBinId + 1} bins; at most ${MAX_BINS_ONE_POSITION} per position`;
  }
  if (Math.abs(minBinId - activeBinId) > 2000 || Math.abs(maxBinId - activeBinId) > 2000) {
    return "range is too far from the active bin";
  }
  return null;
}

/** Which side the user must deposit, given the range and the active bin (DLMM rules). */
export function requiredSides(minBinId: number, maxBinId: number, activeBinId: number): { x: boolean; y: boolean } {
  return { x: maxBinId >= activeBinId, y: minBinId <= activeBinId };
}

/** Bin array indexes touched by a range (70 bins per array). */
export function binArrayIndexes(minBinId: number, maxBinId: number): number[] {
  const lo = Math.floor(minBinId / BINS_PER_ARRAY);
  const hi = Math.floor(maxBinId / BINS_PER_ARRAY);
  const out: number[] = [];
  for (let i = lo; i <= hi; i++) out.push(i);
  return out;
}
