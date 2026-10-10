import type { Metadata } from "next";
import Link from "next/link";
import { ReactNode } from "react";
import { Providers } from "./providers";
import { WalletButton } from "@/components/WalletButton";
import { AccountButton } from "@/components/Account";
import "./globals.css";

export const metadata: Metadata = {
  title: "LP Dash: Meteora DLMM pools",
  description: "Meteora DLMM pools on Solana: filters, charts, signals and non-custodial LP.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <header className="sticky top-0 z-20 border-b border-line bg-bg/90 backdrop-blur">
            <div className="mx-auto flex max-w-[1500px] items-center gap-6 px-4 py-2">
              <Link href="/" className="font-semibold tracking-tight">
                LP<span className="text-acc">Dash</span>
              </Link>
              <nav className="flex gap-4 text-sm text-mut">
                <Link href="/" className="hover:text-slate-100">Pools</Link>
                <Link href="/wallet" className="hover:text-slate-100">My positions</Link>
                <Link href="/bot" className="hover:text-slate-100">Bot</Link>
              </nav>
              <div className="ml-auto flex items-center gap-4">
                <AccountButton />
                <WalletButton />
              </div>
            </div>
          </header>
          <main className="mx-auto max-w-[1500px] px-4 py-4">{children}</main>
          <footer className="mx-auto max-w-[1500px] px-4 py-6 text-xs text-mut">
            Data: Meteora DLMM and Jupiter. Not financial advice. Your keys stay in your wallet.
          </footer>
        </Providers>
      </body>
    </html>
  );
}
