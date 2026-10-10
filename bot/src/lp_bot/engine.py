"""A virtual position stepped minute by minute: fee credit, flip, exits. Pure (no I/O); persisted as JSON."""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .sim import Leg, bid_ask, fee_share
from .strategies import ExitRules

TX_SOL = 0.0002                 # per on-chain action (open, close, flip), incl. priority fee
LP_SHARE = 0.95                 # DLMM pools keep ~5% of swap fees as protocol fee


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
                    rules: ExitRules, tvl_drop_15m: float | None = None) -> str | None:
        """Advance to `ts`; returns an exit reason or None."""
        prev = self.last_price or price
        earned = fee_share(self.legs, prev, price, self.step, pool_fees_usd * LP_SHARE, sol_usd, others_usd) / sol_usd
        self.fees_sol += earned
        self.fee_log.append(earned)
        self.px_log.append((ts, price))
        self.last_ts, self.last_price = ts, price

        if rules.flip and not self.flipped:
            self._maybe_flip(price)

        net = self.net_pct(price)
        self.peak_net = max(self.peak_net, net)
        lowest = min((lg.lowest() for lg in self.legs if lg.bins), default=price)
        highest = max((lg.highest() for lg in self.legs if lg.bins), default=price)
        self.out_below = self.out_below + 1 if price < lowest else 0
        self.out_above = self.out_above + 1 if price > highest and self.tokens(price) == 0 else 0
        age_min = (ts - self.opened_at) / 60

        if tvl_drop_15m is not None and tvl_drop_15m <= -0.5:
            return "rug"
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
            return "out_above"
        flip_leg = next((lg for lg in self.legs if lg.name == "F"), None)
        if flip_leg:
            sol_back, tok_left = flip_leg.sol_held(price), flip_leg.tokens(price) * price
            if sol_back >= 0.9 * (sol_back + tok_left):
                return "flip_done"
        deployed = self.deployed(price)
        if age_min >= rules.fee_death_min and deployed > 0 and len(self.fee_log) >= rules.fee_death_min:
            rate = sum(self.fee_log) * 60 / len(self.fee_log) / deployed * 100
            if rate < rules.fee_death_rate and net >= 0:
                return "fee_death"
        if age_min >= rules.max_hours * 60:
            return "time"
        return None

    def _maybe_flip(self, price: float) -> None:
        a = next((lg for lg in self.legs if lg.name == "A"), None)
        if a is None or a.converted(price) < 0.7:
            return
        self.low_since_fill = min(self.low_since_fill or price, price)
        if price < self.low_since_fill * 1.05:
            return
        toks = a.tokens(price)
        spent = sum(b.sol for b in a.bins if price <= b.price)
        avg = spent / toks if toks else price
        self.idle_sol += a.sol_held(price)
        self.legs.remove(a)
        top = avg * 1.10 / price
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
                "fee_log": list(self.fee_log), "px_log": [list(x) for x in self.px_log], "info": self.info, "step": self.step, "sell_cost": self.sell_cost,
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
