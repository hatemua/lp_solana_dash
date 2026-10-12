# Live bot (real money)

The paper bot always runs. With `BOT_LIVE=true` it also mirrors one strategy (`LIVE_STRATEGY`, default `auto`) with
small real Meteora DLMM positions:

| paper position | real position (bot wallet) |
|---|---|
| opens (spot shapes only) | swap half the SOL into the token (Jupiter), open a spot position, +/- the same bins (max 34) |
| starts unwinding | close the position, offer all its tokens in 5 ask bins right above the price |
| closes | close everything, claim fees, swap every token of that pool back to SOL |

The result of a real position is measured from the bot wallet's SOL balance around each step (one wallet operation
runs at a time), so it includes every swap, fee, tip and the non-refundable bin array rent.

## Limits

- `LIVE_POSITION_SOL` per position (0.1), `LIVE_MAX_OPEN` at once (1), a 0.05 SOL reserve kept for fees and rent.
- `LIVE_DAILY_LOSS_SOL` (0.05): no new real positions for the rest of the UTC day below it.
- The owner wallet (`BOT_OWNER_WALLET`) can, from the /bot page, sign a message (no transaction) to: pause, resume,
  close all now, withdraw all. A withdrawal can only go to the owner wallet. The signed message expires after 2 min
  and can't be reused.

## Keys

- The bot wallet key is `BOT_WALLET_SECRET` (JSON byte array) in the server `.env`, generated on the server. Only the
  executor container receives it; it is removed from the process environment at start and never logged or returned.
  It is not in git, not in the database, not in the bot, API or MCP services.
- Back it up: copy the line from the server `.env` into a password manager (or import it into a wallet app). If the
  server is lost without a backup, the funds in the bot wallet are lost.
- Funding: the /bot page builds a plain SOL transfer from the connected wallet to the bot wallet; the user signs it
  in their own wallet.
- No MCP tool can move funds: the MCP only calls read-only API routes.

## Turning it on

1. `RPC_URL` = a Helius URL (public RPCs drop transactions).
2. Generate the key on the server (prints only the address):
   `docker compose ... run --rm --no-deps --entrypoint node executor -e "const {Keypair}=require('@solana/web3.js');const k=Keypair.generate();console.log('BOT_WALLET_SECRET='+JSON.stringify(Array.from(k.secretKey)));console.error(k.publicKey.toBase58())" >> .env`
3. `BOT_OWNER_WALLET=<your wallet address>`, then rebuild executor, bot, api.
4. Fund the bot wallet from the /bot page, then `BOT_LIVE=true` and rebuild the bot.
5. Kill switch: "Close all now" on the /bot page, or `BOT_LIVE=false` and rebuild the bot.
