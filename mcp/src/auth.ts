import { createHash, timingSafeEqual } from "node:crypto";

/** Constant-time check of an `Authorization: Bearer <token>` header. An empty configured token denies everything. */
export function bearerOk(header: string | undefined, token: string): boolean {
  if (!token || !header) return false;
  const m = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!m) return false;
  const a = createHash("sha256").update(m[1]).digest();
  const b = createHash("sha256").update(token).digest();
  return timingSafeEqual(a, b);
}

export interface OAuthUser {
  id: number;
  email: string;
}

const CACHE_MS = 60_000;
const cache = new Map<string, { user: OAuthUser | null; until: number }>();

/**
 * Validate an OAuth access token issued by our authorization server (the API) by asking the API who owns it.
 * Results are cached for 60 s by token hash; revocation therefore takes effect within a minute.
 */
export async function oauthUser(header: string | undefined, apiUrl: string, clientIp?: string): Promise<OAuthUser | null> {
  const m = /^Bearer\s+(\S{20,200})$/i.exec(header?.trim() ?? "");
  if (!m) return null;
  const key = createHash("sha256").update(m[1]).digest("hex");
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && hit.until > now) return hit.user;
  let user: OAuthUser | null = null;
  try {
    const headers: Record<string, string> = { authorization: `Bearer ${m[1]}`, accept: "application/json" };
    if (clientIp) headers["x-forwarded-for"] = clientIp;
    const r = await fetch(`${apiUrl}/v1/auth/me`, { headers, signal: AbortSignal.timeout(10_000) });
    if (r.ok) {
      const d = (await r.json()) as { via?: string; scope?: string; user?: OAuthUser };
      if (d.via === "oauth" && d.user && (d.scope ?? "").split(" ").includes("mcp:read")) user = d.user;
    } else if (r.status >= 500 || r.status === 429) {
      return null; // do not cache transient failures
    }
  } catch {
    return null;
  }
  if (cache.size > 5000) cache.clear();
  cache.set(key, { user, until: now + CACHE_MS });
  return user;
}
