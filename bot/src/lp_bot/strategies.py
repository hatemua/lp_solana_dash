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
V2 v2_wide / v2_calm / v2_bid_flip  the pro shapes with careful entries (calm token, real round trip <= 3%, pool
               price = market), confirmed warnings, longer holds, and soft exits that sell through the pool.
Swap costs (two-sided entries, selling tokens on exit) are real Jupiter quotes, see jupiter.py.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from .signals import Bar, chop_stats, fee_velocity, price_change, topped
from .sim import Leg, bid_ask, bid_bins_below, even_bids_below, spot_two_sided

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
    fees_24h: float = 0.0
    buy_vol_1h: float = 0.0
    sell_vol_1h: float = 0.0
    traders_1h: int = 0
    bot_holders_pct: float = 0.0
    price_change_1h_pct: float | None = None   # from Jupiter (all venues)
    fee_momentum: float | None = None           # fees/min last 15 min vs the hour before (per-minute data)
    flow: Any = None                            # guards.TokenFlow
    tvl_change_30m: float | None = None

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
    flip_fill: float = 0.7             # flip once this share of the bids has filled ...
    flip_bounce: float = 1.05          # ... and the price is this far above the low since the fill
    flip_target: float = 1.10          # asks up to this multiple of the average buy price
    unwind: bool = False               # soft exits sell the tokens through the pool (ask bins), not with a swap
    unwind_bins: int = 5
    unwind_max_min: int = 60
    unwind_cut: float = 0.05             # CRAWL 2026-10-12: held to the timeout while falling, -11.4%
    confirm_warnings: bool = False     # sell_pressure / dumping must show on 2 ticks in a row and not in the first
    warn_min_age_min: int = 10         # minutes (fast drop, rug and stop still exit at once)


def safe(c: Candidate, top10_max: float = 30) -> bool:
    return c.mint_off and c.freeze_off and c.holders >= 500 and c.top10 <= top10_max and c.mcap >= 250_000


class Strategy:
    name = "base"
    label = ""
    rules = ExitRules()
    careful = False                    # V2 entry: calm token, cheap real round trip, pool price = market price

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


def pro_screen(c: Candidate) -> list[str]:
    """Pool and token checks used by experienced Meteora LPs, tuned with our own study (backtest/lp_study.py).

    Returns the failed checks (empty = pass). Sources: Meteora pool discovery (fees / active TVL, TVL floor),
    LP Army bootcamp (mcap, 1 h fees), Evil Panda / GMGN (holders, top 10, bots), practical DLMM guide (sustained,
    two-way activity, no entry after a vertical move), Meridian (bin step 80-125, bot holders <= 30%).
    """
    bad = []
    if not (10_000 <= c.tvl <= 300_000):
        bad.append("tvl")                      # our study: pools > $500k TVL won 25% of the time
    if not (80 <= c.bin_step <= 125):
        bad.append("bin_step")
    if c.fees_1h < 500 or c.fee_tvl_1h < 1.0:
        bad.append("fees")                     # fee/TVL < 1%/h loses; $500 floor: +65% entries vs $1k, same edge
    if c.fees_24h and c.fees_1h > 0.5 * c.fees_24h:
        bad.append("one_spike")                # the last hour is more than half of the day's fees
    fv = c.fee_momentum if c.fee_momentum is not None else fee_velocity(c.bars)
    if fv is not None and fv < 0.8:
        bad.append("fading")
    if c.buy_vol_1h <= 0 or c.sell_vol_1h <= 0 or not (0.5 <= c.sell_vol_1h / c.buy_vol_1h <= 2.0):
        bad.append("one_way_flow")
    if c.traders_1h < 100:
        bad.append("few_traders")
    ch = c.price_change_1h_pct
    if ch is not None and abs(ch) > 30:
        bad.append("vertical_move")
    if not (c.mint_off and c.freeze_off) or c.mcap < 250_000 or c.holders < 500 or c.top10 > 30 \
            or c.bot_holders_pct > 30 or c.organic < 50:
        bad.append("token")
    return bad


class ProSpot(Strategy):
    name = "pro_spot"
    label = "P1 Pro pools, spot +/-10 bins"
    bins_each_side = 10
    rules = ExitRules(take_profit=5, trail_trigger=3, trail_drop=1.5, stop=-8, out_below_min=15, out_above_min=15,
                      max_hours=2, crash_pct=-10, crash_window_min=15, fee_death_min=20, cooldown_min=20)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if pro_screen(c):
            return None
        half = capital_sol / 2
        legs = [Leg("S", spot_two_sided(c.price, self.bins_each_side, c.step, half, half / c.price))]
        return Plan(legs, idle_sol=0.0, buy_sol=half, info={"fee_tvl_1h": c.fee_tvl_1h, "fees_1h": c.fees_1h,
                                                             "token_age_h": c.token_age_h,
                                                             "bins_each_side": self.bins_each_side})


class ProSpotWide(ProSpot):
    name = "pro_spot_wide"
    label = "P2 Pro pools, spot +/-25 bins"
    bins_each_side = 25
    rules = ExitRules(take_profit=5, trail_trigger=3, trail_drop=1.5, stop=-10, out_below_min=20, out_above_min=20,
                      max_hours=4, crash_pct=-12, crash_window_min=30, fee_death_min=30, cooldown_min=30)


def bid_screen(c: Candidate) -> list[str]:
    """Looser screen for the SOL-only bid: replay (backtest/screen_study.py) fee/TVL >= 2%/h, TVL <= $300k, token
    checks -> +1.35% mean, 61% wins, ~114 entries/day; the two-sided shapes lose money on this looser screen."""
    bad = []
    if c.fee_tvl_1h < 2.0:
        bad.append("fees")
    if not (25_000 <= c.tvl <= 300_000):
        bad.append("tvl")                      # >= $25k: tiny pools are the easiest to rug (SPACEXSI, $14k)
    token_ok = c.mint_off and c.freeze_off and c.mcap >= 250_000 and c.holders >= 500 and c.top10 <= 30
    if not token_ok or c.bot_holders_pct > 30:
        bad.append("token")
    return bad


class ProBid(Strategy):
    name = "pro_bid"
    label = "P3 SOL bids, even 30 bins, fee/TVL >= 2% (trails up)"
    rules = ExitRules(take_profit=5, trail_trigger=3, trail_drop=1.5, stop=-12, out_below_min=30, out_above_min=5,
                      max_hours=4, crash_pct=-15, crash_window_min=15, fee_death_min=30, recenter_above=True,
                      cooldown_min=30)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        if bid_screen(c):
            return None
        # replay: even 30 bins below beat bid-ask 69 bins (median +1.05% vs +0.16% per position)
        legs = [Leg("A", even_bids_below(c.price, 30, c.step, capital_sol))]
        return Plan(legs, idle_sol=0.0, buy_sol=0.0, info={"fee_tvl_1h": c.fee_tvl_1h, "fees_1h": c.fees_1h,
                                                            "token_age_h": c.token_age_h})


# V2 (2026-10-11), from the first 17 pro positions: the pools paid $15 of fees, but swapping the tokens back on exit
# cost ~$15.6 (4-15% quotes) and 9 of 17 positions were closed within 15 min by warnings that showed right after the
# entry. So: enter only calm tokens whose real round trip is cheap and whose pool price matches the market, confirm
# warnings, hold longer, and on soft exits sell the tokens through the pool instead of swapping them.
V2_EXITS: dict[str, Any] = {"unwind": True, "confirm_warnings": True, "cooldown_min": 120}


class V2Wide(ProSpotWide):
    name = "v2_wide"
    label = "V2 Wide spot +/-25, calm entry, sells through the pool"
    careful = True
    rules = ExitRules(take_profit=6, trail_trigger=4, trail_drop=2.5, stop=-10, out_below_min=20, out_above_min=20,
                      max_hours=6, crash_pct=-12, crash_window_min=30, fee_death_min=45, **V2_EXITS)


class V2Calm(ProSpot):
    name = "v2_calm"
    label = "V2 Calm pools, spot +/-40, fee/TVL >= 1.5%"
    careful = True
    bins_each_side = 40
    rules = ExitRules(take_profit=8, trail_trigger=6, trail_drop=3, stop=-12, out_below_min=30, out_above_min=30,
                      max_hours=8, crash_pct=-15, crash_window_min=30, fee_death_min=60, **V2_EXITS)

    def entry(self, c: Candidate, capital_sol: float) -> Plan | None:
        ch = c.price_change_1h_pct
        if ch is None or abs(ch) > 10 or c.fee_tvl_1h < 1.5:
            return None                        # a quiet price: the range keeps earning instead of running out
        return super().entry(c, capital_sol)


class V2BidFlip(ProBid):
    name = "v2_bid_flip"
    label = "V2 SOL bids, fills flipped into asks (no swaps)"
    careful = True
    rules = ExitRules(take_profit=5, trail_trigger=4, trail_drop=2, stop=-12, out_below_min=30, out_above_min=5,
                      max_hours=6, crash_pct=-15, crash_window_min=15, fee_death_min=45, recenter_above=True,
                      flip=True, flip_fill=0.3, flip_bounce=1.03, flip_target=1.04, **V2_EXITS)


PRO: list[Strategy] = [ProSpot(), ProSpotWide(), ProBid()]
V2: list[Strategy] = [V2Wide(), V2Calm(), V2BidFlip()]

STRATEGIES: list[Strategy] = [*V2, *PRO, ToppedBid(), Meridian(), ChopSpot(), ToppedBidV2(), EvilPanda(), Grid(),
                              FeeLeader(), MeridianTrail(), Rabbit()]
BY_NAME = {s.name: s for s in STRATEGIES}
