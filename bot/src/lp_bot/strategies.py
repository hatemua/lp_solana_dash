"""The strategies shadow-tested side by side. Each one: a screen, an entry signal, the legs to open, exit rules.

S1 topped_bid  LP playbook v1: after a pump tops out, SOL-only bid-ask -5%..-55%; flip filled tokens into an ask
               above the price on the bounce. (docs/research/strategy-playbook.md, backtest/playbook_v1.py)
S2 meridian    Meridian agent defaults: mid-size pools, SOL-only bid-ask in the 69 bins right below the price;
               out of range 30 min, stop -15%, take profit +5%, trailing.
S3 chop_spot   Anti-sawtooth: two-sided spot around the price in choppy pools, width from volatility; close when
               out of range 15 min (re-entered after the cooldown = re-center).
S1b topped_bid_v2  S1 + token >= 24 h old + crash exit (-25% in 30 min) + stop when the bounce fails after a flip.
S4 evil_panda  SOL-only bid-ask -60%..-90% on established tokens; waits for a deep dump, flips the bounce, 24 h.
S5 grid        two-sided spot close to the price (buys below, sells above), +/-1.5 sigma over 1 h (6-20 bins a
               side), tokens >= 24 h, crash exit.
S6 fee_leader  the table's leaders by fee/TVL (>= 2%/h), basic safety only (no age floor), same spot grid, 2 h max.
S2b meridian_trail  S2, but when the price runs above the bids they move up under it (no upside left on the table).
S7 rabbit      rising token (+10%..+60% in 1 h, fees accelerating): buy it, sell it back into the pump with a
               token-side bid-ask from +1% to +30%; done when sold out, stop -8%.
Swap costs (two-sided entries, selling tokens on exit) are real Jupiter quotes, see jupiter.py.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from .signals import Bar, chop_stats, fee_velocity, price_change, topped
from .sim import Leg, bid_ask, bid_bins_below, spot_two_sided

SELL_COST_ESTIMATE = 0.015         # marks open positions; the exit uses a real Jupiter quote


@dataclass
class Candidate:
    pool: str
    name: str
    price: float
    tvl: float
    fees_1h: float
    fee_tvl_1h: float               # percent
    bin_step: int
    base_fee_pct: float
    mcap: float
    holders: int
    organic: float
    top10: float
    mint_off: bool
    freeze_off: bool
    bars: list[Bar] = field(default_factory=list)
    token_age_h: float | None = None
    mint: str = ""
    decimals: int = 6

    @property
    def step(self) -> float:
        return self.bin_step / 10_000

    @property
    def sell_cost(self) -> float:
        return SELL_COST_ESTIMATE


@dataclass
class Plan:
    legs: list[Leg]
    idle_sol: float
    buy_sol: float                  # SOL swapped into the token at entry (cost from a live quote)
    info: dict[str, Any]


@dataclass
class ExitRules:
    take_profit: float = 5.0
    trail_trigger: float = 3.0
    trail_drop: float = 1.5
    stop: float = -15.0
    out_below_min: int = 30
    out_above_min: int = 30
    fee_death_min: int = 30
    fee_death_rate: float = 0.5        # our fees, % of deployed capital per hour
    max_hours: float = 6.0
    flip: bool = False
    cooldown_min: int = 60
    crash_pct: float | None = None     # exit if the price is this % below its high of the last crash_window_min
    crash_window_min: int = 30
    flip_stop: bool = False            # after a flip, exit if the price breaks below the pre-bounce low
    recenter_above: bool = False       # SOL bids: when the price stays above them, move them up under it
    max_recenters: int = 20


def safe(c: Candidate, top10_max: float = 30) -> bool:
    return c.mint_off and c.freeze_off and c.holders >= 500 and c.top10 <= top10_max and c.mcap >= 250_000


class Strategy:
    name = "base"
    label = ""
    rules = ExitRules()

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        raise NotImplementedError

    def rank(self, c: Candidate) -> float:
        return c.fee_tvl_1h


class ToppedBid(Strategy):
    name = "topped_bid"
    label = "S1 Topped bid + flip"
    rules = ExitRules(flip=True, max_hours=6)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not safe(c) or c.fees_1h < 500:
            return None
        sig = topped(c.bars, c.price)
        if not sig:
            return None
        legs = [Leg("A", bid_ask(c.price, 0.45, 0.95, c.step, capital_sol * 0.9, "bid"))]
        return Plan(legs, idle_sol=capital_sol * 0.1, buy_sol=0.0, info=sig)


class Meridian(Strategy):
    name = "meridian"
    label = "S2 Meridian bid-ask"
    rules = ExitRules(take_profit=5, stop=-15, out_below_min=30, out_above_min=30, max_hours=12)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not (10_000 <= c.tvl <= 150_000 and 150_000 <= c.mcap <= 10_000_000 and c.holders >= 500
                and c.organic >= 60 and c.top10 <= 60 and 80 <= c.bin_step <= 125 and c.mint_off and c.freeze_off):
            return None
        fv = fee_velocity(c.bars)
        ch = price_change(c.bars, c.price)
        if c.fee_tvl_1h < 0.5 or fv is None or fv < 1.0 or ch is None or not (-0.15 <= ch <= 0.30):
            return None
        legs = [Leg("A", bid_bins_below(c.price, 69, c.step, capital_sol))]
        return Plan(legs, idle_sol=0.0, buy_sol=0.0, info={"fee_velocity": fv, "change_1h": ch})


class ChopSpot(Strategy):
    name = "chop_spot"
    label = "S3 Chop spot (anti-sawtooth)"
    rules = ExitRules(take_profit=5, stop=-10, out_below_min=15, out_above_min=15, max_hours=6, cooldown_min=15)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not safe(c) or c.fees_1h < 500:
            return None
        st = chop_stats(c.bars)
        fv = fee_velocity(c.bars)
        if st["chop"] is None or st["chop"] < 5 or abs(st["trend"] or 0) >= 1 or not (0.01 <= (st["vol"] or 0) <= 0.05):
            return None
        if fv is None or fv < 1.0:
            return None
        width = 2 * st["vol"] * math.sqrt(12)                    # +/- 2 sigma over ~1 h
        n = max(5, min(34, round(math.log(1 + width) / math.log(1 + c.step))))
        half = capital_sol / 2                                   # half the SOL is swapped into the token
        legs = [Leg("S", spot_two_sided(c.price, n, c.step, half, half / c.price))]
        return Plan(legs, idle_sol=0.0, buy_sol=half, info={**st, "fee_velocity": fv, "bins_each_side": n})

    def rank(self, c: Candidate) -> float:
        return (chop_stats(c.bars)["chop"] or 0) * c.fee_tvl_1h


def old_enough(c: Candidate, hours: float = 24) -> bool:
    return c.token_age_h is not None and c.token_age_h >= hours


def grid_plan(c: Candidate, capital_sol: float, sigmas: float, info: dict[str, Any]) -> Plan | None:
    """Two-sided spot close to the price: that is where the swaps (and the fees) happen."""
    st = chop_stats(c.bars)
    if not st["vol"]:
        return None
    width = sigmas * st["vol"] * math.sqrt(12)
    n = max(6, min(20, round(math.log(1 + width) / math.log(1 + c.step))))
    half = capital_sol / 2
    legs = [Leg("G", spot_two_sided(c.price, n, c.step, half, half / c.price))]
    return Plan(legs, idle_sol=0.0, buy_sol=half, info={**info, "vol": st["vol"], "bins_each_side": n})


class ToppedBidV2(ToppedBid):
    name = "topped_bid_v2"
    label = "S1b Topped bid v2 (age, crash exit)"
    rules = ExitRules(flip=True, max_hours=6, crash_pct=-25, flip_stop=True)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        return super().entry(c, capital_sol) if old_enough(c) else None


class EvilPanda(Strategy):
    name = "evil_panda"
    label = "S4 Evil Panda deep bid"
    rules = ExitRules(flip=True, take_profit=5, stop=-20, out_below_min=60, out_above_min=24 * 60, max_hours=24,
                      fee_death_min=24 * 60, cooldown_min=120)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not (safe(c) and old_enough(c) and c.fees_1h >= 300 and c.bin_step >= 80):
            return None
        ch = price_change(c.bars, c.price)
        if ch is None or ch < 0.10:                  # Evil Panda scanner: the coin is pumping (+10% in 1 h)
            return None
        legs = [Leg("A", bid_ask(c.price, 0.10, 0.40, c.step, capital_sol, "bid"))]
        return Plan(legs, idle_sol=0.0, buy_sol=0.0, info={"change_1h": ch})


class Grid(Strategy):
    name = "grid"
    label = "S5 Two-sided grid"
    rules = ExitRules(take_profit=5, stop=-12, out_below_min=30, out_above_min=30, max_hours=6, crash_pct=-25)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not (safe(c) and old_enough(c) and c.fees_1h >= 500):
            return None
        st = chop_stats(c.bars)
        fv = fee_velocity(c.bars)
        if st["trend"] is None or abs(st["trend"]) >= 2 or not (0.01 <= (st["vol"] or 0) <= 0.08):
            return None
        if fv is None or fv < 0.8:
            return None
        return grid_plan(c, capital_sol, 1.5, {"trend": st["trend"], "fee_velocity": fv})

    def rank(self, c: Candidate) -> float:
        return (chop_stats(c.bars)["chop"] or 1) * c.fee_tvl_1h


class FeeLeader(Strategy):
    name = "fee_leader"
    label = "S6 Fee leaders (table top)"
    rules = ExitRules(take_profit=5, trail_trigger=3, stop=-10, out_below_min=15, out_above_min=15, max_hours=2,
                      crash_pct=-20, crash_window_min=15, fee_death_min=20, cooldown_min=30)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not (c.mint_off and c.freeze_off and c.top10 <= 30 and c.holders >= 300):
            return None
        if c.fee_tvl_1h < 2.0 or c.fees_1h < 1_000:
            return None
        return grid_plan(c, capital_sol, 1.5, {"fee_tvl_1h": c.fee_tvl_1h, "token_age_h": c.token_age_h})


class MeridianTrail(Meridian):
    name = "meridian_trail"
    label = "S2b Meridian trail (re-center up)"
    rules = ExitRules(take_profit=5, stop=-15, out_below_min=30, out_above_min=5, max_hours=12, recenter_above=True)


class Rabbit(Strategy):
    name = "rabbit"
    label = "S7 Rabbit (sell into the pump)"
    rules = ExitRules(take_profit=6, trail_trigger=3, trail_drop=1.5, stop=-8, out_below_min=10,
                      out_above_min=24 * 60, max_hours=3, crash_pct=-12, crash_window_min=15, fee_death_min=30,
                      cooldown_min=60)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if not (safe(c) and old_enough(c) and c.fees_1h >= 1_000):
            return None
        ch = price_change(c.bars, c.price)
        fv = fee_velocity(c.bars)
        if ch is None or not (0.10 <= ch <= 0.60) or fv is None or fv < 1.2:
            return None
        tokens = capital_sol / c.price
        legs = [Leg("T", bid_ask(c.price, 1 + c.step, 1.30, c.step, tokens, "ask"))]
        return Plan(legs, idle_sol=0.0, buy_sol=capital_sol, info={"change_1h": ch, "fee_velocity": fv})

    def rank(self, c: Candidate) -> float:
        return (fee_velocity(c.bars) or 0) * c.fee_tvl_1h


STRATEGIES: list[Strategy] = [ToppedBid(), Meridian(), ChopSpot(), ToppedBidV2(), EvilPanda(), Grid(), FeeLeader(),
                              MeridianTrail(), Rabbit()]
BY_NAME = {s.name: s for s in STRATEGIES}
