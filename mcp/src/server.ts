import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { apiClient, ApiError, type Api } from "./api.js";
import { bearerOk, oauthUser } from "./auth.js";
import { botsClient, type Bots } from "./bots.js";
import { TOOLS } from "./tools.js";

const PORT = Number(process.env.MCP_PORT ?? 8100);
const API_URL = (process.env.API_URL ?? "http://api:8000").replace(/\/$/, "");
const TOKEN = process.env.MCP_BEARER_TOKEN ?? ""; // optional static token; OAuth is the normal way in
const PUBLIC_URL = (process.env.PUBLIC_API_URL ?? "https://lp.api.joulity.com").replace(/\/$/, "");
const VERSION = "0.1.0";

const INSTRUCTIONS =
  "LP Solana Dash: Meteora DLMM pool data and LP signals. Start with best_pools_now or search_pools, then " +
  "pool_signal / get_pool_bins / pool_history for a pool, and exit_check for an open range. Signals are heuristics: " +
  "treat them as analysis, not advice. The user's bots: my_bots, bot_strategies, and suggest_bot_settings to propose " +
  "a take profit / stop loss (real-money bots change only when the user clicks Apply). No tool can trade, sign, fund " +
  "or withdraw.";

export function buildServer(api: Api, bots: Bots | null = null): McpServer {
  const server = new McpServer({ name: "lp-solana-dash", version: VERSION }, { instructions: INSTRUCTIONS });
  for (const t of TOOLS) {
    server.registerTool(
      t.name,
      {
        title: t.title,
        description: t.description,
        inputSchema: t.input,
        annotations: { readOnlyHint: !t.write, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      },
      async (args: unknown) => {
        try {
          const data = await t.run(api, args, bots);
          return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
        } catch (e) {
          const msg = e instanceof ApiError ? `API ${e.status}: ${e.message}` : (e as Error).message;
          return { isError: true, content: [{ type: "text" as const, text: msg }] };
        }
      },
    );
  }
  return server;
}

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...headers }).end(JSON.stringify(body));
}

function clientIp(req: IncomingMessage): string | undefined {
  const fwd = req.headers["x-forwarded-for"];
  const first = (Array.isArray(fwd) ? fwd[0] : fwd)?.split(",")[0]?.trim();
  return first || req.socket.remoteAddress || undefined;
}

export async function handle(
  req: IncomingMessage,
  res: ServerResponse,
  token = TOKEN,
  apiUrl = API_URL,
  publicUrl = PUBLIC_URL,
) {
  const path = (req.url ?? "/").split("?")[0];
  if (path === "/health") return send(res, 200, { ok: true, version: VERSION, tools: TOOLS.length });
  if (path !== "/mcp") return send(res, 404, { error: "not found" });
  // OAuth access token (claude.ai, Claude Desktop, Claude Code) or the optional static token (scripts)
  const staticOk = bearerOk(req.headers.authorization, token);
  const user = staticOk ? null : await oauthUser(req.headers.authorization, apiUrl, clientIp(req));
  const authed = staticOk || user !== null;
  if (!authed) {
    const meta = `${publicUrl}/.well-known/oauth-protected-resource/mcp`;
    return send(res, 401, { error: "invalid_token", error_description: "sign in with OAuth" }, {
      "www-authenticate": `Bearer resource_metadata="${meta}", scope="mcp:read bots:suggest"`,
    });
  }
  if (req.method !== "POST") {
    // stateless server: no standalone SSE stream and no sessions to delete
    return send(res, 405, { jsonrpc: "2.0", error: { code: -32000, message: "Method not allowed" }, id: null }, {
      allow: "POST",
    });
  }
  // the user's own token goes to the API only for their bots (bots.ts); the static token has no user
  const bots = user && req.headers.authorization ? botsClient(apiUrl, req.headers.authorization, clientIp(req)) : null;
  const server = buildServer(apiClient(apiUrl, clientIp(req)), bots);
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  res.on("close", () => {
    void transport.close();
    void server.close();
  });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res);
  } catch (e) {
    console.error("mcp request failed", e);
    if (!res.headersSent) {
      send(res, 500, { jsonrpc: "2.0", error: { code: -32603, message: "internal error" }, id: null });
    }
  }
}

if (process.argv[1]?.endsWith("server.js")) {
  if (TOKEN && TOKEN.length < 24) {
    console.error("MCP_BEARER_TOKEN is too short (>= 24 chars) or leave it empty for OAuth only");
    process.exit(1);
  }
  createServer((req, res) => void handle(req, res)).listen(PORT, "0.0.0.0", () =>
    console.log(`lp-mcp listening on :${PORT}, ${TOOLS.length} tools, api ${API_URL}`),
  );
}
