export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "https://lp.api.joulity.com").replace(/\/$/, "");
export const SOL_MINT = "So11111111111111111111111111111111111111112";

export interface PoolRow {
  address: string;
  name: string | null;
  token_x: string;
  token_y: string;
  bin_step: number | null;
  base_fee_pct: number | null;
  pool_created_at: string | null;
  launchpad: string | null;
  is_hot: boolean;
  stats_ts: string | null;
  price: number | null;
  tvl: number | null;
  volume_5m: number | null;
  volume_1h: number | null;
  volume_24h: number | null;
  fees_1h: number | null;
  fees_24h: number | null;
  fee_tvl_1h: number | null;
  fee_tvl_24h: number | null;
  dynamic_fee_pct: number | null;
  sol_pair: boolean;
  token_mint: string | null;
  token_symbol: string | null;
  token_name: string | null;
  token_launchpad: string | null;
  mcap: number | null;
  holders: number | null;
  organic_score: number | null;
  mint_disabled: boolean | null;
  freeze_disabled: boolean | null;
  top10_pct: number | null;
  dev_pct: number | null;
  token_volume_5m: number | null;
  token_volume_1h: number | null;
  price_change_5m: number | null;
  price_change_1h: number | null;
  price_change_24h: number | null;
  pool_age_h: number | null;
  token_age_h: number | null;
  volume_burst: number | null;
  lp_score: number | null;
}

export interface PoolsResponse {
  total: number;
  limit: number;
  offset: number;
  took_ms: number;
  data: PoolRow[];
}

export interface Preset {
  id: string;
  label: string;
  description: string;
  filters: Record<string, number | boolean>;
  sort: string;
}

export interface Bin {
  bin_id: number;
  price: number;
  x: number;
  y: number;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, { ...init, cache: "no-store",
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      msg = j.detail ?? j.error ?? msg;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json() as Promise<T>;
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}
