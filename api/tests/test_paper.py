"""Paper trading: categories, strategy validation and entry filters on a real Jupiter response (2026-10-11)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lp_api import memes as M
from lp_api.paper import categories as C
from lp_api.paper.strategies import TEMPLATES, Entry, Exit, Params, candidates, entry_reasons, template_list

REAL = json.loads((Path(__file__).parent / "fixtures" / "jup_toptrending_1h.json").read_text(encoding="utf-8"))
ROWS = [C.classify(r) for r in M.merge_lists({"toptrending/1h": REAL})]


def test_narratives_of_known_memes() -> None:
    by = {r["symbol"]: r for r in ROWS}
    assert by["$WIF"]["narrative"] == "dog" and by["BOME"]["narrative"] == "culture"
    assert by["TRUMP"]["narrative"] == "political" and by["elizaOS"]["narrative"] == "ai"
    assert C.narrative("MEW", "cat in a dogs world") == "cat"            # the symbol decides first
    assert C.narrative("GRIFFAI", "griffain") == "ai" and C.narrative("CATE", "Catalyst") == "culture"
    assert {r["tier"] for r in ROWS} <= set(C.TIER_LABELS) and by["TRUMP"]["tier"] == "blue"


def test_heat_ranks_narratives() -> None:
    h = C.heat(ROWS, min_tokens=2)
    assert [x["rank"] for x in h] == list(range(1, len(h) + 1))
    assert all(x["tokens"] >= 2 for x in h) and h == sorted(h, key=lambda x: -x["score"])


def test_templates_valid_and_roundtrip() -> None:
    for t in template_list():
        p = Params.model_validate(json.loads(json.dumps(t["params"])))
        assert p == TEMPLATES[t["id"]]["params"]
        assert p.exit.stop_loss_pct > 0 and p.entry.liquidity_min >= 10_000


def test_params_bounds() -> None:
    with pytest.raises(ValidationError):
        Exit(trail_trigger_pct=5)                                         # trail needs both numbers
    with pytest.raises(ValidationError):
        Entry(narratives=["unicorn"])
    with pytest.raises(ValidationError):
        Entry(liquidity_min=1_000)                                        # too thin to exit
    with pytest.raises(ValidationError):
        Exit(stop_loss_pct=0)


def test_entry_filters_on_real_rows() -> None:
    hot = {h["narrative"]: h["rank"] for h in C.heat(ROWS, min_tokens=2)}
    for key, t in TEMPLATES.items():
        for r in candidates(ROWS, t["params"], C.heat(ROWS, min_tokens=2)):
            assert not entry_reasons(r, t["params"].entry, hot), key
            assert not any(f["level"] == "red" for f in r["flags"])
    wif = next(r for r in ROWS if r["symbol"] == "$WIF")
    assert "narrative" in entry_reasons(wif, Entry(narratives=["cat"]), hot)
    assert "mcap_max" in entry_reasons(wif, Entry(mcap_max=1e6), hot)
