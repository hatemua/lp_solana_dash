/** Number formatting for the dashboard (pure, unit-tested). */

export function usd(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `$${(v / 1e3).toFixed(1)}k`;
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

export function pct(v: number | null | undefined, digits = 1, ratio = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  const x = ratio ? v * 100 : v;
  return `${x > 0 ? "+" : ""}${x.toFixed(digits)}%`;
}

export function plainPct(v: number | null | undefined, digits = 2, ratio = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  return `${(ratio ? v * 100 : v).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(1)}k`;
  return v.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1) return v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  const digits = Math.min(12, Math.ceil(-Math.log10(a)) + 3);
  return v.toFixed(digits);
}

export function age(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || !Number.isFinite(hours)) return "–";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours / 24)}d`;
}

export function shortAddr(a: string | null | undefined, n = 4): string {
  if (!a) return "–";
  return a.length <= 2 * n + 1 ? a : `${a.slice(0, n)}…${a.slice(-n)}`;
}
