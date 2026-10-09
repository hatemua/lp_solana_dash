/**
 * Executor: the only service that talks to the Meteora DLMM SDK. Listens on the internal network only.
 * Reads pool bins and wallet positions, and builds UNSIGNED LP transactions for the user's own wallet to sign.
 * It holds no user or bot key (see wallet.ts).
 */
import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { Connection, PublicKey } from "@solana/web3.js";
import { serializeBins, SdkBin } from "./bins.js";
import { addLiquidity, claimFees, decimalsOf as decimalsOfToken, HttpError, quote, removeLiquidity,
  sendSigned, userPositions } from "./wallet.js";

// Load the SDK's CommonJS build: its ESM build has directory imports (@coral-xyz/anchor) that Node rejects.
const require = createRequire(import.meta.url);
const dlmmPkg = require("@meteora-ag/dlmm");
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const DLMM: any = dlmmPkg.default ?? dlmmPkg;

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

const decimalsOf = decimalsOfToken;

const MAX_BODY = 16 * 1024;
function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) reject(new HttpError(413, "body too large"));
      else chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(new HttpError(400, "invalid JSON"));
      }
    });
    req.on("error", reject);
  });
}

const B58 = "[1-9A-HJ-NP-Za-km-z]{32,44}";

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
  try {
    const posM = url.pathname.match(new RegExp(`^/v1/users/(${B58})/positions$`));
    if (req.method === "GET" && posM) return send(res, 200, await userPositions(connection, DLMM, posM[1]));
    const quoteM = url.pathname.match(new RegExp(`^/v1/pools/(${B58})/quote$`));
    if (req.method === "GET" && quoteM) {
      const dlmm = await getPool(quoteM[1]);
      return send(res, 200, await quote(connection, dlmm, Number(url.searchParams.get("min_bin_id")),
        Number(url.searchParams.get("max_bin_id"))));
    }
    if (req.method === "POST" && url.pathname === "/v1/tx/send") {
      return send(res, 200, await sendSigned(connection, await readJson(req) as never));
    }
    const txM = url.pathname.match(new RegExp(`^/v1/pools/(${B58})/tx/(add-liquidity|remove-liquidity|claim-fees)$`));
    if (req.method === "POST" && txM) {
      const body = await readJson(req) as never;
      const dlmm = await getPool(txM[1]);
      const fn = { "add-liquidity": addLiquidity, "remove-liquidity": removeLiquidity, "claim-fees": claimFees }[txM[2]];
      return send(res, 200, await fn!(connection, dlmm, body));
    }
  } catch (e) {
    stats.errors += 1;
    if (e instanceof HttpError) return send(res, e.status, { error: e.message });
    return send(res, 502, { error: (e instanceof Error ? e.message : String(e)).slice(0, 300) });
  }
  if (req.method !== "GET") return send(res, 405, { error: "method not allowed" });
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
