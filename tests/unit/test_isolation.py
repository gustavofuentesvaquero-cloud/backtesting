"""Verify that accidental external dependencies fail before accessing them."""

from pathlib import Path
import socket
import tempfile
import unittest

from market_catalog import MarketDataStore
from scripts.check import ROOT, isolated_checks


class OfflineIsolationTests(unittest.TestCase):
    def test_dns_and_connections_fail_before_network_access(self):
        with isolated_checks("unit"):
            for operation in (lambda: socket.getaddrinfo("example.invalid", 443),
                              lambda: socket.gethostbyname("example.invalid"),
                              lambda: socket.create_connection(("127.0.0.1", 9))):
                with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "Network access"):
                    operation()
            with socket.socket() as connection:
                with self.assertRaisesRegex(RuntimeError, "Network access"):
                    connection.connect_ex(("127.0.0.1", 9))
            with socket.socket(type=socket.SOCK_DGRAM) as connection:
                with self.assertRaisesRegex(RuntimeError, "Network access"):
                    connection.sendto(b"test", ("127.0.0.1", 9))

    def test_unit_stores_reject_implicit_and_historical_directories(self):
        with isolated_checks("unit"):
            for path in (None, ROOT / "data" / "historical", ROOT / "tests" / "fixtures" / "historical"):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    MarketDataStore(base_dir=path)

    def test_unit_store_allows_synthetic_data_and_blocks_downloads(self):
        with tempfile.TemporaryDirectory() as directory, isolated_checks("unit"):
            store = MarketDataStore(base_dir=directory)
            try:
                self.assertEqual(store.base_dir, Path(directory))
                for method in (store.fetch_and_store_ticker, store.fetch_and_store_cme_mgc):
                    with self.assertRaisesRegex(RuntimeError, "Network access"):
                        method("TEST")
            finally:
                store._con.close()

    def test_historical_store_rejects_working_cache(self):
        with isolated_checks("historical"), self.assertRaisesRegex(RuntimeError, "verified frozen"):
            MarketDataStore(base_dir=ROOT / "data" / "historical")
