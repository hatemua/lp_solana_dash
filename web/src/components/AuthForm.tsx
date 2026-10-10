"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FormEvent, useState } from "react";
import { login, MIN_PASSWORD, safeNext, signup } from "@/lib/auth";

export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const connecting = next.includes("/oauth/authorize?");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "signup" && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    setBusy(true);
    try {
      await (mode === "login" ? login : signup)(email, password);
      window.location.assign(next); // full navigation: the OAuth consent page is served by the API host
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const other = mode === "login" ? "signup" : "login";
  const otherHref = `/${other}${params.get("next") ? `?next=${encodeURIComponent(params.get("next")!)}` : ""}`;

  return (
    <div className="mx-auto mt-10 max-w-sm">
      <form onSubmit={submit} className="rounded-lg border border-line bg-panel p-6">
        <h1 className="mb-1 text-lg font-semibold">{mode === "login" ? "Log in" : "Create an account"}</h1>
        <p className="mb-5 text-sm text-mut">
          {connecting
            ? "An app (MCP client) wants read-only access to LP Dash. Sign in to continue."
            : "Accounts are used to connect Claude and other MCP clients. Browsing pools needs no account."}
        </p>
        <label className="mb-3 block text-sm">
          <span className="mb-1 block text-mut">Email</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border border-line bg-bg px-3 py-2 outline-none focus:border-acc"
          />
        </label>
        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-mut">Password</span>
          <input
            type="password"
            required
            minLength={mode === "signup" ? MIN_PASSWORD : undefined}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-line bg-bg px-3 py-2 outline-none focus:border-acc"
          />
          {mode === "signup" && <span className="mt-1 block text-xs text-mut">At least {MIN_PASSWORD} characters.</span>}
        </label>
        {error && <p className="mb-3 text-sm text-neg">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-acc py-2 font-medium text-bg disabled:opacity-60"
        >
          {busy ? "…" : mode === "login" ? "Log in" : "Sign up"}
        </button>
        <p className="mt-4 text-center text-sm text-mut">
          {mode === "login" ? "No account yet? " : "Already have an account? "}
          <Link href={otherHref} className="text-acc hover:underline">
            {mode === "login" ? "Sign up" : "Log in"}
          </Link>
        </p>
      </form>
    </div>
  );
}
