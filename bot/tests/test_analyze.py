from lp_bot.analyze import BY_KEY, FIT, choose


def shape(key: str, fits: bool, n: int, mean: float, lower: float, win: float) -> dict:
    return {"key": key, "label": key, "fits": fits,
            "stats": {"n": n, "mean_pct": mean, "lower_pct": lower, "win_rate": win}}


def test_choose_takes_the_best_worst_case_and_skips_weak_edges() -> None:
    pick, _ = choose([shape("spot_10", True, 10, 3.0, 0.2, 0.7),      # higher average, shaky
                      shape("spot_25", True, 10, 2.0, 1.2, 0.8),      # lower average, solid
                      shape("bid_flip", False, 10, 5.0, 4.0, 0.9)])   # doesn't fit the regime
    assert pick is not None and pick["shape"] == "spot_25" and pick["confidence"] == "high"
    none, why = choose([shape("spot_10", True, 10, 0.5, 0.1, 0.6), shape("spot_25", True, 3, 4.0, 3.0, 1.0),
                        shape("spot_40", True, 10, 2.0, -0.4, 0.6), shape("bids_30", True, 10, 2.0, 0.5, 0.4)])
    assert none is None and len(why) == 4


def test_every_regime_maps_to_known_shapes_and_falling_to_none() -> None:
    assert FIT["falling"] == []
    assert all(k in BY_KEY for keys in FIT.values() for k in keys)
