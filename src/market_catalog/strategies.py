"""Versioned, state-explicit strategy API for deterministic simulation."""

from __future__ import annotations

import hashlib
import json
import math
import random
from functools import lru_cache
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .events import MarketEvent
from .simulation import (
    CancelOrderIntent,
    ConvertedMetrics,
    ExposureSnapshot,
    MarginSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    TopOfBook,
)


STRATEGY_API_VERSION = "1.0.0"
STRATEGY_STATE_VERSION = "1.0.0"
_RNG_ALGORITHM = "python.random.Random"
_SCHEMA_FILES = {
    "config": "strategy_config.schema.json",
    "state": "strategy_state.schema.json",
}


@lru_cache(maxsize=2)
def _schema_validator(kind: str) -> Draft202012Validator:
    filename = _SCHEMA_FILES[kind]
    source_path = Path(__file__).resolve().parents[2] / "schemas" / filename
    if not source_path.is_file():
        source_path = Path(sys.prefix) / "share" / "market-catalog" / "schemas" / filename
    schema = json.loads(source_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_schema(kind: str, value: Mapping[str, Any]) -> None:
    error = next(_schema_validator(kind).iter_errors(value), None)
    if error is not None:
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise ValueError(f"{path}: {error.message}")


def _freeze_json(value: Any, path: str = "$", *, object_root: bool = False) -> Any:
    """Validate JSON-compatible data and recursively make it immutable."""

    if isinstance(value, Decimal):
        raise TypeError(f"{path}: encode exact decimals as JSON strings, not Decimal objects.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: JSON numbers must be finite.")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path}: JSON object keys must be strings.")
        return MappingProxyType(
            {key: _freeze_json(item, f"{path}.{key}") for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[{index}]") for index, item in enumerate(value))
    raise TypeError(f"{path}: {type(value).__name__} is not JSON-compatible.")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_event_payload(value: Any, path: str = "event.payload") -> Any:
    """Freeze canonical event data while preserving its exact Decimal leaves."""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path}: event payload keys must be strings.")
        return MappingProxyType(
            {key: _freeze_event_payload(item, f"{path}.{key}") for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(
            _freeze_event_payload(item, f"{path}[{index}]") for index, item in enumerate(value)
        )
    return _freeze_json(value, path)


def _object_json(value: Any, name: str) -> Mapping[str, Any]:
    frozen = _freeze_json(value, name, object_root=True)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object.")
    return frozen


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


@dataclass(frozen=True)
class StrategyConfig:
    """Validated identity and JSON parameters for one strategy instance."""

    strategy_id: str
    instance_id: str
    strategy_version: str
    parameters: Mapping[str, Any]
    api_version: str = STRATEGY_API_VERSION

    def __post_init__(self) -> None:
        for name in ("strategy_id", "instance_id", "strategy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
        if self.api_version != STRATEGY_API_VERSION:
            raise ValueError(
                f"Unsupported strategy API {self.api_version!r}; expected {STRATEGY_API_VERSION!r}."
            )
        object.__setattr__(self, "parameters", _object_json(self.parameters, "parameters"))
        _validate_schema("config", self.to_mapping())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StrategyConfig":
        if not isinstance(value, Mapping):
            raise TypeError("strategy config must be a mapping.")
        if any(not isinstance(key, str) for key in value):
            raise ValueError("strategy config field names must be strings.")
        allowed = {"strategy_id", "instance_id", "strategy_version", "api_version", "parameters"}
        unknown = set(value) - allowed
        missing = {"strategy_id", "instance_id", "strategy_version", "parameters"} - set(value)
        if unknown:
            raise ValueError(f"Unknown strategy config fields: {sorted(unknown)}.")
        if missing:
            raise ValueError(f"Missing strategy config fields: {sorted(missing)}.")
        return cls(
            strategy_id=value["strategy_id"],
            instance_id=value["instance_id"],
            strategy_version=value["strategy_version"],
            api_version=value.get("api_version", STRATEGY_API_VERSION),
            parameters=value["parameters"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "strategy_id": self.strategy_id,
            "instance_id": self.instance_id,
            "strategy_version": self.strategy_version,
            "parameters": _thaw_json(self.parameters),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_mapping()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ViewVersion:
    session_id: str
    version: int
    arrival_ordinal: int


@dataclass(frozen=True)
class StrategyMarketView:
    """Immutable event and current L1 view, identified by session and ordinal."""

    snapshot_id: ViewVersion
    event: MarketEvent
    top_of_book: TopOfBook


@dataclass(frozen=True)
class StrategyPortfolioView:
    """Immutable ledger snapshot and its committed version/time boundary."""

    snapshot_id: ViewVersion
    cash_balances: Mapping[str, Decimal]
    positions: Mapping[str, Decimal]
    realized_pnl: Mapping[str, Decimal]
    unrealized_pnl: Mapping[str, Decimal]
    commissions_paid: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    dividends_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    financing_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    exposure: ExposureSnapshot = field(default_factory=ExposureSnapshot)
    margin: MarginSnapshot = field(default_factory=MarginSnapshot)
    converted: ConvertedMetrics = field(default_factory=ConvertedMetrics)


@dataclass(frozen=True)
class StrategyContext:
    """Instance-local deterministic context; random draws are seeded and persisted."""

    config: StrategyConfig
    seed: int
    rng: random.Random


@dataclass(frozen=True)
class StrategyDecision:
    """New serializable strategy state and zero or more simulated intents."""

    state: Mapping[str, Any]
    intents: tuple[OrderIntent | CancelOrderIntent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _object_json(self.state, "state"))
        intents = tuple(self.intents)
        if any(not isinstance(intent, (OrderIntent, CancelOrderIntent)) for intent in intents):
            raise TypeError("StrategyDecision.intents must contain order or cancel intent values.")
        object.__setattr__(self, "intents", intents)


class StrategyV1(Protocol):
    """Strategy callbacks receive snapshots and return explicit replacement state."""

    def on_market(
        self,
        context: StrategyContext,
        market: StrategyMarketView,
        portfolio: StrategyPortfolioView,
        state: Mapping[str, Any],
    ) -> StrategyDecision:
        """Return updated JSON state and simulated intents for one usable event."""


@dataclass(frozen=True)
class StrategyRunRecord:
    strategy_id: str
    instance_id: str
    strategy_version: str
    api_version: str
    seed: int
    config_json: str
    state_json: str
    callback_count: int
    lifecycle_status: str


class StrategyInstance:
    """Logical in-process strategy instance with isolated config, state and PRNG."""

    def __init__(
        self,
        strategy: StrategyV1,
        config: StrategyConfig,
        *,
        seed: int,
        initial_state: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer.")
        callback = getattr(strategy, "on_market", None)
        if not callable(callback):
            raise TypeError("Strategy API 1.x objects must implement on_market(...).")
        declared_api = getattr(strategy, "api_version", config.api_version)
        if declared_api != config.api_version:
            raise ValueError(
                f"Strategy declares API {declared_api!r}, config selects {config.api_version!r}."
            )
        self.strategy = strategy
        self.config = config
        self.seed = seed
        self._state = _object_json({} if initial_state is None else initial_state, "initial_state")
        self._callback_count = 0
        self._rng = random.Random(seed)
        self._context = StrategyContext(config=config, seed=seed, rng=self._rng)
        self._started = False
        self._closed = False
        self._restored = False
        self._lifecycle_status = "created"

        config_validator = getattr(strategy, "validate_config", None)
        if callable(config_validator):
            result = config_validator(config.parameters)
            if result is False:
                raise ValueError(f"Strategy {config.strategy_id!r} rejected its configuration.")

    @property
    def state(self) -> Mapping[str, Any]:
        return self._state

    @property
    def callback_count(self) -> int:
        return self._callback_count

    @property
    def lifecycle_status(self) -> str:
        return self._lifecycle_status

    def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("A closed strategy instance cannot be restarted.")
        callback = getattr(self.strategy, "on_start", None)
        restored_rng_state = self._rng.getstate() if self._restored else None
        try:
            if callable(callback):
                callback(self._context, self._state)
        finally:
            # on_start can run again after a checkpoint restore. Do not consume
            # the restored event-time random stream a second time.
            if restored_rng_state is not None:
                self._rng.setstate(restored_rng_state)
                self._restored = False
        self._started = True
        self._lifecycle_status = "running"

    def on_market(
        self,
        event: MarketEvent,
        book: TopOfBook,
        portfolio: PortfolioSnapshot,
        *,
        session_id: str,
    ) -> tuple[OrderIntent | CancelOrderIntent, ...]:
        if not self._started or self._closed:
            raise RuntimeError("Strategy instance must be started and open before on_market.")
        frozen_event = MarketEvent(
            provider_id=event.provider_id,
            channel=event.channel,
            instrument_id=event.instrument_id,
            event_type=event.event_type,
            source_timestamp_raw=event.source_timestamp_raw,
            source_timestamp_ns=event.source_timestamp_ns,
            timestamp_semantics=event.timestamp_semantics,
            receive_time_utc_ns=event.receive_time_utc_ns,
            receive_time_monotonic_ns=event.receive_time_monotonic_ns,
            arrival_ordinal=event.arrival_ordinal,
            sequence=event.sequence,
            sequence_scope=event.sequence_scope,
            payload=_freeze_event_payload(event.payload),
            quality_flags=tuple(event.quality_flags),
            decision_usable=event.decision_usable,
            book_state=event.book_state,
            raw_frame=event.raw_frame,
            schema_version=event.schema_version,
        )
        market = StrategyMarketView(
            snapshot_id=ViewVersion(session_id, event.arrival_ordinal, event.arrival_ordinal),
            event=frozen_event,
            top_of_book=book,
        )
        portfolio_view = StrategyPortfolioView(
            snapshot_id=ViewVersion(
                session_id,
                portfolio.version,
                -1 if portfolio.as_of_ordinal is None else portfolio.as_of_ordinal,
            ),
            cash_balances=portfolio.cash_balances,
            positions=portfolio.positions,
            realized_pnl=portfolio.realized_pnl,
            unrealized_pnl=portfolio.unrealized_pnl,
            commissions_paid=portfolio.commissions_paid,
            dividends_by_currency=portfolio.dividends_by_currency,
            financing_by_currency=portfolio.financing_by_currency,
            exposure=portfolio.exposure,
            margin=portfolio.margin,
            converted=portfolio.converted,
        )

        result = self.strategy.on_market(self._context, market, portfolio_view, self._state)
        if result is None:
            result = StrategyDecision(state=self._state)
        if not isinstance(result, StrategyDecision):
            raise TypeError("on_market must return StrategyDecision (or None to keep state unchanged).")
        self._state = result.state
        self._callback_count += 1
        # Order IDs are namespaced before shared validation so instances cannot collide.
        namespaced: list[OrderIntent | CancelOrderIntent] = []
        for intent in result.intents:
            if isinstance(intent, OrderIntent):
                namespaced.append(OrderIntent(
                    order_id=f"{self.config.instance_id}/{intent.order_id}",
                    instrument_id=intent.instrument_id,
                    side=intent.side,
                    order_type=intent.order_type,
                    quantity=intent.quantity,
                    limit_price=intent.limit_price,
                ))
            else:
                namespaced.append(CancelOrderIntent(
                    request_id=f"{self.config.instance_id}/{intent.request_id}",
                    order_id=f"{self.config.instance_id}/{intent.order_id}",
                ))
        return tuple(namespaced)

    def close(self) -> None:
        if self._closed:
            return
        if not self._started:
            self.start()
        callback = getattr(self.strategy, "on_stop", None)
        try:
            if callable(callback):
                callback(self._context, self._state)
        except Exception:
            self._lifecycle_status = "close_failed"
            raise
        else:
            self._lifecycle_status = "closed"
        finally:
            self._closed = True

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "snapshot_version": STRATEGY_STATE_VERSION,
            "api_version": self.config.api_version,
            "strategy_id": self.config.strategy_id,
            "instance_id": self.config.instance_id,
            "strategy_version": self.config.strategy_version,
            "config_sha256": self.config.digest,
            "seed": self.seed,
            "rng_algorithm": _RNG_ALGORITHM,
            "callback_count": self._callback_count,
            "state": _thaw_json(self._state),
            "rng_state": _thaw_json(_freeze_json(self._rng.getstate(), "rng_state")),
        }

    def state_json(self) -> str:
        snapshot = self.state_snapshot()
        _validate_schema("state", snapshot)
        return _canonical_json(snapshot)

    def save_state(self, path: str | Path) -> None:
        Path(path).write_text(self.state_json() + "\n", encoding="utf-8")

    def restore_state(self, snapshot: Mapping[str, Any] | str | Path) -> None:
        """Restore only before start, after checking API, identity, config and seed."""

        if self._started or self._closed:
            raise RuntimeError("Strategy state can only be restored before the instance starts.")
        if isinstance(snapshot, Path):
            document = json.loads(snapshot.read_text(encoding="utf-8"))
        elif isinstance(snapshot, str):
            try:
                document = json.loads(snapshot)
            except json.JSONDecodeError:
                document = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        else:
            document = dict(snapshot)
        if not isinstance(document, Mapping):
            raise ValueError("State snapshot must be a JSON object.")
        _validate_schema("state", document)
        required = {
            "snapshot_version", "api_version", "strategy_id", "instance_id",
            "strategy_version", "config_sha256", "seed", "rng_algorithm",
            "callback_count", "state", "rng_state",
        }
        if set(document) != required:
            raise ValueError("State snapshot fields do not match StrategyState 1.0.0.")
        expected = {
            "snapshot_version": STRATEGY_STATE_VERSION,
            "api_version": self.config.api_version,
            "strategy_id": self.config.strategy_id,
            "instance_id": self.config.instance_id,
            "strategy_version": self.config.strategy_version,
            "config_sha256": self.config.digest,
            "seed": self.seed,
            "rng_algorithm": _RNG_ALGORITHM,
        }
        for name, value in expected.items():
            if document.get(name) != value:
                raise ValueError(f"State snapshot {name} does not match this strategy instance.")
        callback_count = document["callback_count"]
        if isinstance(callback_count, bool) or not isinstance(callback_count, int) or callback_count < 0:
            raise ValueError("State snapshot callback_count must be a non-negative integer.")
        state = _object_json(document["state"], "state")
        rng_state = _json_to_tuple(document["rng_state"])
        # Validate PRNG state before mutating the instance.
        test_rng = random.Random(self.seed)
        test_rng.setstate(rng_state)
        self._state = state
        self._callback_count = callback_count
        self._rng.setstate(rng_state)
        self._restored = True

    def run_record(self) -> StrategyRunRecord:
        return StrategyRunRecord(
            strategy_id=self.config.strategy_id,
            instance_id=self.config.instance_id,
            strategy_version=self.config.strategy_version,
            api_version=self.config.api_version,
            seed=self.seed,
            config_json=_canonical_json(self.config.to_mapping()),
            state_json=self.state_json(),
            callback_count=self._callback_count,
            lifecycle_status=self._lifecycle_status,
        )


def _json_to_tuple(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_json_to_tuple(item) for item in value)
    if isinstance(value, dict):
        return {key: _json_to_tuple(item) for key, item in value.items()}
    return value
