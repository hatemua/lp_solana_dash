"""Memecoin categories: narrative (dog, cat, frog, AI, political, ...) from the name and symbol, size tier from the
market cap, stage from the age, and narrative heat (which narratives the market is rotating into right now)."""

import re
import statistics
from typing import Any

# narrative -> keywords matched as whole words or word prefixes in "symbol name" (lower case)
NARRATIVES: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "agi", "gpt", "llm", "agent", "agents", "neural", "eliza", "claude", "grok", "gork", "sentient",
           "robot", "bot", "cyber", "virtual", "machine", "brain", "openai", "deepseek", "aixbt", "zerebro"),
    "political": ("trump", "melania", "maga", "biden", "kamala", "vance", "president", "potus", "america",
                  "usa", "patriot", "republican", "democrat", "election", "barron", "rfk"),
    "celebrity": ("elon", "musk", "kanye", "drake", "tate", "spacex", "tesla", "mrbeast", "jenner", "snoop",
                  "cz", "saylor", "vitalik", "bezos", "zuck"),
    "dog": ("dog", "doge", "dogs", "shib", "shiba", "inu", "wif", "bonk", "puppy", "pup", "pups", "corgi", "husky",
            "chihuahua", "floki", "dogwif", "pitbull", "retriever", "jotchua", "woof", "mutt", "hound", "doggo",
            "kabosu", "myro", "samo"),
    "cat": ("cat", "cats", "kitty", "kitten", "meow", "mew", "popcat", "nyan", "purr", "mog", "michi", "catwif",
            "neko", "billy", "catwif"),
    "frog": ("frog", "pepe", "toad", "ribbit", "kek", "brett", "frogs", "pepecoin", "apu"),
    "animal": ("monkey", "ape", "apes", "penguin", "pengu", "pnut", "squirrel", "bear", "bull", "fish", "bird",
               "hippo", "moodeng", "goat", "horse", "rabbit", "bunny", "duck", "hamster", "panda", "sloth", "otter",
               "wolf", "lion", "tiger", "chicken", "rat", "mouse", "owl", "fox", "cow", "pig", "shark", "whale",
               "seal", "giga", "chad", "gorilla", "chimp", "snail", "turtle", "crab", "lobster", "bee"),
    "gaming": ("game", "gaming", "gta", "play", "pixel", "quest", "arcade", "minecraft", "roblox", "pokemon"),
}
NARRATIVE_LABELS = {"ai": "AI", "political": "Political", "celebrity": "Celebrity", "dog": "Dog", "cat": "Cat",
                    "frog": "Frog", "animal": "Other animals", "gaming": "Gaming", "culture": "Culture / other"}
ORDER = ("ai", "political", "celebrity", "dog", "cat", "frog", "animal", "gaming")   # first match wins


def _rx(kws: tuple[str, ...]) -> re.Pattern[str]:
    """Words of 4+ letters also match as a prefix ('doge' in 'dogecoin'); short ones only as whole words."""
    parts = [re.escape(k) + ("" if len(k) >= 4 else r"(?![a-z])") for k in kws]
    return re.compile(r"(?<![a-z])(?:" + "|".join(parts) + ")")


_RX = {n: _rx(kws) for n, kws in NARRATIVES.items()}

TIERS = (("blue", 100e6), ("large", 10e6), ("mid", 1e6), ("small", 100e3), ("micro", 0.0))
TIER_LABELS = {"blue": "Blue chip >= $100M", "large": "Large $10-100M", "mid": "Mid $1-10M",
               "small": "Small $100k-1M", "micro": "Micro < $100k"}


def _words(symbol: str | None, name: str | None) -> str:
    """Lower-case text with camelCase and digits split, e.g. 'catWifHat' -> 'cat wif hat'."""
    raw = f"{symbol or ''} {name or ''}"
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    return re.sub(r"[^a-z]+", " ", raw.lower())


def narrative(symbol: str | None, name: str | None) -> str:
    sym = (symbol or "").lower().lstrip("$")
    if len(sym) > 3 and sym.endswith("ai"):                             # GRIFFAI, VIRTUALAI
        return "ai"
    for text in (_words(symbol, None), _words(symbol, name)):           # the symbol decides first: MEW is a cat
        for n in ORDER:
            if _RX[n].search(text):
                return n
    return "culture"


def tier(mcap: float | None) -> str | None:
    if mcap is None:
        return None
    return next(t for t, floor in TIERS if mcap >= floor)


def stage(age_h: float | None) -> str | None:
    if age_h is None:
        return None
    return "new" if age_h < 24 else "young" if age_h < 24 * 7 else "established"


def classify(r: dict[str, Any]) -> dict[str, Any]:
    """Adds narrative, tier and stage to a normalized screener row (in place) and returns it."""
    r["narrative"] = narrative(r.get("symbol"), r.get("name"))
    r["tier"] = tier(r.get("mcap"))
    r["stage"] = stage(r.get("age_h"))
    return r


def heat(rows: list[dict[str, Any]], min_tokens: int = 3) -> list[dict[str, Any]]:
    """Narratives ranked by where the money is going now: median 1 h and 6 h move of their liquid tokens (> 1 day,
    liquidity >= $50k), weighted with how much of their liquidity traded in the last hour."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if (r.get("age_h") or 0) >= 24 and (r.get("liquidity") or 0) >= 50_000 and r.get("chg_1h") is not None:
            groups.setdefault(r["narrative"], []).append(r)
    out = []
    for n, g in groups.items():
        if len(g) < min_tokens:
            continue
        m1 = statistics.median(r["chg_1h"] for r in g)
        m6 = statistics.median((r.get("chg_6h") or 0) for r in g)
        vol = sum(r.get("volume_1h") or 0 for r in g)
        liq = sum(r.get("liquidity") or 0 for r in g)
        turnover = vol / liq if liq else 0.0
        score = 0.6 * m1 + 0.4 * m6 / 3 + 10 * min(turnover, 1.0)
        out.append({"narrative": n, "label": NARRATIVE_LABELS.get(n, n), "tokens": len(g), "median_chg_1h": m1,
                    "median_chg_6h": m6, "volume_1h": vol, "turnover_1h": turnover, "score": round(score, 2)})
    out.sort(key=lambda x: -x["score"])
    for i, x in enumerate(out):
        x["rank"] = i + 1
    return out
