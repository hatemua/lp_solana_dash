# How Meteora DLMM works: what decides profit for an LP

Sources: Meteora docs ([dynamic fees](https://docs.meteora.ag/dlmm/dynamic-fees), [fee calculation](https://docs.meteora.ag/overview/products/dlmm/dlmm-fee-calculation), [SOL required for rent](https://docs.meteora.ag/getting-started/sol-required-for-rent), [DLMM FAQ](https://docs.meteora.ag/dlmm/dlmm-faq)). DLMM follows the Liquidity Book design.

## 1. Bins
- Liquidity sits in discrete **bins**; each bin is one price. Bin step `s` (in basis points) is the gap between bins: price(bin i) = base × (1 + s/10 000)^i.
- Bin step 100 → 1% per bin. A +30% range = ln(1.3)/ln(1.01) ≈ **26 bins**; bin step 50 → 53 bins; bin step 200 → 14 bins.
- Inside one bin the price is fixed (**zero slippage inside a bin**). A swap uses up the bin, then moves to the next one.

## 2. What each bin holds
- **Active bin** (current price): holds both token and SOL.
- **Bins above the price**: only the token. When buyers push the price up, your tokens are **sold for SOL** at each bin's price (you sell into the pump, bin by bin).
- **Bins below the price**: only SOL. When sellers push the price down, your SOL **buys the token** at each bin's price (you buy the dip, bin by bin).
- So an LP position = a ladder of limit orders that refills itself, plus fees.

## 3. Fees: who earns what
- Each swap pays fee rate × amount traded **in each bin it crosses**. That fee goes to the LPs **of that bin**, split by their share of that bin's liquidity.
- **Only bins the price actually trades through earn fees.** Liquidity far from the price earns nothing.
- Fee rate = **base fee + variable fee**:
  - base fee = base factor × bin step, fixed when the pool is created (memecoin pools are often 1–5%; the "fee tier");
  - variable fee = A × (volatility accumulator × bin step)². The accumulator grows with the **number of bins crossed recently**, decays after a filter period and resets after a decay period. So **fees go up automatically when the price moves fast** (pumps and dumps): that's the "fee boost".
- Protocol share: 10% of fees on standard pools (20% on launch pools), so LPs keep ~90%.

## 4. Costs
- Position rent ≈ 0.057–0.059 SOL, **refunded** when you close the position.
- Bin array rent ≈ 0.075 SOL, **not refunded**, paid only if you are the first to use those bins (avoid fresh, empty price zones).
- Ranges over 69 bins need extra rent (refundable).
- Transaction fees for every add, remove and claim, plus the swap cost when you sell the tokens you receive.

## 5. P&L of a position
**P&L = fees earned + value change of what you hold − costs**
- Price goes up through your range: you end up holding more SOL (sold the pump). Good, plus fees.
- Price chops up and down inside your range: you sell high, buy low again and again, plus fees on every pass. **This is where LPs make money.**
- Price falls out of the bottom of your range: you end up holding only the token, and it keeps falling. This is the loss ("impermanent loss" / getting dumped on).
- Price leaves the top of your range: you hold only SOL and earn nothing more until you move the range.

## 6. What the wallets we found do
(3Zhq / 2WMJ / syfp, from on-chain transactions)
- Add liquidity in a narrow range near the price, **remove and re-add every 1–5 minutes** to stay in the active bin (where all the fees are), then **sell the tokens they receive** right away via Jupiter.
- Effect: they keep a high share of the active bin (high fees) and do not stay exposed to a dump for long. They pay for this with transaction fees, rent churn and selling costs.

## 7. When it works and when it fails
- Works: **lots of volume going back and forth** (high fees, price returns) and a high fee tier or variable fee (volatile but not one-way).
- Fails: **one-way dumps** (inventory loses faster than fees come in); thin pools where your share is tiny; paying non-refundable bin-array rent often.
- The numbers to measure live: our share of the active bin, fees per minute, price drift per minute, cost per re-center, and the result per token type (age, volume, fee tier).
