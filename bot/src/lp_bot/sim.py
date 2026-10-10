"""Virtual DLMM positions: bins, legs, valuation and fee share. Pure functions, no I/O.

Prices are SOL per token (the pool's quote is SOL). A "bid" bin holds SOL while the price is above it and turns into
tokens (bought at the bin price) once the price trades down to it; an "ask" bin holds tokens until the price trades
up through it. DLMM composition depends only on the current price, so valuation is path-independent; fees are not.
"""

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Bin:
    price: float
    sol: float = 0.0      # SOL deposited (bid bins)
    tok: float = 0.0      # tokens deposited (ask bins)
    side: str = "bid"

    def holds_token(self, p: float) -> bool:
        return p <= self.price if self.side == "bid" else p < self.price

    def value(self, p: float) -> float:           # SOL
        if self.side == "bid":
            return self.sol if p > self.price else self.sol / self.price * p
        return self.tok * p if p < self.price else self.tok * self.price

    def tokens(self, p: float) -> float:
        if self.side == "bid":
            return self.sol / self.price if p <= self.price else 0.0
        return self.tok if p < self.price else 0.0

    def sol_held(self, p: float) -> float:
        if self.side == "bid":
            return self.sol if p > self.price else 0.0
        return self.tok * self.price if p >= self.price else 0.0


@dataclass
class Leg:
    name: str
    bins: list[Bin] = field(default_factory=list)

    def value(self, p: float) -> float:
        return sum(b.value(p) for b in self.bins)

    def tokens(self, p: float) -> float:
        return sum(b.tokens(p) for b in self.bins)

    def sol_held(self, p: float) -> float:
        return sum(b.sol_held(p) for b in self.bins)

    def converted(self, p: float) -> float:
        """Share of a bid leg's SOL that has been turned into tokens."""
        tot = sum(b.sol for b in self.bins if b.side == "bid")
        return sum(b.sol for b in self.bins if b.side == "bid" and p <= b.price) / tot if tot else 0.0

    def lowest(self) -> float:
        return min(b.price for b in self.bins)

    def highest(self) -> float:
        return max(b.price for b in self.bins)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "bins": [asdict(b) for b in self.bins]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Leg":
        return Leg(d["name"], [Bin(**b) for b in d["bins"]])


def bid_ask(p0: float, lo: float, hi: float, step: float, amount: float, side: str) -> list[Bin]:
    """Bid-ask ladder, weight growing away from the price.

    side="bid": SOL `amount` in bins from p0*hi down to p0*lo (fractions < 1).
    side="ask": token `amount` in bins from p0*lo up to p0*hi (fractions > 1).
    """
    r = 1 + step
    if side == "bid":
        k0, k1 = math.ceil(math.log(1 / hi) / math.log(r)), math.floor(math.log(1 / lo) / math.log(r))
        ks = list(range(max(1, k0), k1 + 1))
        w = list(range(1, len(ks) + 1))
        tot = sum(w)
        return [Bin(price=p0 / r ** k, sol=amount * wi / tot, side="bid") for k, wi in zip(ks, w, strict=True)]
    ks = list(range(max(1, math.ceil(math.log(lo) / math.log(r))), math.floor(math.log(hi) / math.log(r)) + 1))
    w = list(range(1, len(ks) + 1))
    tot = sum(w)
    return [Bin(price=p0 * r ** k, tok=amount * wi / tot, side="ask") for k, wi in zip(ks, w, strict=True)]


def bid_bins_below(p0: float, n: int, step: float, amount: float) -> list[Bin]:
    """Bid-ask with SOL in the n bins right below the price (Meridian style)."""
    r = 1 + step
    w = list(range(1, n + 1))
    tot = sum(w)
    return [Bin(price=p0 / r ** k, sol=amount * wi / tot, side="bid") for k, wi in zip(range(1, n + 1), w, strict=True)]


def spot_two_sided(p0: float, n: int, step: float, sol_amount: float, tok_amount: float) -> list[Bin]:
    """Spot (uniform) around the price: SOL in n bins below, tokens in n bins above."""
    r = 1 + step
    bins = [Bin(price=p0 / r ** k, sol=sol_amount / n, side="bid") for k in range(1, n + 1)]
    bins += [Bin(price=p0 * r ** k, tok=tok_amount / n, side="ask") for k in range(1, n + 1)]
    return bins


def bins_between(p_a: float, p_b: float, step: float) -> int:
    """Number of pool bins the price touched moving from p_a to p_b (at least the active one)."""
    if p_a <= 0 or p_b <= 0:
        return 1
    return abs(round(math.log(p_b / p_a) / math.log(1 + step))) + 1


def fee_share(legs: list[Leg], p_prev: float, p_now: float, step: float, fees_usd: float, sol_usd: float,
              others_usd: Any) -> float:
    """Our LP fees (USD) for a move p_prev -> p_now during which the pool earned `fees_usd`.

    Fees are spread evenly over the bins touched; in each of our bins inside that span we get
    ours / (ours + others). `others_usd(price)` returns the other LPs' liquidity (USD) in the bin at that price.
    """
    if fees_usd <= 0:
        return 0.0
    lo, hi = min(p_prev, p_now) / (1 + step / 2), max(p_prev, p_now) * (1 + step / 2)
    per_bin = fees_usd / bins_between(p_prev, p_now, step)
    earned = 0.0
    for leg in legs:
        for b in leg.bins:
            if lo <= b.price <= hi:
                ours = b.value(p_now) * sol_usd
                if ours > 0:
                    earned += per_bin * ours / (ours + max(0.0, others_usd(b.price)))
    return earned
