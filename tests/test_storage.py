"""Unit tests for DuckDB storage layer and Interactive Brokers adapter."""

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from pathlib import Path
import tempfile

import pandas as pd

from market_catalog import (
    IBKRConfig,
    IBKRFeedAdapter,
    MarketDataStore,
)


class StorageAndAdapterTests(unittest.TestCase):
    def test_save_and_load_bars_with_duckdb(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarketDataStore(base_dir=temp_dir)
            df = pd.DataFrame([
                {"date": "2026-10-01", "open": 2650.0, "high": 2665.0, "low": 2640.0, "close": 2660.0, "volume": 100},
                {"date": "2026-10-02", "open": 2660.0, "high": 2675.0, "low": 2655.0, "close": 2670.0, "volume": 120},
                {"date": "2026-10-03", "open": 2670.0, "high": 2685.0, "low": 2660.0, "close": 2680.0, "volume": 150},
            ])
            path = store.save_bars("MGC", df, timeframe="1d")
            self.assertTrue(path.exists())

            loaded = store.load_bars("MGC", timeframe="1d")
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded.iloc[0]["close"], 2660.0)

            levels = store.get_market_levels("MGC", timeframe="1d", lookback_bars=3)
            self.assertEqual(levels["swing_low"], Decimal("2640.00"))
            self.assertEqual(levels["swing_high"], Decimal("2685.00"))
            self.assertEqual(levels["latest_close"], Decimal("2680.00"))

    def test_ibkr_adapter_receives_live_quotes(self):
        adapter = IBKRFeedAdapter(IBKRConfig(host="127.0.0.1", port=7497))
        # Adapter starts in disconnected state until connect()
        health = adapter.health()
        self.assertFalse(health.connected)

        # Push a real-time quote directly into the queue
        adapter.push_realtime_quote(
            symbol="MGC",
            bid_price="2650.10",
            bid_quantity="20",
            ask_price="2650.20",
            ask_quantity="25",
        )

        frame = adapter.receive(timeout=1.0)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.provider_id, "IBKR")
        self.assertEqual(frame.channel, "L1")

        adapter.close()
        health_closed = adapter.health()
        self.assertFalse(health_closed.connected)
        self.assertEqual(health_closed.status, "disconnected")
