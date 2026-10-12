# Your bots (paper and real money)

On the /bot page a signed-in user creates bots. Each bot **follows one house strategy** (`auto`, `v2_wide`, ...), the
strategies the bot service runs all the time on real pool data:

| house position | your bot |
|---|---|
| opens | opens one too, if the bot has a free slot (`max_open`) and is not stopped for the day |
| net % reaches your take profit / stop loss | closes (`take_profit` / `stop_loss`) |
| starts unwinding (real money) | closes its position and offers its tokens in 5 ask bins above the price |
| closes | closes too (same exit reason) |

- **Paper**: the result is the bot's size x the house position's net %. No wallet, no money.
- **Real money**: a real Meteora spot position from the bot's own wallet (half the SOL swapped into the token with
  Jupiter, +/- the same bins, max 34). The result is measured from the wallet's SOL balance around each step, so it
  includes every swap, fee, tip and the non-refundable bin array rent. Only spot shapes, so only `auto` for now.

Per bot: size per trade (SOL), max open, take profit %, stop loss %, daily loss limit and daily target (SOL; no new
trades for the rest of the UTC day once reached). A real-money bot always keeps 0.05 SOL for fees and rent.

## Who can do what

| action | needs |
|---|---|
| create a paper bot, change settings, pause / resume, stop, delete | signed in (web session) |
| suggest take profit / stop loss | the MCP connection (`bots:suggest` scope): a paper bot takes it at once, a real-money bot shows "Claude suggests ... Apply" |
| create a real-money bot | signed in + your wallet signs a message + your wallet is on `LIVE_WALLETS` |
| fund | a transfer you sign in your wallet, from the owner wallet to the bot wallet |
| close all, withdraw, delete a real-money bot | the owner wallet signs a fresh message (2 min, single use); a withdrawal goes only to the owner wallet |

No MCP tool can trade, sign, fund, close or withdraw. Real money is invite-only (`LIVE_WALLETS`): holding other
people's funds needs a lawyer, terms and a security review first.

## Keys

- One secret: `BOT_MASTER_SEED` (32 random bytes, hex) in the server `.env`, given only to the executor, removed from
  its environment at start. It is not in git, not in the database, not in the bot, API or MCP services.
- Each bot's wallet = `HMAC-SHA256(master, "lp-dash-bot:{bot_id}:{owner wallet}")` as an ed25519 seed, derived in memory
  when needed. A request naming another owner derives another (empty) wallet.
- **Back up the master seed** (password manager). With it and a bot's id and owner, its wallet can be rebuilt; without
  it, the funds in every bot wallet are lost if the server is lost.

## Turning real money on

1. `RPC_URL` = a Helius URL (public RPCs drop transactions).
2. Generate the seed on the server (prints nothing):
   `echo "BOT_MASTER_SEED=$(openssl rand -hex 32)" >> .env && chmod 600 .env`
3. `LIVE_WALLETS=<your wallet address>` and `LIVE_ENABLED=true`, then rebuild executor and bot.
4. On /bot: connect the wallet, New bot, Real money, sign, then "Send to bot wallet".
5. Kill switches: "Close all now" on the bot, or `LIVE_ENABLED=false` (nothing new opens; closing still works).

History: the first live mirror (one global wallet, `live_positions`) was replaced by these bots on 2026-10-12.
