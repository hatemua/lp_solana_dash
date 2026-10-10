import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer, type RequestListener, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { handle } from "../src/server.js";

const TOKEN = "t".repeat(32);
const OAUTH = "o".repeat(43);
const POOL = "57JmG2uos4wGFtBuerGS2azHEPBBnp8FJRgeLmnx8uXV";

async function listen(fn: RequestListener): Promise<{ srv: Server; url: string }> {
  const srv = createServer(fn);
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
  return { srv, url: `http://127.0.0.1:${(srv.address() as AddressInfo).port}` };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function rpc(url: string, body: unknown, token = TOKEN): Promise<{ status: number; body: any }> {
  const r = await fetch(`${url}/mcp`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
    },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: r.status === 200 ? await r.json() : await r.text() };
}

test("streamable HTTP: auth, tools/list and a tool call through a fake API", async () => {
  const seen: string[] = [];
  const fakeApi = await listen((req, res) => {
    if (req.url === "/v1/auth/me") {
      const ok = req.headers.authorization === `Bearer ${OAUTH}`;
      res.writeHead(ok ? 200 : 401, { "content-type": "application/json" });
      return res.end(JSON.stringify(ok ? { via: "oauth", scope: "mcp:read", user: { id: 1, email: "a@b.co" } } : {}));
    }
    seen.push(`${req.method} ${req.url} ${req.headers["x-forwarded-for"]}`);
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ address: "p", tvl: 1 }));
  });
  const mcp = await listen((req, res) => void handle(req, res, TOKEN, fakeApi.url));
  try {
    assert.equal((await fetch(`${mcp.url}/health`)).status, 200);
    const denied = await fetch(`${mcp.url}/mcp`, { method: "POST", headers: { authorization: `Bearer ${"x".repeat(43)}` } });
    assert.equal(denied.status, 401);
    assert.match(denied.headers.get("www-authenticate") ?? "", /resource_metadata="[^"]+\/\.well-known\/oauth-protected-resource\/mcp"/);
    const viaOAuth = await rpc(mcp.url, { jsonrpc: "2.0", id: 0, method: "tools/list", params: {} }, OAUTH);
    assert.equal(viaOAuth.status, 200);
    assert.equal((await fetch(`${mcp.url}/mcp`, { headers: { authorization: `Bearer ${TOKEN}` } })).status, 405);

    const init = await rpc(mcp.url, {
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "0" } },
    });
    assert.equal(init.status, 200);
    assert.equal(init.body.result.serverInfo.name, "lp-solana-dash");

    const list = await rpc(mcp.url, { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
    assert.equal(list.body.result.tools.length, 12);
    for (const t of list.body.result.tools) assert.equal(t.annotations.readOnlyHint, true);

    const call = await rpc(mcp.url, {
      jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "get_pool", arguments: { address: POOL } },
    });
    assert.deepEqual(JSON.parse(call.body.result.content[0].text), { address: "p", tvl: 1 });
    assert.equal(seen[0], `GET /v1/pools/${POOL} 127.0.0.1`);

    const bad = await rpc(mcp.url, {
      jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "get_pool", arguments: { address: "nope" } },
    });
    assert.ok(bad.body.result?.isError || bad.body.error);
    assert.equal(seen.length, 1); // invalid input never reaches the API
  } finally {
    mcp.srv.close();
    fakeApi.srv.close();
  }
});
