from __future__ import annotations

import pytest

from density_contract_v1 import DensityContractError, validate_request


def test_fail_closed_wrong_min_origins():
    with pytest.raises(DensityContractError) as ei:
        validate_request(
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=1,
            min_unique_origins=1,
        )
    assert ei.value.code == "MIN_UNIQUE_ORIGINS_MISMATCH"


def test_fail_closed_15m():
    with pytest.raises(DensityContractError) as ei:
        validate_request(
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=1,
            min_unique_origins=2,
        )
    assert ei.value.code == "TIMEFRAME_NOT_SUPPORTED"


def test_fail_closed_wrong_method():
    with pytest.raises(DensityContractError) as ei:
        validate_request(
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=1,
            min_unique_origins=2,
            density_method="clusters_by_dc",
        )
    assert ei.value.code == "DENSITY_METHOD_MISMATCH"


def test_hook_disabled_by_default():
    import os
    from integration_hook_v1 import attach_density_bands_if_enabled, hook_enabled

    os.environ.pop("BT_D3_DENSITY_PROVIDER_HOOK", None)
    assert hook_enabled() is False
    assert attach_density_bands_if_enabled("DOGEUSDT", "2026-09-25T11:00:00Z") is None
