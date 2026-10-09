# Reverse-engineering the 14 profitable wallets (2026-10-09)

Data: SolanaTracker positions for the 14 v3 wallets (4,735 positions, 3,323 with ≥ $20 invested), Jupiter token info (age, mcap), Jupiter 15-min charts for the swing wallets' tokens (1,524 tokens). Scripts in `ideas/reverse/` (key file via `ST_KEY_FILE`, never committed).

## Three types
| Type | Wallets | Behaviour | Replicable? |
|---|---|---|---|
| Launch snipers | EhrQ (89% of buys in the first minute, 76% win, avg loss −3.5%, hold ~0 min), A1mV (80% < 1 min), 4Bc4 (46% < 1 min, 31% 1–10 min) | buy at mcap < $20k in the first minute, sell in seconds/minutes | No (same-block speed, bundles) |
| Bot | 4ZvS (95% win, 5 buys / 1 sell, tokens > 7 d at "< $20k") | market-making / volume bot | No |
| Swing | AuT92, syfp, 4T8x, 291Y, AQPh, 8Kxi, 2WMJ, SF2Q, 3Zhq (+ part of CyT2) | tokens 10 min – 7+ days old, mcap $100k – $10M+, hold hours–days | studied below |

- Profit concentration: best 5% of positions = 144% (AuT92), 172% (4T8x), 369% (CyT2) of total profit → the other 95% lose net.
- Result by mcap at entry (all but EhrQ/4ZvS): < $20k +5.8% median (snipes); **$20–100k −16.5% median, 39% win (worst — the post-migration zone)**; $100k–1M +2.3%; **$1–10M +9.3%, 64% win**; > $10M +6.5%, 69% win.

## What the chart looks like when the swing wallets buy (370 entries with full chart context)
| | their entries (median) | all moments, same tokens |
|---|---|---|
| 1 h change | +7% | 0% |
| 24 h change | +24% | 0% |
| volume last 1 h / avg hour | 1.9× | 0.67× |
| from 7 d high | −25% | −33% |
| next 24 h close | −5% | 0% |
| next 24 h max gain / max drop | +22% / −25% | +13% / −12% |

They buy strength + volume; winners and losers look the same at entry.

## Rule tests (15-min bars, pessimistic stop-first, 1.2% round trip + gas)
Signal: 1 h ≥ +5%, 24 h ≥ 0, volume 1 h ≥ 1.5× average, 3 d ≤ +100%, mcap ≥ $1M. Set A = tokens they traded (599), set B = other tokens (74, 4 days).

| Exit | A avg | B avg | random entries A / B |
|---|---|---|---|
| TP20/SL20, 24 h | −0.9% | −4.0% | −1.4% / −3.7% |
| TP50/SL25 | +1.7% | −7.2% | |
| vol spike 3× | 0.0% | −5.6% | |
| ½ at +50%, trail 30%, stop −25%, 7 d | +0.2% | −14.0% | +0.5% / −7.9% |
| trail 40%, stop −30% | +2.4% | −12.3% | |

The momentum + volume entry is no better than random entries with the same exits; nothing survives on set B.

## Conclusion
Their edge is not visible in price/volume: it is speed (snipers), information/selection (which token, before others), and fat-tail luck (a few huge winners). A chart rule copied from them does not beat random. Useful takeaway: avoid the $20–100k post-migration zone; established $1M+ tokens are where the swing wallets win, but we have no entry signal for them yet.

## Swing "consensus" test (k swing wallets buy the same token within 48 h, entry +30 min, `ideas/reverse/consensus.py`)
| Signal (mcap ≥ $1M) | n | hold 72 h avg | median | TP30/SL20 3 d | ½@+50% trail 30% |
|---|---|---|---|---|---|
| k=1 (any swing wallet) | 261 | −7.9% | −30.6% | −3.2% | −7.5% |
| k=2 | 39 | +24.4% | −19.9% | −1.1% | −7.3% |
| k=3 | 7 | +75.6% | −19.9% | −7.1% | −18.2% |
| control: k=2 tokens at random times | 195 | +73.4% | −15.7% | −3.3% | +1.5% |
- Positive averages come from a few tokens that ran 5–10×; the median trade loses 20–30%. Tight exits (TP/SL) are negative everywhere.
- Random times on the same tokens do better than the consensus moment → the timing adds nothing; the value is in the tokens, and that is in-sample (wallets were picked for being profitable over this same period).
- Verdict: swing on $1M+ tokens is a lottery profile (many −20–30% trades, rare ×5–10). Not proven; a forward test needs wallets selected walk-forward and many trades.

## Correction (after the user pushed back): the swing wallets DO have an edge per trade
Earlier tables used the median trade; the **average** trade (equal size, pnl incl. open positions) is what makes money:
| Swing wallets (all 10) | days 30–16 ago | days 15–8 ago | last 7 days |
|---|---|---|---|
| avg ROI per trade | +21.0% (n=208, ±5.4%) | +8.2% (n=211, ±5.1%) | +16.3% (n=127, ±6.5%) |
- Consistent in every period: syfp (+13.6 / +11.8 / +7.9%, ~10 trades/day), 2WMJ (+52 / +16 / +47%), 3Zhq (+39 / +31 / +56%). AQPh, SF2Q good then fading. CyT2 negative in all three periods (its $ profit came from a few big bets); 4T8x and AuT92 unstable.
- How they win: **selling in parts**. Positions with 4+ sells: +25% $-ROI, +$497k; 1 sell: −15%, −$115k. Winners are closed fast (median 1.6 h, +28%); losers are held longer (7.1 h, −34%). Adding to positions (4+ buys) made +$206k.
- One trade's ROI has a standard deviation of ~73% → about **95 trades** are needed to tell +15% from 0. The live copy runs (11 and 5 copies) were far too small to judge — that is why they looked negative.
- Our earlier chart-rule and consensus tests replaced their exits with fixed TP/SL/hold rules and entered 30 min late; that threw away the part that works (their exits).

## Second correction: most of the "profit" is Meteora LP activity counted as zero-cost tokens
- Rebuilt every position from raw trades (SolanaTracker `/wallet/{w}/trades`, `ideas/reverse/ladder.py`): closed buy→sell positions average **−10.9%** (win 26%).
- Split of the positions endpoint: positions where the wallet sold **more tokens than it bought** ("extra-token") vs normal ones:

| | positions | P&L | avg ROI |
|---|---|---|---|
| normal (sold ≤ bought) | 1,202 | **−$14,395** | +2.0% |
| extra-token (sold > bought) | 520 | **+$399,203** | +47.3% |

- On-chain check (3Zhq, token FLY): the extra tokens come from **Meteora DLMM** (`LBUZKh…` program, pool `4Hxjd3…`): add SOL liquidity, remove liquidity (tokens come back), re-add, every 1–5 minutes, then sell the tokens via Jupiter. These wallets are **active DLMM LPs**; the tracker counts LP withdrawals as free tokens (no cost), so their reported profit is inflated. Summing the SOL flows of the visible FLY transactions gives roughly −0.13 SOL, while the tracker shows +$59 realized on $41.
- Real buy→sell trading by these wallets ≈ break-even. Their actual method = Meteora DLMM LP with frequent re-centering → test it with the LP Shadow (`ideas/lp-shadow-prompt.md`) and measure their true P&L from SOL flows.
