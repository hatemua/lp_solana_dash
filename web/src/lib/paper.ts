import { API_URL } from "./api";

/** Paper (demo) trading: virtual money only. Calls carry the session cookie. */

export interface EntryParams {
  narratives: string[];
  tiers: string[];
  hot_narratives: number;
  safe_only: boolean;
  socials_only: boolean;
  exclude_boosted: boolean;
  rank_by: string;
  [key: string]: number | boolean | string | string[] | null;
}

export interface ExitParams {
  stop_loss_pct: number;
  take_profit_pct: number | null;
  take_profit_part: number;
  trail_trigger_pct: number | null;
  trail_drop_pct: number | null;
  max_hours: number;
  fast_drop_pct: number | null;
  rug_liquidity_drop_pct: number;
  sell_pressure_exit: boolean;
  narrative_cool_rank: number | null;
  [key: string]: number | boolean | null;
}

export interface Params {
  entry: EntryParams;
  exit: ExitParams;
  cooldown_h: number;
  max_cost_pct: number;
}

export interface Template {
  id: string;
  name: string;
  risk: "low" | "medium" | "high";
  description: string;
  params: Params;
}

export interface MyStrategy {
  id: number;
  name: string;
  description: string;
  base_template: string | null;
  params: Params;
  created_at: string;
}

export interface Bot {
  id: number;
  user_id: number | null;
  house_key: string | null;
  name: string;
  strategy_key: string;
  strategy_name: string;
  params: Params;
  budget_usd: number;
  cash_usd: number;
  position_usd: number;
  max_open: number;
  status: "running" | "paused" | "stopping" | "stopped";
  created_at: string;
  open_positions: number;
  closed_positions: number;
  wins: number;
  realized_usd: number;
  unrealized_usd: number;
  open_value_usd: number;
  equity_usd: number;
  pnl_usd: number;
  pnl_pct: number;
  win_rate: number | null;
}

export interface Account {
  start_usd: number;
  cash_usd: number;
  in_bots_usd: number;
  equity_usd: number;
  pnl_usd: number;
  pnl_pct: number;
  created_at: string;
  reset_at: string;
}

export interface PaperPosition {
  id: number;
  mint: string;
  symbol: string | null;
  name: string | null;
  narrative: string | null;
  tier: string | null;
  status: "open" | "closed";
  opened_at: string;
  closed_at: string | null;
  entry_price: number;
  last_price: number;
  peak_price: number;
  usd_in: number;
  usd_out: number;
  value_usd?: number;
  costs_usd: number;
  entry_cost_pct: number | null;
  exit_reason: string | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  price_chg_pct: number | null;
  tp_done: string | null;
}

export interface Heat {
  narrative: string;
  label: string;
  tokens: number;
  median_chg_1h: number;
  median_chg_6h: number;
  rank: number;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    let msg = body?.detail ?? body?.error ?? `${r.status}`;
    if (Array.isArray(msg)) msg = msg.map((d: { loc?: string[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw Object.assign(new Error(typeof msg === "string" ? msg : JSON.stringify(msg)), { status: r.status });
  }
  return body as T;
}

const post = <T>(path: string, body: unknown = {}) => call<T>(path, { method: "POST", body: JSON.stringify(body) });

export const templates = () =>
  call<{ templates: Template[]; narratives: Record<string, string>; tiers: Record<string, string> }>("/v1/paper/templates");
export const house = () => call<{ bots: Bot[]; heartbeat: { ts: number; tokens: number; sol_usd: number; paused: string | null } | null }>("/v1/paper/house");
export const account = () => call<{ account: Account | null; bots: Bot[]; max_usd: number }>("/v1/paper/account");
export const createAccount = (start_usd: number, reset = false) =>
  post<{ account: Account; bots: Bot[] }>("/v1/paper/account", { start_usd, reset });
export const strategies = () => call<{ templates: Template[]; mine: MyStrategy[] }>("/v1/paper/strategies");
export const createStrategy = (name: string, description: string, base_template: string | null, params: Params) =>
  post<{ id: number }>("/v1/paper/strategies", { name, description, base_template, params });
export const deleteStrategy = (id: number) => post<{ ok: boolean }>(`/v1/paper/strategies/${id}/delete`);
export const createBot = (b: { name: string; strategy: string; budget_usd: number; position_usd: number; max_open: number }) =>
  post<Bot>("/v1/paper/bots", b);
export const botAction = (id: number, action: "pause" | "resume" | "stop") => post<Bot>(`/v1/paper/bots/${id}/action`, { action });
export const deleteBot = (id: number) => post<{ ok: boolean }>(`/v1/paper/bots/${id}/delete`);
export const botPositions = (id: number) => call<{ bot: Bot; positions: PaperPosition[] }>(`/v1/paper/bots/${id}/positions?limit=200`);
export const categories = () => call<{ heat: Heat[]; counts: Record<string, number> }>("/v1/memes/categories");
