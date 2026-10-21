"""Verified, frozen inputs for historical regression tests."""

from scripts.data_baseline import DEFAULT_TARGET, verify_baseline
from market_catalog import MarketDataStore


def frozen_store():
    verify_baseline()
    return MarketDataStore(base_dir=DEFAULT_TARGET)
