import { API_URL } from "./api";

export interface User {
  id: number;
  email: string;
  created_at: string;
}

export interface Connection {
  client_id: string;
  client_name: string | null;
  since: string;
  last_used: string;
}

/** Auth calls carry the session cookie (HttpOnly, set by lp.api.joulity.com). */
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = body?.detail ?? body?.error ?? `${r.status}`;
    throw Object.assign(new Error(typeof msg === "string" ? msg : JSON.stringify(msg)), { status: r.status });
  }
  return body as T;
}

export async function me(): Promise<User | null> {
  try {
    return (await call<{ user: User }>("/v1/auth/me")).user;
  } catch (e) {
    if ((e as { status?: number }).status === 401) return null;
    throw e;
  }
}

export const login = (email: string, password: string) =>
  call<{ user: User }>("/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });

export const signup = (email: string, password: string) =>
  call<{ user: User }>("/v1/auth/signup", { method: "POST", body: JSON.stringify({ email, password }) });

export const logout = () => call<{ ok: boolean }>("/v1/auth/logout", { method: "POST" });

export const connections = () => call<{ connections: Connection[] }>("/v1/auth/connections");

export const revokeConnection = (clientId: string) =>
  call<{ ok: boolean }>(`/v1/auth/connections/${encodeURIComponent(clientId)}/revoke`, { method: "POST" });

/**
 * Where to go after login. Only our own OAuth authorize URL (an MCP client connecting) or a local path;
 * anything else would be an open redirect.
 */
export function safeNext(next: string | null, apiUrl = API_URL): string {
  if (!next) return "/account";
  if (next.startsWith(`${apiUrl}/oauth/authorize?`)) return next;
  if (/^\/(?!\/)[\w\-/?=&%.]*$/.test(next)) return next;
  return "/account";
}

export const MIN_PASSWORD = 10;
