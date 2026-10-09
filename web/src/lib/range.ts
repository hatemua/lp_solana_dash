/** DLMM range helpers (pure, unit-tested). price(bin i) = base × (1 + binStep/10 000)^i. */

export type Side = "sol" | "token" | "both";

/** Number of bins needed to cover a relative move of `pct` (e.g. 0.2 = +20%). */
export function binsFor(pct: number, binStep: number): number {
  if (pct <= 0) return 0;
  return Math.ceil(Math.log(1 + pct) / Math.log(1 + binStep / 10_000));
}

/** Price of a bin relative to the active bin. */
export function priceAt(activePrice: number, activeBin: number, bin: number, binStep: number): number {
  return activePrice * (1 + binStep / 10_000) ** (bin - activeBin);
}

/**
 * Bin range for a deposit side and a width in % (down for SOL, up for the token, both ways for 50/50).
 * tokenIsX: true when the non-SOL token is token X (bins above the price hold X).
 */
export function rangeFor(side: Side, widthPct: number, activeBin: number, binStep: number, tokenIsX: boolean,
                         maxBins = 69): { min: number; max: number } {
  const n = Math.max(1, Math.min(binsFor(widthPct / 100, binStep), side === "both" ? Math.floor((maxBins - 1) / 2) : maxBins - 1));
  const up = { min: activeBin, max: activeBin + n };        // holds token X
  const down = { min: activeBin - n, max: activeBin };      // holds token Y
  if (side === "both") return { min: activeBin - n, max: activeBin + n };
  const tokenSide = tokenIsX ? up : down;
  const solSide = tokenIsX ? down : up;
  return side === "token" ? tokenSide : solSide;
}

/** Deposit amounts in X/Y for the chosen side (the UI asks for one amount in SOL or the token). */
export function amountsFor(side: Side, amount: number, tokenIsX: boolean, solPerToken: number):
  { x: number; y: number } {
  if (side === "both") {
    const tokenAmt = amount / 2 / solPerToken;       // amount is in SOL: half SOL, half token value
    const sol = amount / 2;
    return tokenIsX ? { x: tokenAmt, y: sol } : { x: sol, y: tokenAmt };
  }
  if (side === "sol") return tokenIsX ? { x: 0, y: amount } : { x: amount, y: 0 };
  return tokenIsX ? { x: amount, y: 0 } : { x: 0, y: amount };
}
