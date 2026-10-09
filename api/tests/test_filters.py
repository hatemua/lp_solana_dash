import pytest

from lp_api.filters import BOOLEAN, MAX_LIMIT, NUMERIC, PRESETS, FilterError, build_query


def test_defaults_sort_by_fee_tvl() -> None:
    q = build_query({})
    assert "ORDER BY fee_tvl_1h DESC NULLS LAST" in q.sql
    assert q.params["limit"] == 100 and q.params["offset"] == 0
    assert "WHERE" not in q.sql.split("FROM v")[-1]


def test_min_max_are_bound_parameters() -> None:
    q = build_query({"tvl_min": "10000", "volume_1h_max": 5e5, "fee_tvl_24h_min": "0.05"})
    tail = q.sql.split("FROM v")[-1]
    assert "tvl >= :tvl_min" in tail and "volume_1h <= :volume_1h_max" in tail
    assert q.params["tvl_min"] == 10000.0 and q.params["fee_tvl_24h_min"] == 0.05


def test_unknown_filter_rejected() -> None:
    with pytest.raises(FilterError):
        build_query({"tvl; DROP TABLE pools_min": 1})
    with pytest.raises(FilterError):
        build_query({"secret_field": 1})


def test_bad_number_and_sort_rejected() -> None:
    with pytest.raises(FilterError):
        build_query({"tvl_min": "lots"})
    with pytest.raises(FilterError):
        build_query({"sort": "address; --"})
    with pytest.raises(FilterError):
        build_query({"order": "sideways"})


def test_booleans_and_search() -> None:
    q = build_query({"sol_pair": "true", "mint_disabled": "0", "q": "BONK"})
    assert q.params["sol_pair"] is True and q.params["mint_disabled"] is False
    assert q.params["q"] == "%BONK%"


def test_limit_is_capped() -> None:
    assert build_query({"limit": 99999}).params["limit"] == MAX_LIMIT
    assert build_query({"limit": -5}).params["limit"] == 1


def test_bin_steps_list() -> None:
    q = build_query({"bin_steps": "80,100,125"})
    assert q.params["bin_steps"] == [80, 100, 125]


@pytest.mark.parametrize("name", list(PRESETS))
def test_presets_only_use_known_fields(name: str) -> None:
    preset = PRESETS[name]
    for key in preset["filters"]:
        field = key[:-4] if key.endswith(("_min", "_max")) else key
        assert field in NUMERIC or field in BOOLEAN, key
    q = build_query({"preset": name})
    assert f"ORDER BY {preset['sort']}" in q.sql


def test_explicit_params_override_preset() -> None:
    q = build_query({"preset": "rabbit500", "token_volume_5m_min": 1_000_000})
    assert q.params["token_volume_5m_min"] == 1_000_000


def test_unknown_preset() -> None:
    with pytest.raises(FilterError):
        build_query({"preset": "nope"})
