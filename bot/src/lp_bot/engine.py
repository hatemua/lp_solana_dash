"""A virtual position stepped minute by minute: fee credit, flip, exits. Pure (no I/O); persisted as JSON."""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .sim import Bin, Leg, bid_ask, bid_bins_below, even_bids_below, fee_share
from .strategies import ExitRules

TX_SOL = 0.0002                 # per on-chain action (open, close, flip), incl. priority fee
LP_SHARE = 0.95                 # DLMM pools keep ~5% of swap fees as protocol fee
SOFT_EXITS = ("take_profit", "trailing", "fee_death", "time")     # no danger: sell through the pool, not a swap


@dataclass
class Position:
    strategy: str
    pool: str
    name: str
    opened_at: float
    entry_price: float
    capital_sol: float
    step: float
    sell_cost: float
    legs: list[Leg]
    idle_sol: float = 0.0
    fees_sol: float = 0.0
    costs_sol: float = 0.0
    peak_net: float = -1e9
    trail_on: bool = False
    low_since_fill: float | None = None
    flipped: bool = False
    out_below: int = 0
    out_above: int = 0
    last_ts: float = 0.0
    last_price: float = 0.0
    fee_log: deque = field(default_factory=lambda: deque(maxlen=30))     # our fees (SOL) per minute
    px_log: deque = field(default_factory=lambda: deque(maxlen=60))      # (ts, price) for the crash exit
    info: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    # ------------------------------------------------------------------ valuation
    def tokens(self, p: float) -> float:
        return sum(lg.tokens(p) for lg in self.legs)

    def value(self, p: float) -> float:
        return self.idle_sol + sum(lg.value(p) for lg in self.legs)

    def net_pct(self, p: float) -> float:
        liquidation = self.value(p) - self.tokens(p) * p * self.sell_cost
        return ((liquidation + self.fees_sol - self.costs_sol) / self.capital_sol - 1) * 100

    def deployed(self, p: float) -> float:
        return sum(lg.value(p) for lg in self.legs)

    # ------------------------------------------------------------------ one minute
    def step_minute(self, ts: float, price: float, pool_fees_usd: float, sol_usd: float, others_usd: Any,
                    rules: ExitRules, tvl_drop_15m: float | None = None, danger: str | None = None) -> str | None:
        """Advance to `ts`; returns an exit reason or None."""
        prev = self.last_price or price
        earned = fee_share(self.legs, prev, price, self.step, pool_fees_usd * LP_SHARE, sol_usd, others_usd) / sol_usd
        self.fees_sol += earned
        self.fee_log.append(earned)
        self.px_log.append((ts, price))
        self.last_ts, self.last_price = ts, price

        if rules.flip and not self.flipped and not self.info.get("unwind"):
            self._maybe_flip(price, rules)

        net = self.net_pct(price)
        self.peak_net = max(self.peak_net, net)
        lowest = min((lg.lowest() for lg in self.legs if lg.bins), default=price)
        highest = max((lg.highest() for lg in self.legs if lg.bins), default=price)
        self.out_below = self.out_below + 1 if price < lowest else 0
        self.out_above = self.out_above + 1 if price > highest and self.tokens(price) == 0 else 0
        age_min = (ts - self.opened_at) / 60

        hard = self._hard_exit(ts, price, tvl_drop_15m, danger)
        if hard:
            return hard
        if self.info.get("unwind"):
            return self._unwind_exit(ts, price, net, rules)
        if net >= rules.take_profit:
            return "take_profit"
        if net >= rules.trail_trigger:
            self.trail_on = True
        if self.trail_on and net <= self.peak_net - rules.trail_drop:
            return "trailing"
        if net <= rules.stop:
            return "stop"
        if rules.crash_pct is not None:
            recent = [px for t, px in self.px_log if t >= ts - rules.crash_window_min * 60]
            if recent and (price / max(recent) - 1) * 100 <= rules.crash_pct:
                return "crash"
        if rules.flip_stop and self.flipped and self.low_since_fill and price < self.low_since_fill:
            return "flip_failed"
        if self.out_below >= rules.out_below_min:
            return "out_below"
        if self.out_above >= rules.out_above_min:
            if rules.recenter_above and self.info.get("recenters", 0) < rules.max_recenters:
                self._recenter_below(price)
            else:
                return "out_above"
        sell_leg = next((lg for lg in self.legs if lg.name in ("F", "T")), None)
        if sell_leg:
            sol_back, tok_left = sell_leg.sol_held(price), sell_leg.tokens(price) * price
            if sol_back >= 0.9 * (sol_back + tok_left):
                return "flip_done" if sell_leg.name == "F" else "sold_out"
        deployed = self.deployed(price)
        if age_min >= rules.fee_death_min and deployed > 0 and len(self.fee_log) >= rules.fee_death_min:
            rate = sum(self.fee_log) * 60 / len(self.fee_log) / deployed * 100
            if rate < rules.fee_death_rate and net >= 0:
                return "fee_death"
        if age_min >= rules.max_hours * 60:
            return "time"
        return None

    def _recenter_below(self, price: float) -> None:
        """All legs are SOL and the price is above them: rebuild the same ladder right under the new price."""
        sol = sum(lg.sol_held(price) for lg in self.legs)
        n = max(len(lg.bins) for lg in self.legs)
        amounts = [b.sol for lg in self.legs for b in lg.bins if b.sol > 0]
        even = bool(amounts) and max(amounts) <= min(amounts) * 1.01          # keep the ladder's shape
        self.legs = [Leg("A", (even_bids_below if even else bid_bins_below)(price, n, self.step, sol))]
        self.out_above = 0
        self.costs_sol += 2 * TX_SOL
        self.info["recenters"] = self.info.get("recenters", 0) + 1

    def _hard_exit(self, ts: float, price: float, tvl_drop_15m: float | None, danger: str | None) -> str | None:
        if tvl_drop_15m is not None and tvl_drop_15m <= -0.3:
            return "rug"                                   # LPs pulled 30% of the pool within 15 min
        last3 = [px for t, px in self.px_log if t >= ts - 3 * 60]
        if last3 and (price / max(last3) - 1) * 100 <= -6:
            return "fast_drop"                             # -6% within 3 minutes: don't wait for the stop
        if danger and self.tokens(price) > 0:
            return danger                                  # early crash warning while we hold the token
        return None

    def start_unwind(self, price: float, rules: ExitRules, reason: str) -> bool:
        """Soft exit: instead of swapping the tokens back (2-15% on these pools), offer them in a few ask bins right
        above the price and let traders buy them (we earn the fee). False when there is little left to sell."""
        toks = self.tokens(price)
        if toks * price < 0.05 * self.capital_sol:
            return False
        n, r = rules.unwind_bins, 1 + self.step
        self.idle_sol += sum(lg.sol_held(price) for lg in self.legs)
        self.legs = [Leg("U", [Bin(price=price * r ** k, tok=toks / n, side="ask") for k in range(1, n + 1)])]
        self.costs_sol += 2 * TX_SOL
        self.info["unwind"] = {"ts": self.last_ts, "reason": reason, "price": price, "tokens": toks}
        return True

    def _unwind_exit(self, ts: float, price: float, net: float, rules: ExitRules) -> str | None:
        u = self.legs[0]
        sold, left = u.sol_held(price), u.tokens(price) * price
        if sold >= 0.9 * (sold + left):
            return "unwound"
        if net <= rules.stop:
            return "stop"
        if ts - self.info["unwind"]["ts"] >= rules.unwind_max_min * 60:
            return "unwind_timeout"                        # the rest is swapped back at the real quote
        return None

    def _maybe_flip(self, price: float, rules: ExitRules | None = None) -> None:
        rules = rules or ExitRules()
        a = next((lg for lg in self.legs if lg.name == "A"), None)
        if a is None or a.converted(price) < rules.flip_fill:
            return
        self.low_since_fill = min(self.low_since_fill or price, price)
        if price < self.low_since_fill * rules.flip_bounce:
            return
        toks = a.tokens(price)
        spent = sum(b.sol for b in a.bins if price <= b.price)
        avg = spent / toks if toks else price
        self.idle_sol += a.sol_held(price)
        self.legs.remove(a)
        top = avg * rules.flip_target / price
        if top > 1 + self.step:
            self.legs.append(Leg("F", bid_ask(price, 1 + self.step, top, self.step, toks, "ask")))
        else:                                              # already above the average: just sell
            self.idle_sol += toks * price * (1 - self.sell_cost)
        self.flipped = True
        self.costs_sol += 2 * TX_SOL
        self.info["flip"] = {"ts": self.last_ts, "price": price, "avg_buy": avg, "tokens": toks}

    def close(self, price: float) -> float:
        self.costs_sol += TX_SOL * max(1, len(self.legs))
        return self.net_pct(price)

    # ------------------------------------------------------------------ persistence
    def state(self) -> dict[str, Any]:
        return {"legs": [lg.to_dict() for lg in self.legs], "idle_sol": self.idle_sol, "fees_sol": self.fees_sol,
                "costs_sol": self.costs_sol, "peak_net": self.peak_net, "trail_on": self.trail_on,
                "low_since_fill": self.low_since_fill, "flipped": self.flipped, "out_below": self.out_below,
                "out_above": self.out_above, "last_ts": self.last_ts, "last_price": self.last_price,
                "fee_log": list(self.fee_log), "px_log": [list(x) for x in self.px_log], "info": self.info,
                "step": self.step, "sell_cost": self.sell_cost,
                "capital_sol": self.capital_sol}

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Position":
        s = row["state"]
        return Position(strategy=row["strategy"], pool=row["pool"], name=row["name"] or "",
                        opened_at=row["opened_at"].timestamp(), entry_price=row["entry_price"],
                        capital_sol=s["capital_sol"], step=s["step"], sell_cost=s["sell_cost"],
                        legs=[Leg.from_dict(d) for d in s["legs"]], idle_sol=s["idle_sol"], fees_sol=s["fees_sol"],
                        costs_sol=s["costs_sol"], peak_net=s["peak_net"], trail_on=s["trail_on"],
                        low_since_fill=s["low_since_fill"], flipped=s["flipped"], out_below=s["out_below"],
                        out_above=s["out_above"], last_ts=s["last_ts"], last_price=s["last_price"],
                        fee_log=deque(s["fee_log"], maxlen=30),
                        px_log=deque((tuple(x) for x in s.get("px_log", [])), maxlen=60), info=s["info"],
                        id=row["id"])
