/**
 * The signed-in user's bots, with their own OAuth token (forwarded to the API). Reads, plus one change: a take profit /
 * stop loss suggestion (POST /v1/lpbots/{id}/suggest, needs the bots:suggest scope). A paper bot takes it at once; a
 * real-money bot keeps it until the owner clicks Apply. Nothing here can move money, close, trade or sign.
 */
import { ApiError, queryString, type Query } from "./api.js";

export interface Suggestion { take_profit_pct?: number; stop_loss_pct?: number; reason?: string }

export interface Bots {
  get(path: string, q?: Query): Promise<unknown>;
  suggest(botId: number, s: Suggestion): Promise<unknown>;
}

async function call(url: string, init: RequestInit): Promise<unknown> {
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(30_000) });
  const text = await res.text();
  if (!res.ok) {
    let msg = text.slice(0, 300);
    try {
      const j = JSON.parse(text) as { detail?: unknown; error?: unknown };
      const d = j.detail ?? j.error;
      if (d !== undefined) msg = typeof d === "string" ? d : JSON.stringify(d).slice(0, 300);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return JSON.parse(text) as unknown;
}

export function botsClient(baseUrl: string, authorization: string, clientIp?: string): Bots {
  const headers: Record<string, string> = { accept: "application/json", authorization };
  if (clientIp) headers["x-forwarded-for"] = clientIp;
  return {
    async get(path, q) {
      if (!/^\/v1\/lpbots(\/|$|\?)/.test(path)) throw new Error("bots client reads /v1/lpbots only");
      return call(`${baseUrl}${path}${queryString(q)}`, { method: "GET", headers });
    },
    async suggest(botId, s) {
      if (!Number.isInteger(botId) || botId < 1) throw new Error("bot_id must be a positive integer");
      return call(`${baseUrl}/v1/lpbots/${botId}/suggest`, {
        method: "POST", headers: { ...headers, "content-type": "application/json" }, body: JSON.stringify(s),
      });
    },
  };
}
