"""Paper meme-bot strategies: entry filters over the universe + exit rules. Built-in templates come from the research
(docs/research/meme-strategies.md); users can copy and change every number, within the bounds below.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .categories import NARRATIVES, TIERS

NARRATIVE_IDS = (*NARRATIVES, "culture")
TIER_IDS = tuple(t for t, _ in TIERS)
RankBy = Literal["momentum", "safety", "chg_1h", "volume_1h", "buy_ratio_1h", "liquidity", "holder_chg_1h"]
Pct = Field(default=None, ge=-100, le=10_000)


class Entry(BaseModel):
    """Which tokens a bot may buy. Unset numbers are not checked."""
    narratives: list[str] = Field(default_factory=list, description="only these narratives (empty = all)")
    tiers: list[str] = Field(default_factory=list, description="only these size tiers (empty = all)")
    hot_narratives: int = Field(0, ge=0, le=9, description="only the top N narratives by heat (0 = off)")
    safe_only: bool = True                                          # no red flag
    socials_only: bool = False
    exclude_boosted: bool = False
    mcap_min: float | None = Field(None, ge=0)
    mcap_max: float | None = Field(None, ge=0)
    liquidity_min: float = Field(50_000, ge=10_000)               # never below $10k: exits must be possible
    age_h_min: float | None = Field(None, ge=0)
    age_h_max: float | None = Field(None, ge=0)
    holders_min: float | None = Field(None, ge=0)
    top10_max: float | None = Field(None, ge=0, le=100)
    organic_min: float | None = Field(None, ge=0, le=100)
    volume_1h_min: float | None = Field(None, ge=0)
    buy_ratio_1h_min: float | None = Field(None, ge=0, le=100)
    ds_txn_ratio_1h_min: float | None = Field(None, ge=0, le=100)
    holder_chg_1h_min: float | None = Field(None, ge=-100, le=100)
    chg_5m_min: float | None = Pct
    chg_5m_max: float | None = Pct
    chg_1h_min: float | None = Pct
    chg_1h_max: float | None = Pct
    chg_6h_min: float | None = Pct
    chg_6h_max: float | None = Pct
    chg_24h_min: float | None = Pct
    chg_24h_max: float | None = Pct
    momentum_min: float | None = Field(None, ge=0, le=100)
    safety_min: float | None = Field(None, ge=0, le=100)
    rank_by: RankBy = "momentum"

    @model_validator(mode="after")
    def _known(self) -> "Entry":
        bad = [n for n in self.narratives if n not in NARRATIVE_IDS] + [t for t in self.tiers if t not in TIER_IDS]
        if bad:
            raise ValueError(f"unknown narrative or tier: {bad}")
        return self


class Exit(BaseModel):
    """When a bot sells. Percentages are price moves from the entry price."""
    stop_loss_pct: float = Field(10, ge=1, le=90)
    take_profit_pct: float | None = Field(None, ge=1, le=10_000)
    take_profit_part: float = Field(1.0, ge=0.1, le=1.0, description="share sold at take profit; the rest trails")
    trail_trigger_pct: float | None = Field(None, ge=0.5, le=10_000)
    trail_drop_pct: float | None = Field(None, ge=0.5, le=90)
    max_hours: float = Field(12, ge=0.25, le=168)
    fast_drop_pct: float | None = Field(10, ge=2, le=90, description="sell if the price falls this much in 2 min")
    rug_liquidity_drop_pct: float = Field(30, ge=5, le=90, description="sell if liquidity falls this much")
    sell_pressure_exit: bool = True
    narrative_cool_rank: int | None = Field(None, ge=1, le=9, description="sell when the narrative drops below")

    @model_validator(mode="after")
    def _trail(self) -> "Exit":
        if (self.trail_trigger_pct is None) != (self.trail_drop_pct is None):
            raise ValueError("set both trail_trigger_pct and trail_drop_pct, or neither")
        return self


class Params(BaseModel):
    entry: Entry = Field(default_factory=lambda: Entry.model_validate({}))
    exit: Exit = Field(default_factory=lambda: Exit.model_validate({}))
    cooldown_h: float = Field(6, ge=0, le=168, description="no re-buy of a token for this long after selling it")
    max_cost_pct: float = Field(3, ge=0.5, le=15, description="skip tokens whose buy+sell round trip costs more")


def _p(entry: dict[str, Any], exit: dict[str, Any], **kw: Any) -> Params:
    return Params(entry=Entry(**entry), exit=Exit(**exit), **kw)


TEMPLATES: dict[str, dict[str, Any]] = {
    "blue_trend": {
        "name": "Blue-chip trend", "risk": "low",
        "description": "Large, liquid memes ($10M+, a week old) in an uptrend with buyers in control. Large liquid "
                       "coins tend to keep their short-term direction (momentum). Tight trailing stop.",
        "params": _p({"mcap_min": 10e6, "liquidity_min": 500_000, "age_h_min": 168, "chg_1h_min": 2, "chg_1h_max": 15,
                      "chg_6h_min": 0, "buy_ratio_1h_min": 1.2, "chg_5m_min": -2, "rank_by": "momentum"},
                     {"stop_loss_pct": 6, "trail_trigger_pct": 4, "trail_drop_pct": 5, "max_hours": 12,
                      "fast_drop_pct": 6}),
    },
    "dip_reversal": {
        "name": "Dip reversal", "risk": "medium",
        "description": "Mid-size memes down 20%+ in 24 h that turn up this hour with buyers ahead. Small coins tend "
                       "to bounce back after sharp drops (reversal). Fixed take profit.",
        "params": _p({"mcap_min": 1e6, "mcap_max": 50e6, "age_h_min": 72, "liquidity_min": 150_000,
                      "chg_24h_max": -20, "chg_1h_min": 1, "chg_5m_min": 0, "buy_ratio_1h_min": 1.2,
                      "holders_min": 1_000, "rank_by": "buy_ratio_1h"},
                     {"stop_loss_pct": 10, "take_profit_pct": 15, "trail_trigger_pct": 8, "trail_drop_pct": 6,
                      "max_hours": 24}),
    },
    "narrative_rotation": {
        "name": "Narrative rotation", "risk": "medium",
        "description": "Buys the strongest safe token of the 2 hottest narratives (dog, cat, AI, political...) and "
                       "sells when its narrative cools off. Rides where the money rotates.",
        "params": _p({"hot_narratives": 2, "age_h_min": 24, "liquidity_min": 100_000, "momentum_min": 50,
                      "chg_1h_min": 0, "chg_1h_max": 40, "rank_by": "momentum"},
                     {"stop_loss_pct": 10, "trail_trigger_pct": 6, "trail_drop_pct": 8, "max_hours": 12,
                      "narrative_cool_rank": 4}),
    },
    "safe_runner": {
        "name": "Safe runner", "risk": "medium",
        "description": "The screener's 'safe runners': older than a day, real holders, no red flags, rising with "
                       "buyers. Takes half at +20% and trails the rest.",
        "params": _p({"age_h_min": 24, "mcap_min": 1e6, "liquidity_min": 100_000, "holders_min": 2_000,
                      "top10_max": 30, "organic_min": 50, "buy_ratio_1h_min": 1.1, "chg_1h_min": 0, "chg_1h_max": 40,
                      "socials_only": True},
                     {"stop_loss_pct": 8, "take_profit_pct": 20, "take_profit_part": 0.5, "trail_trigger_pct": 8,
                      "trail_drop_pct": 7, "max_hours": 8}),
    },
    "fresh_momentum": {
        "name": "Fresh momentum", "risk": "high",
        "description": "Tokens 2-24 h old with real liquidity, many holders and organic buyers. Over 99% of new "
                       "launches die, so small size, wide stop, half sold at +30%.",
        "params": _p({"age_h_min": 2, "age_h_max": 24, "liquidity_min": 50_000, "holders_min": 500, "top10_max": 30,
                      "organic_min": 50, "buy_ratio_1h_min": 1.3, "chg_1h_min": 5, "chg_1h_max": 60,
                      "socials_only": True, "rank_by": "momentum"},
                     {"stop_loss_pct": 15, "take_profit_pct": 30, "take_profit_part": 0.5, "trail_trigger_pct": 30,
                      "trail_drop_pct": 15, "max_hours": 6, "fast_drop_pct": 12}, max_cost_pct=4),
    },
}


def template_list() -> list[dict[str, Any]]:
    return [{"id": k, "name": t["name"], "risk": t["risk"], "description": t["description"],
             "params": t["params"].model_dump()} for k, t in TEMPLATES.items()]


_CHECKS = (("mcap_min", "mcap", 1), ("mcap_max", "mcap", -1), ("liquidity_min", "liquidity", 1),
           ("age_h_min", "age_h", 1), ("age_h_max", "age_h", -1), ("holders_min", "holders", 1),
           ("top10_max", "top10_pct", -1), ("organic_min", "organic_score", 1), ("volume_1h_min", "volume_1h", 1),
           ("buy_ratio_1h_min", "buy_ratio_1h", 1), ("ds_txn_ratio_1h_min", "ds_txn_ratio_1h", 1),
           ("holder_chg_1h_min", "holder_chg_1h", 1), ("chg_5m_min", "chg_5m", 1), ("chg_5m_max", "chg_5m", -1),
           ("chg_1h_min", "chg_1h", 1), ("chg_1h_max", "chg_1h", -1), ("chg_6h_min", "chg_6h", 1),
           ("chg_6h_max", "chg_6h", -1), ("chg_24h_min", "chg_24h", 1), ("chg_24h_max", "chg_24h", -1),
           ("momentum_min", "momentum", 1), ("safety_min", "safety", 1))


def entry_reasons(r: dict[str, Any], e: Entry, hot: dict[str, int]) -> list[str]:
    """Why a token fails the entry filters (empty = it passes). `hot` = narrative -> heat rank."""
    bad = []
    if e.narratives and r.get("narrative") not in e.narratives:
        bad.append("narrative")
    if e.tiers and r.get("tier") not in e.tiers:
        bad.append("tier")
    if e.hot_narratives and hot.get(r.get("narrative") or "", 99) > e.hot_narratives:
        bad.append("narrative_not_hot")
    if e.safe_only and any(f["level"] == "red" for f in r.get("flags") or []):
        bad.append("red_flag")
    if e.socials_only and not (r.get("has_website") or r.get("has_twitter") or r.get("has_telegram")):
        bad.append("no_socials")
    if e.exclude_boosted and r.get("boost"):
        bad.append("boosted")
    for key, field, sign in _CHECKS:
        limit = getattr(e, key)
        if limit is None:
            continue
        v = r.get(field)
        if v is None or (sign > 0 and v < limit) or (sign < 0 and v > limit):
            bad.append(key)
    # always-on guards: no falling knife, no sellers' market, a price both sources agree on
    if (r.get("chg_5m") or 0) <= -8:
        bad.append("dumping_5m")
    if (r.get("buy_5m") or 0) + (r.get("sell_5m") or 0) >= 2_000 and (r.get("sell_5m") or 0) > 2 * max(
            r.get("buy_5m") or 0, 1):
        bad.append("sell_pressure_5m")
    if not r.get("decimals") and r.get("decimals") != 0:
        bad.append("no_decimals")
    return bad


def candidates(tokens: list[dict[str, Any]], p: Params, heat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hot = {h["narrative"]: h["rank"] for h in heat}
    ok = [r for r in tokens if not entry_reasons(r, p.entry, hot)]
    key = p.entry.rank_by
    ok.sort(key=lambda r: -(r.get(key) or 0))
    return ok
