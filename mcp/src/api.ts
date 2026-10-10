/** Minimal client for the LP Solana Dash REST API (internal network: http://api:8000). GET only. */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export type Query = Record<string, string | number | boolean | undefined | null>;

export function queryString(q: Query = {}): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export interface Api {
  get(path: string, q?: Query): Promise<unknown>;
}

export function apiClient(baseUrl: string, clientIp?: string, timeoutMs = 60_000): Api {
  return {
    async get(path, q) {
      // read-only by construction: this client can only issue GET requests
      const headers: Record<string, string> = { accept: "application/json" };
      if (clientIp) headers["x-forwarded-for"] = clientIp; // the API rate-limits per real client, not per MCP host
      const res = await fetch(`${baseUrl}${path}${queryString(q)}`, {
        method: "GET",
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      });
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
    },
  };
}
