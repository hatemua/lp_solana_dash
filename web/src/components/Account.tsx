"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { API_URL } from "@/lib/api";
import { Connection, connections, logout, me, revokeConnection, User } from "@/lib/auth";

export function AccountButton() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  useEffect(() => {
    me().then(setUser, () => setUser(null));
  }, []);
  if (user === undefined) return null;
  if (!user) {
    return (
      <Link href="/login" className="text-sm text-mut hover:text-slate-100">
        Log in
      </Link>
    );
  }
  return (
    <Link href="/account" className="max-w-[160px] truncate text-sm text-mut hover:text-slate-100" title={user.email}>
      {user.email}
    </Link>
  );
}

export function AccountView() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [apps, setApps] = useState<Connection[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const u = await me();
      setUser(u);
      if (u) setApps((await connections()).connections);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (user === undefined) return <p className="text-mut">Loading…</p>;
  if (!user) {
    return (
      <p className="mt-10 text-center text-mut">
        <Link href="/login?next=/account" className="text-acc hover:underline">Log in</Link> to see your account.
      </p>
    );
  }

  const mcpUrl = `${API_URL}/mcp`;
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <section className="rounded-lg border border-line bg-panel p-5">
        <div className="flex items-center gap-3">
          <div>
            <h1 className="text-lg font-semibold">{user.email}</h1>
            <p className="text-xs text-mut">Member since {new Date(user.created_at).toLocaleDateString()}</p>
          </div>
          <button
            onClick={async () => {
              await logout();
              window.location.assign("/");
            }}
            className="ml-auto rounded border border-line px-3 py-1.5 text-sm hover:border-acc"
          >
            Log out
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-line bg-panel p-5">
        <h2 className="mb-2 font-semibold">Connect Claude (MCP)</h2>
        <p className="mb-3 text-sm text-mut">
          Read-only tools: pool search, details, bins, history, best pools now, pool signal and exit check. No tool can
          trade, sign or move funds.
        </p>
        <div className="mb-3 flex items-center gap-2 rounded border border-line bg-bg px-3 py-2 font-mono text-sm">
          <span className="truncate">{mcpUrl}</span>
          <button onClick={() => void navigator.clipboard?.writeText(mcpUrl)} className="ml-auto text-xs text-acc">
            Copy
          </button>
        </div>
        <ul className="list-disc space-y-1 pl-5 text-sm text-mut">
          <li>
            <b className="text-slate-200">claude.ai / Claude Desktop:</b> Settings → Connectors → Add custom connector →
            paste the URL, then sign in here and click Allow.
          </li>
          <li>
            <b className="text-slate-200">Claude Code:</b>{" "}
            <code className="font-mono text-xs">claude mcp add --transport http lp-dash {mcpUrl}</code>, then run{" "}
            <code className="font-mono text-xs">/mcp</code> to sign in.
          </li>
        </ul>
      </section>

      <section className="rounded-lg border border-line bg-panel p-5">
        <h2 className="mb-2 font-semibold">Connected apps</h2>
        {error && <p className="text-sm text-neg">{error}</p>}
        {apps.length === 0 ? (
          <p className="text-sm text-mut">None yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {apps.map((a) => (
              <li key={a.client_id} className="flex items-center gap-3 py-2 text-sm">
                <div>
                  <div>{a.client_name ?? a.client_id}</div>
                  <div className="text-xs text-mut">
                    since {new Date(a.since).toLocaleString()} · last token {new Date(a.last_used).toLocaleString()}
                  </div>
                </div>
                <button
                  onClick={async () => {
                    await revokeConnection(a.client_id);
                    await load();
                  }}
                  className="ml-auto rounded border border-line px-2 py-1 text-xs hover:border-neg hover:text-neg"
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
