/**
 * Executor: the only service that talks to the Meteora DLMM SDK. Listens on localhost / the internal network only.
 * M1 is read-only (pool bins). It holds no keys and builds no transactions.
 */
import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { Connection, PublicKey } from "@solana/web3.js";
import DLMMModule from "@meteora-ag/dlmm";
import { serializeBins, SdkBin } from "./bins.js";

// the package ships CJS + ESM builds; take the class from either
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const DLMM: any = (DLMMModule as any).default ?? DLMMModule;

const PORT = Number(process.env.EXECUTOR_PORT ?? 8200);
const HOST = process.env.EXECUTOR_HOST ?? "0.0.0.0";
const RPC_URLS = [process.env.RPC_URL, process.env.RPC_BACKUP_URL, "https://api.mainnet-beta.solana.com"]
  .filter((u): u is string => Boolean(u));
const CACHE_MS = 10 * 60 * 1000;
const MAX_SIDE = 300;

let rpcIndex = 0;
let connection = new Connection(RPC_URLS[0], "confirmed");
const pools = new Map<string, { dlmm: any; at: number }>(); // eslint-disable-line @typescript-eslint/no-explicit-any
const stats = { requests: 0, errors: 0, rpcSwitches: 0 };

function rotateRpc(): void {
  rpcIndex = (rpcIndex + 1) % RPC_URLS.length;
  connection = new Connection(RPC_URLS[rpcIndex], "confirmed");
  pools.clear();
  stats.rpcSwitches += 1;
}

async function getPool(address: string) {
  const hit = pools.get(address);
  if (hit && Date.now() - hit.at < CACHE_MS) {
    await hit.dlmm.refetchStates();
    return hit.dlmm;
  }
  const dlmm = await DLMM.create(connection, new PublicKey(address));
  pools.set(address, { dlmm, at: Date.now() });
  return dlmm;
}

function decimalsOf(token: any): number { // eslint-disable-line @typescript-eslint/no-explicit-any
  return Number(token?.mint?.decimals ?? token?.decimal ?? token?.decimals ?? 0);
}

async function bins(address: string, left: number, right: number) {
  const dlmm = await getPool(address);
  const res = await dlmm.getBinsAroundActiveBin(left, right);
  const dx = decimalsOf(dlmm.tokenX);
  const dy = decimalsOf(dlmm.tokenY);
  const out = serializeBins(res.bins as SdkBin[], dx, dy);
  const active = out.find((b) => b.bin_id === res.activeBin);
  return {
    pool: address,
    activeBinId: res.activeBin,
    binStep: Number(dlmm.lbPair.binStep),
    activePrice: active?.price ?? null,
    decimalsX: dx,
    decimalsY: dy,
    bins: out,
  };
}

function send(res: ServerResponse, code: number, body: unknown): void {
  res.writeHead(code, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", "http://localhost");
  stats.requests += 1;
  if (req.method !== "GET") return send(res, 405, { error: "read-only service" });
  if (url.pathname === "/health") {
    return send(res, 200, { ok: true, rpc: new URL(RPC_URLS[rpcIndex]).host, cachedPools: pools.size, ...stats });
  }
  const m = url.pathname.match(/^\/v1\/pools\/([1-9A-HJ-NP-Za-km-z]{32,44})\/bins$/);
  if (m) {
    const left = Math.min(Number(url.searchParams.get("left") ?? 70), MAX_SIDE);
    const right = Math.min(Number(url.searchParams.get("right") ?? 70), MAX_SIDE);
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return send(res, 200, await bins(m[1], left, right));
      } catch (e) {
        stats.errors += 1;
        const msg = e instanceof Error ? e.message : String(e);
        if (attempt === 0 && /429|fetch failed|timeout|ECONNRESET/i.test(msg) && RPC_URLS.length > 1) {
          rotateRpc();
          continue;
        }
        return send(res, 502, { error: msg.slice(0, 300) });
      }
    }
  }
  return send(res, 404, { error: "not found" });
}

createServer((req, res) => {
  handle(req, res).catch((e) => send(res, 500, { error: String(e).slice(0, 300) }));
}).listen(PORT, HOST, () => {
  console.log(`executor listening on ${HOST}:${PORT}, rpc ${new URL(RPC_URLS[0]).host}`);
});
