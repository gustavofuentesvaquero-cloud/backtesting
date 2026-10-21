import io
import json
import logging
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from market_catalog import (
    Alert,
    AlertPolicy,
    Catalog,
    DeploymentProfileError,
    EventProcessor,
    EventProcessorConfig,
    JsonFixtureNormalizer,
    JsonLinesFixtureAdapter,
    JsonLogFormatter,
    MarketDataPipeline,
    ReconnectBackoff,
    ReconnectPolicy,
    SessionRecorder,
    build_observability_snapshot,
    load_deployment_profile,
    log_alerts,
    validate_deployment_profile,
)
from market_catalog.cli import main as cli_main
from market_catalog.events import AdapterHealth, RawFrame, StreamHealth
from market_catalog.quality import DataQualityMonitor, QualityPolicy


def sample_catalog() -> Catalog:
    return Catalog.from_mapping({
        "schema_version": "1.0.0",
        "catalog_version": "1.0.0",
        "instruments": [
            {
                "id": "SYNTH",
                "asset_class": "equity",
                "venue": "SYNTHETIC",
                "provider_symbols": [
                    {"provider_id": "FIXTURE", "symbol": "SYNTH"},
                    {"provider_id": "test_prov", "symbol": "SYNTH"},
                    {"provider_id": "prov_reconnect", "symbol": "SYNTH"},
                ],
                "valid_from": "2030-01-01",
                "valid_to": None,
                "calendar_id": "TEST-CALENDAR",
                "price_tick": "0.01",
                "price_precision": 2,
                "quantity_unit": "share",
                "quantity_precision": 0,
                "minimum_quantity": "1",
                "quantity_increment": "1",
                "quote_currency": "USD",
                "valuation_model": "equity_shares",
                "provenance": {"status": "synthetic", "sources": [], "notes": "Test only."},
                "historical_price_policy": {"price_basis": "raw", "corporate_action_events": []},
            },
            {
                "id": "GOLD",
                "asset_class": "equity",
                "venue": "SYNTHETIC",
                "provider_symbols": [
                    {"provider_id": "prov_reconnect", "symbol": "GOLD"},
                    {"provider_id": "test_prov", "symbol": "GOLD"},
                ],
                "valid_from": "2030-01-01",
                "valid_to": None,
                "calendar_id": "TEST-CALENDAR",
                "price_tick": "0.01",
                "price_precision": 2,
                "quantity_unit": "share",
                "quantity_precision": 0,
                "minimum_quantity": "1",
                "quantity_increment": "1",
                "quote_currency": "USD",
                "valuation_model": "equity_shares",
                "provenance": {"status": "synthetic", "sources": [], "notes": "Test only."},
                "historical_price_policy": {"price_basis": "raw", "corporate_action_events": []},
            },
        ],
        "calendars": [],
        "continuous_futures": [],
    })


def make_raw_frame(ordinal: int, *, provider_id: str = "test_prov", channel: str = "L1") -> RawFrame:
    message = {
        "event_type": "quote",
        "symbol": "SYNTH",
        "sequence": ordinal + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(1_899_999_999_000_000_000 + ordinal),
            "unix_ns": 1_899_999_999_000_000_000 + ordinal,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": "99.75", "quantity": "10"},
            "ask": {"price": "100.00", "quantity": "12"},
        },
    }
    return RawFrame(
        provider_id=provider_id,
        channel=channel,
        receive_time_utc_ns=1_900_000_000_000_000_000 + ordinal,
        receive_time_monotonic_ns=100 + ordinal,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
    )


class NoOpStrategy:
    def on_event(self, event, book):
        pass


class StubAdapter:
    def __init__(self, provider_id: str = "test_prov"):
        self.provider_id = provider_id

    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return True

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            connected=True,
            status="connected",
            detail="ok",
            received_frames=3,
            last_received_monotonic_ns=1000,
        )


class ObservabilityAndDeploymentTests(unittest.TestCase):
    def test_structured_log_formatting_and_correlation(self):
        formatter = JsonLogFormatter()
        logger = logging.getLogger("test.structured.logger")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            extra = {
                "session_id": "session-xyz-123",
                "instrument_id": "XAUUSD",
                "sequence": 1005,
                "order_id": "sim-order-456",
                "stage": "matching",
                "provider_id": "mock_provider",
            }
            logger.info("Order simulation filled partially", extra=extra)

            line = stream.getvalue().strip()
            self.assertTrue(line)
            record = json.loads(line)

            self.assertEqual(record["severity"], "info")
            self.assertEqual(record["session_id"], "session-xyz-123")
            self.assertEqual(record["instrument_id"], "XAUUSD")
            self.assertEqual(record["sequence"], 1005)
            self.assertEqual(record["order_id"], "sim-order-456")
            self.assertEqual(record["stage"], "matching")
            self.assertEqual(record["provider_id"], "mock_provider")
            self.assertIn("timestamp", record)
        finally:
            logger.removeHandler(handler)

    def test_credential_and_secret_redaction(self):
        formatter = JsonLogFormatter()
        logger = logging.getLogger("test.redact.logger")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            extra = {
                "session_id": "session-sec-1",
                "api_key": "secret-api-key-9999",
                "user_token": "super_token_abc",
                "context": {
                    "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.test",
                    "safe_param": "public_feed_01",
                    "nested": {
                        "password": "db_password_1234",
                    },
                },
            }
            logger.warning("Connecting with api_key=plain_key_777 and token=abcde", extra=extra)

            record = json.loads(stream.getvalue().strip())
            self.assertEqual(record["api_key"], "[REDACTED]")
            self.assertEqual(record["user_token"], "[REDACTED]")
            self.assertEqual(record["authorization"], "[REDACTED]")
            self.assertEqual(record["nested"]["password"], "[REDACTED]")
            self.assertEqual(record["safe_param"], "public_feed_01")
            self.assertNotIn("plain_key_777", record["message"])
            self.assertIn("[REDACTED]", record["message"])
        finally:
            logger.removeHandler(handler)

    def test_reconnect_backoff_and_book_invalidation(self):
        policy = ReconnectPolicy(
            initial_delay_seconds=0.5,
            maximum_delay_seconds=4.0,
            multiplier=2.0,
            jitter_fraction=0.0,
        )
        backoff = ReconnectBackoff(policy, seed=42)

        self.assertEqual(backoff.next_delay_seconds(), 0.5)
        self.assertEqual(backoff.next_delay_seconds(), 1.0)
        self.assertEqual(backoff.next_delay_seconds(), 2.0)
        self.assertEqual(backoff.next_delay_seconds(), 4.0)
        self.assertEqual(backoff.next_delay_seconds(), 4.0)
        self.assertEqual(backoff.attempt, 5)

        backoff.reset()
        self.assertEqual(backoff.attempt, 0)
        self.assertEqual(backoff.next_delay_seconds(), 0.5)

        adapter = StubAdapter(provider_id="prov_reconnect")
        normalizer = JsonFixtureNormalizer(sample_catalog())
        quality = DataQualityMonitor(QualityPolicy())
        pipeline = MarketDataPipeline(adapter, normalizer, quality)

        pipeline.ingest(make_raw_frame(1, provider_id="prov_reconnect"))
        health = quality.health_snapshot(now_monotonic_ns=2000)
        self.assertTrue(any(item.decision_usable for item in health))

        backoff.mark_connected(pipeline, "prov_reconnect")
        self.assertEqual(backoff.attempt, 0)

        health_after = quality.health_snapshot(now_monotonic_ns=3000)
        self.assertTrue(all(not item.decision_usable for item in health_after))
        self.assertTrue(all(item.book_state == "awaiting_snapshot" for item in health_after))

    def test_deployment_profile_validation_and_rejection(self):
        valid_profile = {
            "profile_version": "1.0.0",
            "live_execution_enabled": False,
            "execution_adapter": None,
            "outbound_network": {
                "default_policy": "deny",
                "allowed_hosts": ["feed.example.com", "192.168.1.10"],
            },
            "credentials": {
                "source": "external_secret_manager",
                "permissions": ["market_data.read", "catalog.read"],
                "redact_from_logs": True,
                "secrets_in_config": False,
            },
        }
        validated = validate_deployment_profile(valid_profile)
        self.assertEqual(validated["profile_version"], "1.0.0")

        bad_execution = dict(valid_profile, live_execution_enabled=True)
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_execution)

        bad_adapter = dict(valid_profile, execution_adapter="live_broker")
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_adapter)

        bad_network_policy = dict(valid_profile, outbound_network={"default_policy": "allow", "allowed_hosts": []})
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_network_policy)

        bad_wildcard_host = dict(valid_profile, outbound_network={"default_policy": "deny", "allowed_hosts": ["*"]})
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_wildcard_host)

        bad_permission = dict(valid_profile, credentials={
            "source": "vault",
            "permissions": ["market_data.read", "orders.write"],
            "redact_from_logs": True,
            "secrets_in_config": False,
        })
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_permission)

        bad_scope = dict(valid_profile, credentials={
            "source": "vault",
            "permissions": ["market_data.admin"],
            "redact_from_logs": True,
            "secrets_in_config": False,
        })
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_scope)

        bad_secrets_in_config = dict(valid_profile, credentials={
            "source": "vault",
            "permissions": ["market_data.read"],
            "redact_from_logs": True,
            "secrets_in_config": True,
        })
        with self.assertRaises(DeploymentProfileError):
            validate_deployment_profile(bad_secrets_in_config)

    def test_observability_snapshot_and_metrics_publishing(self):
        catalog = sample_catalog()
        adapter = StubAdapter(provider_id="test_prov")
        normalizer = JsonFixtureNormalizer(catalog)
        quality = DataQualityMonitor(QualityPolicy())
        pipeline = MarketDataPipeline(adapter, normalizer, quality)

        processor = EventProcessor(pipeline, catalog, NoOpStrategy(), EventProcessorConfig())
        processor.start()
        processor.submit(make_raw_frame(1, provider_id="test_prov"))
        processor.submit(make_raw_frame(2, provider_id="test_prov"))
        report = processor.finish(timeout=5)

        policy = AlertPolicy(
            max_receive_silence_ns=1_000_000,
            queue_utilization_threshold=Decimal("0.80"),
            quality_count_threshold=1,
            recording_required=False,
            max_spread_by_instrument={"SYNTH": Decimal("2.0")},
        )

        now_ns = 5_000_000_000
        snapshot = build_observability_snapshot(
            report,
            pipeline,
            now_monotonic_ns=now_ns,
            policy=policy,
            spread_by_instrument={"SYNTH": Decimal("0.5")},
        )

        mapping = snapshot.to_mapping()
        self.assertEqual(mapping["schema_version"], "1.0.0")
        self.assertEqual(mapping["session"]["session_id"], report.session_id)

        metrics = snapshot.metrics
        self.assertIn("market.feed.connected.test_prov", metrics)
        self.assertIn("market.feed.frames_received.test_prov", metrics)
        self.assertIn("market.feed.sequence_gaps", metrics)
        self.assertIn("market.feed.duplicates", metrics)
        self.assertIn("market.feed.stale_events", metrics)
        self.assertIn("market.engine.events.processed", metrics)
        self.assertIn("market.engine.fills", metrics)
        self.assertIn("market.engine.orders", metrics)
        self.assertIn("market.portfolio.version", metrics)
        self.assertIn("market.portfolio.ledger_entries", metrics)

        found_queue = any("market.engine.queue.utilization" in k for k in metrics)
        self.assertTrue(found_queue)
        found_stage = any("market.engine.stage.duration_ns" in k for k in metrics)
        self.assertTrue(found_stage)

    def test_alerts_detection_and_logging(self):
        catalog = sample_catalog()
        adapter = MagicMock()
        adapter.provider_id = "mock_bad_prov"
        adapter.health.return_value = AdapterHealth(
            connected=False,
            status="disconnected",
            detail="Socket lost",
            received_frames=0,
            last_received_monotonic_ns=None,
        )
        normalizer = JsonFixtureNormalizer(catalog)
        quality = DataQualityMonitor(QualityPolicy())
        pipeline = MarketDataPipeline(adapter, normalizer, quality)

        processor = EventProcessor(pipeline, catalog, NoOpStrategy(), EventProcessorConfig())
        processor.start()
        report = processor.finish(timeout=5)

        policy = AlertPolicy(
            max_receive_silence_ns=500_000,
            queue_utilization_threshold=Decimal("0.50"),
            quality_count_threshold=1,
            recording_required=True,
            max_spread_by_instrument={"SYNTH": Decimal("1.0")},
        )

        snapshot = build_observability_snapshot(
            report,
            pipeline,
            now_monotonic_ns=10_000_000,
            policy=policy,
            replay_matched=False,
            spread_by_instrument={"SYNTH": Decimal("5.0")},
        )

        codes = {alert.code for alert in snapshot.alerts}
        self.assertIn("feed_disconnected", codes)
        self.assertIn("recording_missing", codes)
        self.assertIn("replay_divergence", codes)
        self.assertIn("spread_anomalous", codes)

        test_logger = logging.getLogger("test.alert.logger")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.WARNING)

        try:
            log_alerts(snapshot, test_logger)
            output = stream.getvalue()
            self.assertIn("feed_disconnected", output)
            self.assertIn("recording_missing", output)
            self.assertIn("replay_divergence", output)
            self.assertIn("spread_anomalous", output)
        finally:
            test_logger.removeHandler(handler)

    def test_cli_validate_deployment_and_alerts(self):
        ret_dep = cli_main(["validate-deployment", "config/deployment.profile.json"])
        self.assertEqual(ret_dep, 0)

        ret_alerts = cli_main(["validate-alerts", "config/alerts.json"])
        self.assertEqual(ret_alerts, 0)
