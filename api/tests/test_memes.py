"""Memecoin screener on a real Jupiter toptrending/1h response (saved 2026-10-11)."""

import json
from pathlib import Path

import pytest

from lp_api import memes as M

REAL = json.loads((Path(__file__).parent / "fixtures" / "jup_toptrending_1h.json").read_text(encoding="utf-8"))


def by_symbol(rows: list[dict], sym: str) -> dict:
    return next(r for r in rows if r["symbol"] == sym)


def test_merge_keeps_memes_only_and_tags_lists() -> None:
    rows = M.merge_lists({"toptrending/1h": REAL, "toptraded/24h": REAL[:5]})
    syms = {r["symbol"] for r in rows}
    assert {"BOME", "Fartcoin", "TRUMP", "$WIF", "USELESS"} <= syms
    assert not syms & {"JUP", "RAY", "SPCX", "NVDAx", "ZEC", "HYPE", "PUMP", "GRASS"}   # defi, stocks, majors
    assert len({r["mint"] for r in rows}) == len(rows)
    assert by_symbol(rows, "BOME")["lists"] == ["toptraded/24h", "toptrending/1h"]


def test_normalize_fields_and_scores_in_range() -> None:
    rows = M.merge_lists({"toptrending/1h": REAL})
    for r in rows:
        assert 0 <= r["safety"] <= 100 and 0 <= r["momentum"] <= 100
        assert r["volume_1h"] == pytest.approx(r["buy_1h"] + r["sell_1h"])
        if r["age_h"] is not None:
            assert r["age_h"] >= 0
    wif = by_symbol(rows, "$WIF")
    assert wif["mint_disabled"] is True and wif["age_h"] > 24 * 300 and wif["mcap"] > 1e8


def test_flags_on_risky_token() -> None:
    t = dict(REAL[0], audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True,
                             "topHoldersPercentage": 62}, liquidity=8_000,
             stats1h={"priceChange": -30, "buyVolume": 3_000, "sellVolume": 20_000})
    r = M.normalize(t, ["recent"])
    codes = {f["code"] for f in r["flags"] if f["level"] == "red"}
    assert {"mint_authority", "concentrated", "thin_liquidity", "sell_pressure", "dumping"} <= codes
    assert r["safety"] == 0


def test_screen_filters_sort_presets() -> None:
    rows = M.merge_lists({"toptrending/1h": REAL})
    out = M.screen(rows, {"mcap_min": "100000000", "sort": "mcap"})
    assert out["total"] >= 1 and all(r["mcap"] >= 1e8 for r in out["data"])
    mc = [r["mcap"] for r in out["data"]]
    assert mc == sorted(mc, reverse=True)
    safe = M.screen(rows, {"preset": "established"})
    assert all(not any(f["level"] == "red" for f in r["flags"]) for r in safe["data"])
    assert all(r["age_h"] >= 720 for r in safe["data"])
    assert M.screen(rows, {"q": "fart"})["data"][0]["symbol"] == "Fartcoin"
    assert M.screen(rows, {"meteora_only": "true"})["total"] == 0       # no pools joined in this test


def test_screen_rejects_bad_input() -> None:
    with pytest.raises(M.ScreenError):
        M.screen([], {"preset": "nope"})
    with pytest.raises(M.ScreenError):
        M.screen([], {"sort": "drop table"})
    with pytest.raises(M.ScreenError):
        M.screen([], {"mcap_min": "lots"})
