import json
from pathlib import Path
from typing import Any

import pytest

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def pools_page() -> dict[str, Any]:
    """Real response of GET /pools (trimmed to 5 pools: SOL-USDC, 3 SOL memecoin pools, 1 small pool)."""
    return load("meteora_pools_page.json")


@pytest.fixture
def ohlcv() -> dict[str, Any]:
    return load("meteora_ohlcv_5m.json")


@pytest.fixture
def volume() -> dict[str, Any]:
    return load("meteora_volume_5m.json")


@pytest.fixture
def jup_assets() -> list[dict[str, Any]]:
    return load("jupiter_assets.json")


@pytest.fixture
def jup_chart() -> dict[str, Any]:
    return load("jupiter_chart_1m.json")
