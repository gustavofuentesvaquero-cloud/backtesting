"""Deterministic L1 book, order validation, matching, and portfolio primitives.

This module deliberately describes a limited simulation model. It does not
model queue priority, hidden liquidity, exchange margin rules, or live routing.
"""

from __future__ import annotations

import re
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
)
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from .events import MarketEvent
from .models import Catalog

LEDGER_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class TopOfBook:
    instrument_id: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    arrival_ordinal: int


class Strategy(Protocol):
    """Synchronous Python callback invoked in session arrival order."""

    def on_event(
        self, event: MarketEvent, book: TopOfBook
    ) -> Iterable["OrderIntent | CancelOrderIntent"] | "OrderIntent | CancelOrderIntent" | None:
        """Return zero or more simulated order intents for this instrument."""


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class CancelOrderIntent:
    """Request to cancel a simulated order within the current session."""

    request_id: str
    order_id: str


@dataclass(frozen=True)
class LatencyModel:
    version: str = "1.0.0"
    decision_to_arrival_ns: int = 0

    def __post_init__(self) -> None:
        _validate_model_version(self.version, "latency.version")
        if isinstance(self.decision_to_arrival_ns, bool) or not isinstance(self.decision_to_arrival_ns, int):
            raise ValueError("decision_to_arrival_ns must be an integer.")
        if self.decision_to_arrival_ns < 0:
            raise ValueError("decision_to_arrival_ns must be non-negative.")


@dataclass(frozen=True)
class SlippageModel:
    version: str = "1.0.0"
    ticks: int = 0

    def __post_init__(self) -> None:
        _validate_model_version(self.version, "slippage.version")
        if isinstance(self.ticks, bool) or not isinstance(self.ticks, int) or self.ticks < 0:
            raise ValueError("slippage ticks must be a non-negative integer.")


_ROUNDING_MODES = {
    "ROUND_CEILING": ROUND_CEILING,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_FLOOR": ROUND_FLOOR,
    "ROUND_HALF_DOWN": ROUND_HALF_DOWN,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_UP": ROUND_UP,
}


@dataclass(frozen=True)
class CommissionModel:
    version: str = "1.0.0"
    per_unit: Decimal = Decimal("0")
    notional_rate: Decimal = Decimal("0")
    currency: str | None = None
    currency_precision: int | None = None
    rounding: str = "ROUND_HALF_EVEN"

    def __post_init__(self) -> None:
        _validate_model_version(self.version, "commission.version")
        for name in ("per_unit", "notional_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"commission {name} must be a finite non-negative Decimal.")
        if self.currency is not None and (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency.upper() != self.currency
        ):
            raise ValueError("commission currency must be a three-letter uppercase code.")
        if self.currency_precision is not None and (
            isinstance(self.currency_precision, bool)
            or not isinstance(self.currency_precision, int)
            or not 0 <= self.currency_precision <= 18
        ):
            raise ValueError("currency_precision must be between 0 and 18 when provided.")
        if self.rounding not in _ROUNDING_MODES:
            raise ValueError(f"Unsupported commission rounding mode {self.rounding!r}.")


@dataclass(frozen=True)
class MatchingPolicy:
    """Serializable, explicitly versioned assumptions for the L1 simulator."""

    version: str = "1.0.0"
    queue_model: str = "conservative_l1_no_touch"
    spread_model: str = "observed_l1_bid_ask"
    virtual_time_model: str = "source_utc_then_receive_utc"
    latency: LatencyModel = field(default_factory=LatencyModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)
    commission: CommissionModel = field(default_factory=CommissionModel)

    def __post_init__(self) -> None:
        _validate_model_version(self.version, "matching_policy.version")
        if self.queue_model != "conservative_l1_no_touch":
            raise ValueError("queue_model must be 'conservative_l1_no_touch'.")
        if self.spread_model != "observed_l1_bid_ask":
            raise ValueError("spread_model must be 'observed_l1_bid_ask'.")
        if self.virtual_time_model != "source_utc_then_receive_utc":
            raise ValueError("virtual_time_model must be 'source_utc_then_receive_utc'.")
        if not isinstance(self.latency, LatencyModel):
            raise TypeError("latency must be a LatencyModel.")
        if not isinstance(self.slippage, SlippageModel):
            raise TypeError("slippage must be a SlippageModel.")
        if not isinstance(self.commission, CommissionModel):
            raise TypeError("commission must be a CommissionModel.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "queue_model": self.queue_model,
            "spread_model": self.spread_model,
            "virtual_time_model": self.virtual_time_model,
            "latency": {
                "version": self.latency.version,
                "decision_to_arrival_ns": self.latency.decision_to_arrival_ns,
            },
            "slippage": {"version": self.slippage.version, "ticks": self.slippage.ticks},
            "commission": {
                "version": self.commission.version,
                "per_unit": format(self.commission.per_unit, "f"),
                "notional_rate": format(self.commission.notional_rate, "f"),
                "currency": self.commission.currency,
                "currency_precision": self.commission.currency_precision,
                "rounding": self.commission.rounding,
            },
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MatchingPolicy":
        if not isinstance(value, Mapping):
            raise TypeError("matching policy must be a mapping.")
        _require_exact_fields(value, {"version", "queue_model", "spread_model", "virtual_time_model", "latency", "slippage", "commission"}, "matching policy")
        latency = value["latency"]
        slippage = value["slippage"]
        commission = value["commission"]
        if not isinstance(latency, Mapping) or not isinstance(slippage, Mapping) or not isinstance(commission, Mapping):
            raise TypeError("latency, slippage, and commission must be objects.")
        _require_exact_fields(latency, {"version", "decision_to_arrival_ns"}, "latency")
        _require_exact_fields(slippage, {"version", "ticks"}, "slippage")
        _require_exact_fields(commission, {"version", "per_unit", "notional_rate", "currency", "currency_precision", "rounding"}, "commission")
        return cls(
            version=value["version"],
            queue_model=value["queue_model"],
            spread_model=value["spread_model"],
            virtual_time_model=value["virtual_time_model"],
            latency=LatencyModel(**latency),
            slippage=SlippageModel(**slippage),
            commission=CommissionModel(
                version=commission["version"],
                per_unit=_policy_decimal(commission["per_unit"], "commission.per_unit"),
                notional_rate=_policy_decimal(commission["notional_rate"], "commission.notional_rate"),
                currency=commission["currency"],
                currency_precision=commission["currency_precision"],
                rounding=commission["rounding"],
            ),
        )


def _validate_model_version(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty version string.")


def _policy_decimal(value: Any, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be encoded as a decimal string.")
    if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None:
        raise ValueError(f"{field_name} must be a non-negative plain decimal string.")
    return Decimal(value)


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"Unknown {name} fields: {sorted(unknown)}.")
    if missing:
        raise ValueError(f"Missing {name} fields: {sorted(missing)}.")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    currency: str
    arrival_ordinal: int
    decision_ordinal: int | None = None
    decision_timestamp_ns: int | None = None
    order_arrival_timestamp_ns: int | None = None
    match_timestamp_ns: int | None = None
    bid_at_match: Decimal | None = None
    ask_at_match: Decimal | None = None
    spread_at_match: Decimal | None = None
    slippage: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    commission_currency: str | None = None
    liquidity_source: str = "displayed_l1"
    matching_rule: str = "l1_marketable_only_v1"
    policy_version: str = "1.0.0"
    match_provider_id: str | None = None
    match_channel: str | None = None
    match_sequence: int | None = None
    bid_quantity_at_match: Decimal | None = None
    ask_quantity_at_match: Decimal | None = None
    available_quantity_before_fill: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "instrument_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"fill {name} must be non-empty.")
        if self.side not in {"buy", "sell"}:
            raise ValueError("fill side must be buy or sell.")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isalpha() or self.currency.upper() != self.currency:
            raise ValueError("fill currency must be a three-letter uppercase code.")
        for name in _FILL_DECIMAL_FIELDS:
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise ValueError(f"fill {name} must be a finite Decimal when provided.")
        if self.quantity <= 0 or self.price <= 0 or self.commission < 0 or self.slippage < 0:
            raise ValueError("fill quantity and price must be positive; commission and slippage cannot be negative.")
        for name in ("arrival_ordinal", "decision_ordinal", "decision_timestamp_ns", "order_arrival_timestamp_ns", "match_timestamp_ns", "match_sequence"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"fill {name} must be a non-negative integer when provided.")
        for name in ("commission_currency",):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha() or value.upper() != value):
                raise ValueError(f"fill {name} must be a three-letter uppercase currency code when provided.")
        for name in ("liquidity_source", "matching_rule", "policy_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"fill {name} must be non-empty.")
        for name in ("match_provider_id", "match_channel"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"fill {name} must be non-empty when provided.")
        for name in ("bid_at_match", "ask_at_match"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"fill {name} must be positive when provided.")
        for name in ("spread_at_match", "bid_quantity_at_match", "ask_quantity_at_match", "available_quantity_before_fill"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"fill {name} cannot be negative.")


@dataclass(frozen=True)
class OrderOutcome:
    order_id: str
    instrument_id: str
    status: str
    filled_quantity: Decimal
    remaining_quantity: Decimal
    reason: str | None = None
    decision_ordinal: int | None = None
    decision_timestamp_ns: int | None = None
    accepted_timestamp_ns: int | None = None
    arrival_timestamp_ns: int | None = None
    last_update_timestamp_ns: int | None = None
    completion_timestamp_ns: int | None = None
    commission_total: Decimal = Decimal(0)
    commission_currency: str | None = None
    policy_version: str = "1.0.0"


@dataclass(frozen=True)
class CancellationOutcome:
    request_id: str
    order_id: str
    status: str
    timestamp_ns: int | None
    reason: str | None = None
    arrival_ordinal: int | None = None


@dataclass(frozen=True)
class CashFlow:
    currency: str
    amount: Decimal
    category: str

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isalpha() or self.currency.upper() != self.currency:
            raise ValueError("cash-flow currency must be a three-letter uppercase code.")
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise ValueError("cash-flow amount must be a finite Decimal.")
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("cash-flow category must be non-empty.")


@dataclass(frozen=True)
class ConversionRate:
    """Captured direct conversion: one unit of from_currency equals rate in to_currency."""

    from_currency: str
    to_currency: str
    rate: Decimal
    timestamp_ns: int
    source: str
    arrival_ordinal: int | None = None
    instrument_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("from_currency", "to_currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha() or value.upper() != value:
                raise ValueError(f"{name} must be a three-letter uppercase currency code.")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate <= 0:
            raise ValueError("conversion rate must be a finite positive Decimal.")
        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int) or self.timestamp_ns < 0:
            raise ValueError("conversion timestamp_ns must be a non-negative integer.")
        if self.arrival_ordinal is not None and (
            isinstance(self.arrival_ordinal, bool) or not isinstance(self.arrival_ordinal, int) or self.arrival_ordinal < 0
        ):
            raise ValueError("conversion arrival_ordinal must be a non-negative integer when provided.")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("conversion source must be a non-empty string.")


@dataclass(frozen=True)
class ExposureLimit:
    scope: str
    key: str
    max_abs_notional: Decimal

    def __post_init__(self) -> None:
        if self.scope not in {"instrument", "currency"}:
            raise ValueError("exposure limit scope must be 'instrument' or 'currency'.")
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("exposure limit key must be non-empty.")
        if self.scope == "currency" and (
            len(self.key) != 3 or not self.key.isascii() or not self.key.isalpha() or self.key.upper() != self.key
        ):
            raise ValueError("currency exposure limit key must be a three-letter uppercase currency code.")
        if not isinstance(self.max_abs_notional, Decimal) or not self.max_abs_notional.is_finite() or self.max_abs_notional < 0:
            raise ValueError("max_abs_notional must be a finite non-negative Decimal.")


@dataclass(frozen=True)
class MarginRule:
    instrument_id: str
    model: str
    initial_requirement: Decimal
    maintenance_requirement: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("margin rule instrument_id must be non-empty.")
        if self.model not in {"per_unit", "notional_rate"}:
            raise ValueError("margin model must be 'per_unit' or 'notional_rate'.")
        for name in ("initial_requirement", "maintenance_requirement"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal.")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isalpha() or self.currency.upper() != self.currency:
            raise ValueError("margin rule currency must be a three-letter uppercase code.")


@dataclass(frozen=True)
class PortfolioPolicy:
    reporting_currency: str | None = None
    max_conversion_age_ns: int | None = None
    exposure_limits: tuple[ExposureLimit, ...] = ()
    margin_rules: tuple[MarginRule, ...] = ()
    version: str = "1.0.0"
    mark_model: str = "conservative_l1_bid_for_longs_ask_for_shorts"
    equity_price_basis: str = "raw"

    def __post_init__(self) -> None:
        _validate_model_version(self.version, "portfolio_policy.version")
        if self.equity_price_basis not in {"raw", "split_adjusted", "total_return_adjusted"}:
            raise ValueError("Unsupported equity_price_basis.")
        if self.mark_model != "conservative_l1_bid_for_longs_ask_for_shorts":
            raise ValueError("Unsupported portfolio mark_model.")
        if self.reporting_currency is not None and (
            not isinstance(self.reporting_currency, str)
            or len(self.reporting_currency) != 3
            or not self.reporting_currency.isascii()
            or not self.reporting_currency.isalpha()
            or self.reporting_currency.upper() != self.reporting_currency
        ):
            raise ValueError("reporting_currency must be a three-letter uppercase code.")
        if self.max_conversion_age_ns is not None and (
            isinstance(self.max_conversion_age_ns, bool)
            or not isinstance(self.max_conversion_age_ns, int)
            or self.max_conversion_age_ns < 0
        ):
            raise ValueError("max_conversion_age_ns must be a non-negative integer.")
        limits = tuple(self.exposure_limits)
        rules = tuple(self.margin_rules)
        if any(not isinstance(item, ExposureLimit) for item in limits):
            raise TypeError("exposure_limits must contain ExposureLimit values.")
        if any(not isinstance(item, MarginRule) for item in rules):
            raise TypeError("margin_rules must contain MarginRule values.")
        limit_keys = [(item.scope, item.key) for item in limits]
        if len(set(limit_keys)) != len(limit_keys):
            raise ValueError("exposure limit scope/key pairs must be unique.")
        rule_ids = [item.instrument_id for item in rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("margin rules must be unique by instrument_id.")
        object.__setattr__(self, "exposure_limits", limits)
        object.__setattr__(self, "margin_rules", rules)

    def to_mapping(self) -> dict[str, Any]:
        result = {
            "version": self.version,
            "mark_model": self.mark_model,
            "reporting_currency": self.reporting_currency,
            "max_conversion_age_ns": self.max_conversion_age_ns,
            "exposure_limits": [
                {
                    "scope": item.scope,
                    "key": item.key,
                    "max_abs_notional": format(item.max_abs_notional, "f"),
                }
                for item in self.exposure_limits
            ],
            "margin_rules": [
                {
                    "instrument_id": item.instrument_id,
                    "model": item.model,
                    "initial_requirement": format(item.initial_requirement, "f"),
                    "maintenance_requirement": format(item.maintenance_requirement, "f"),
                    "currency": item.currency,
                }
                for item in self.margin_rules
            ],
        }
        # Preserve recorded raw-policy hashes; the schema default is raw.
        if self.equity_price_basis != "raw":
            result["equity_price_basis"] = self.equity_price_basis
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PortfolioPolicy":
        expected = {"version", "mark_model", "reporting_currency", "max_conversion_age_ns", "exposure_limits", "margin_rules"}
        if not isinstance(value, Mapping) or set(value) - {"equity_price_basis"} != expected:
            raise ValueError("Portfolio policy has missing or unknown fields.")
        if not isinstance(value["exposure_limits"], (list, tuple)) or not isinstance(value["margin_rules"], (list, tuple)):
            raise ValueError("Portfolio policy exposure_limits and margin_rules must be arrays.")
        if any(not isinstance(item, Mapping) or set(item) != {"scope", "key", "max_abs_notional"} for item in value["exposure_limits"]):
            raise ValueError("Portfolio policy exposure limit has missing or unknown fields.")
        if any(not isinstance(item, Mapping) or set(item) != {"instrument_id", "model", "initial_requirement", "maintenance_requirement", "currency"} for item in value["margin_rules"]):
            raise ValueError("Portfolio policy margin rule has missing or unknown fields.")
        limits = tuple(
            ExposureLimit(
                scope=item["scope"], key=item["key"],
                max_abs_notional=_parse_decimal_string(item["max_abs_notional"], "exposure_limit.max_abs_notional"),
            )
            for item in value["exposure_limits"]
        )
        rules = tuple(
            MarginRule(
                instrument_id=item["instrument_id"], model=item["model"],
                initial_requirement=_parse_decimal_string(item["initial_requirement"], "margin_rule.initial_requirement"),
                maintenance_requirement=_parse_decimal_string(item["maintenance_requirement"], "margin_rule.maintenance_requirement"),
                currency=item["currency"],
            )
            for item in value["margin_rules"]
        )
        return cls(
            reporting_currency=value["reporting_currency"],
            max_conversion_age_ns=value["max_conversion_age_ns"],
            exposure_limits=limits,
            margin_rules=rules,
            version=value["version"],
            mark_model=value["mark_model"],
            equity_price_basis=value.get("equity_price_basis", "raw"),
        )


@dataclass(frozen=True)
class InstrumentExposure:
    instrument_id: str
    quantity: Decimal
    mark_price: Decimal | None
    signed_notional: Decimal | None
    notional_currency: str
    base_currency_exposure: Decimal | None = None
    base_currency: str | None = None
    mark: "ValuationMark | None" = None


@dataclass(frozen=True)
class ExposureLimitBreach:
    scope: str
    key: str
    actual_abs_notional: Decimal
    maximum_abs_notional: Decimal
    currency: str


@dataclass(frozen=True)
class ExposureSnapshot:
    by_instrument: Mapping[str, InstrumentExposure] = field(default_factory=lambda: MappingProxyType({}))
    gross_notional_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    unpriced_instruments: tuple[str, ...] = ()
    limit_breaches: tuple[ExposureLimitBreach, ...] = ()


@dataclass(frozen=True)
class MarginSnapshot:
    initial_required_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    maintenance_required_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    unconfigured_instruments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConvertedMetrics:
    reporting_currency: str | None = None
    conversion_rates_used: Mapping[str, ConversionRate] = field(default_factory=lambda: MappingProxyType({}))
    cash_balance: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    commissions_paid: Decimal | None = None
    dividends: Decimal | None = None
    financing: Decimal | None = None
    net_pnl: Decimal | None = None
    missing_conversion_currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValuationMark:
    instrument_id: str
    price: Decimal
    side: str
    bid_price: Decimal
    ask_price: Decimal
    timestamp_ns: int | None
    arrival_ordinal: int
    provider_id: str | None = None
    channel: str | None = None
    sequence: int | None = None
    source: str = "observed_l1"


@dataclass(frozen=True)
class LedgerEntry:
    """Immutable business event; derived accounting state can be rebuilt from entries."""

    entry_id: str
    kind: str
    timestamp_ns: int | None
    arrival_ordinal: int | None
    instrument_id: str | None = None
    currency: str | None = None
    amount: Decimal | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    cash_flows: tuple[CashFlow, ...] = ()
    realized_pnl: CashFlow | None = None
    fill: Fill | None = None
    conversion_rate: ConversionRate | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    provider_id: str | None = None
    channel: str | None = None
    sequence: int | None = None
    source: str | None = None
    reference_id: str | None = None
    portfolio_version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("ledger entry_id must be non-empty.")
        if self.kind not in {
            "fill", "commission", "cash_adjustment", "split", "dividend", "funding",
            "futures_settlement", "conversion_rate", "mark", "checkpoint",
        }:
            raise ValueError(f"Unsupported ledger entry kind {self.kind!r}.")
        for name in ("instrument_id", "source", "reference_id", "provider_id", "channel"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"ledger {name} must be non-empty when provided.")
        if self.currency is not None and (
            not isinstance(self.currency, str) or len(self.currency) != 3
            or not self.currency.isascii() or not self.currency.isalpha() or self.currency.upper() != self.currency
        ):
            raise ValueError("ledger currency must be a three-letter uppercase currency code.")
        for name in ("timestamp_ns", "arrival_ordinal", "sequence", "portfolio_version"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"ledger {name} must be a non-negative integer when provided.")
        for name in ("amount", "quantity", "price", "bid_price", "ask_price"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise ValueError(f"ledger {name} must be a finite Decimal when provided.")
        flows = tuple(self.cash_flows)
        if any(not isinstance(flow, CashFlow) for flow in flows):
            raise TypeError("ledger cash_flows must contain CashFlow values.")
        if self.realized_pnl is not None and not isinstance(self.realized_pnl, CashFlow):
            raise TypeError("ledger realized_pnl must be a CashFlow value.")
        if self.fill is not None and not isinstance(self.fill, Fill):
            raise TypeError("ledger fill must be an immutable Fill value.")
        if self.conversion_rate is not None and not isinstance(self.conversion_rate, ConversionRate):
            raise TypeError("ledger conversion_rate must be a ConversionRate value.")
        object.__setattr__(self, "cash_flows", flows)

    def to_mapping(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation of this fact."""
        data = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in {"cash_flows", "realized_pnl", "fill", "conversion_rate"}
        }
        for name in ("amount", "quantity", "price", "bid_price", "ask_price"):
            value = data[name]
            data[name] = None if value is None else format(value, "f")
        data["cash_flows"] = [_cash_flow_to_mapping(flow) for flow in self.cash_flows]
        data["realized_pnl"] = None if self.realized_pnl is None else _cash_flow_to_mapping(self.realized_pnl)
        data["fill"] = None if self.fill is None else _fill_to_mapping(self.fill)
        data["conversion_rate"] = None if self.conversion_rate is None else _conversion_to_mapping(self.conversion_rate)
        data["ledger_schema_version"] = LEDGER_SCHEMA_VERSION
        return data

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LedgerEntry":
        if not isinstance(value, Mapping):
            raise TypeError("ledger entry JSON must be an object.")
        allowed = set(cls.__dataclass_fields__) | {"ledger_schema_version"}
        if set(value) != allowed:
            raise ValueError(f"ledger entry fields differ; missing={sorted(allowed - set(value))}, unknown={sorted(set(value) - allowed)}.")
        if value["ledger_schema_version"] != LEDGER_SCHEMA_VERSION:
            raise ValueError(f"Unsupported ledger schema version {value['ledger_schema_version']!r}.")
        data = dict(value)
        data.pop("ledger_schema_version")
        for name in ("amount", "quantity", "price", "bid_price", "ask_price"):
            raw = data[name]
            data[name] = None if raw is None else _parse_decimal_string(raw, f"ledger.{name}")
        data["cash_flows"] = tuple(_cash_flow_from_mapping(item) for item in data["cash_flows"])
        data["realized_pnl"] = None if data["realized_pnl"] is None else _cash_flow_from_mapping(data["realized_pnl"])
        data["fill"] = None if data["fill"] is None else _fill_from_mapping(data["fill"])
        data["conversion_rate"] = None if data["conversion_rate"] is None else _conversion_from_mapping(data["conversion_rate"])
        return cls(**data)


def _parse_decimal_string(value: Any, field_name: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError(f"{field_name} must be encoded as a plain decimal string.")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return result


def _cash_flow_to_mapping(flow: CashFlow) -> dict[str, Any]:
    return {"currency": flow.currency, "amount": format(flow.amount, "f"), "category": flow.category}


def _cash_flow_from_mapping(value: Mapping[str, Any]) -> CashFlow:
    if not isinstance(value, Mapping) or set(value) != {"currency", "amount", "category"}:
        raise ValueError("cash flow must contain currency, amount, and category only.")
    return CashFlow(value["currency"], _parse_decimal_string(value["amount"], "cash_flow.amount"), value["category"])


def _conversion_to_mapping(rate: ConversionRate) -> dict[str, Any]:
    return {
        "from_currency": rate.from_currency, "to_currency": rate.to_currency,
        "rate": format(rate.rate, "f"), "timestamp_ns": rate.timestamp_ns,
        "source": rate.source, "arrival_ordinal": rate.arrival_ordinal,
        "instrument_id": rate.instrument_id,
    }


def _conversion_from_mapping(value: Mapping[str, Any]) -> ConversionRate:
    required = {"from_currency", "to_currency", "rate", "timestamp_ns", "source", "arrival_ordinal", "instrument_id"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("conversion rate has missing or unknown fields.")
    data = dict(value)
    data["rate"] = _parse_decimal_string(data["rate"], "conversion_rate.rate")
    return ConversionRate(**data)


_FILL_DECIMAL_FIELDS = {
    "quantity", "price", "bid_at_match", "ask_at_match", "spread_at_match",
    "slippage", "commission", "bid_quantity_at_match", "ask_quantity_at_match",
    "available_quantity_before_fill",
}


def _fill_to_mapping(fill: Fill) -> dict[str, Any]:
    return {
        name: (format(value, "f") if isinstance(value, Decimal) else value)
        for name, value in ((key, getattr(fill, key)) for key in fill.__dataclass_fields__)
    }


def _fill_from_mapping(value: Mapping[str, Any]) -> Fill:
    required = set(Fill.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("fill has missing or unknown fields.")
    data = dict(value)
    for name in _FILL_DECIMAL_FIELDS:
        raw = data[name]
        data[name] = None if raw is None else _parse_decimal_string(raw, f"fill.{name}")
    return Fill(**data)


def ledger_to_jsonl(entries: Iterable[LedgerEntry]) -> str:
    """Serialize ledger facts from a portfolio or session report outside processing."""
    return "".join(
        json.dumps(entry.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        for entry in entries
    )


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_balances: Mapping[str, Decimal]
    positions: Mapping[str, Decimal]
    realized_pnl: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    unrealized_pnl: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    version: int = 0
    as_of_ordinal: int | None = None
    commissions_paid: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    dividends_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    financing_by_currency: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    as_of_timestamp_ns: int | None = None
    marks: Mapping[str, ValuationMark] = field(default_factory=lambda: MappingProxyType({}))
    exposure: ExposureSnapshot = field(default_factory=ExposureSnapshot)
    margin: MarginSnapshot = field(default_factory=MarginSnapshot)
    converted: ConvertedMetrics = field(default_factory=ConvertedMetrics)
    ledger_entry_count: int = 0


class OrderValidationError(ValueError):
    """An intent cannot be executed with the selected catalog and model."""


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OrderValidationError(f"{field} must be a finite Decimal.")
    return value


def _multiple_of(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        return False
    return value % increment == 0


class BookBuilder:
    """Keep a usable L1 quote per instrument; L2 remains quality-checked upstream."""

    def __init__(self) -> None:
        self._books: dict[str, TopOfBook] = {}
        self.last_diagnostic: str | None = None

    def invalidate(self, instrument_id: str) -> None:
        self._books.pop(instrument_id, None)

    def update(self, event: MarketEvent | None) -> TopOfBook | None:
        self.last_diagnostic = None
        if event is None:
            return None
        if event.event_type == "quote" and not event.decision_usable:
            self._books.pop(event.instrument_id, None)
            self.last_diagnostic = "unusable_l1_quote"
            return None
        if not event.decision_usable:
            return self._books.get(event.instrument_id)
        if event.event_type != "quote":
            return self._books.get(event.instrument_id)

        bid = event.payload["bid"]
        ask = event.payload["ask"]
        bid_price = bid["price"]
        ask_price = ask["price"]
        if bid_price > ask_price:
            self._books.pop(event.instrument_id, None)
            self.last_diagnostic = "crossed_l1_book"
            return None

        book = TopOfBook(
            instrument_id=event.instrument_id,
            bid_price=bid_price,
            bid_quantity=bid["quantity"],
            ask_price=ask_price,
            ask_quantity=ask["quantity"],
            arrival_ordinal=event.arrival_ordinal,
        )
        self._books[event.instrument_id] = book
        return book


class IntentValidator:
    """Check intents against executable instrument increments and catalog data."""

    def __init__(self, catalog: Catalog):
        self._instruments = {str(item.get("id")): item for item in catalog.instruments}
        self._continuous_ids = {str(item.get("id")) for item in catalog.continuous_futures}
        self._seen_order_ids: set[str] = set()
        self._seen_cancel_ids: set[str] = set()

    def validate(self, intent: OrderIntent, event: MarketEvent) -> None:
        if not isinstance(intent, OrderIntent):
            raise OrderValidationError("strategy outputs must be OrderIntent instances.")
        if not isinstance(intent.order_id, str) or not intent.order_id.strip():
            raise OrderValidationError("order_id must be a non-empty string.")
        if intent.order_id in self._seen_order_ids:
            raise OrderValidationError(f"order_id {intent.order_id!r} was already used in this session.")
        # Reserve ids even when the rest of the intent is invalid. Reusing a
        # rejected id later would make fills and diagnostics ambiguous.
        self._seen_order_ids.add(intent.order_id)
        if intent.instrument_id in self._continuous_ids:
            raise OrderValidationError("continuous futures series are analytical and not executable.")
        if intent.instrument_id != event.instrument_id:
            raise OrderValidationError("an intent must target the instrument that triggered its callback.")
        instrument = self._instruments.get(intent.instrument_id)
        if instrument is None:
            raise OrderValidationError(f"unknown executable instrument {intent.instrument_id!r}.")
        if intent.side not in {"buy", "sell"}:
            raise OrderValidationError("side must be 'buy' or 'sell'.")
        if intent.order_type not in {"market", "limit"}:
            raise OrderValidationError("order_type must be 'market' or 'limit'.")
        quantity = _decimal(intent.quantity, "quantity")
        minimum = instrument.get("minimum_quantity")
        increment = instrument.get("quantity_increment")
        if not isinstance(minimum, Decimal) or not isinstance(increment, Decimal):
            raise OrderValidationError("catalog quantity rules must be parsed as Decimal values.")
        if quantity < minimum or not _multiple_of(quantity - minimum, increment):
            raise OrderValidationError(
                f"quantity must be at least {minimum} and align to increment {increment}."
            )

        if intent.order_type == "market":
            if intent.limit_price is not None:
                raise OrderValidationError("market orders must not specify limit_price.")
            return
        if intent.limit_price is None:
            raise OrderValidationError("limit orders require limit_price.")
        price = _decimal(intent.limit_price, "limit_price")
        tick = instrument.get("price_tick")
        if not isinstance(tick, Decimal) or tick <= 0 or not _multiple_of(price, tick):
            raise OrderValidationError("limit_price must align to the instrument price_tick.")

    def validate_cancel(self, intent: CancelOrderIntent) -> None:
        if not isinstance(intent, CancelOrderIntent):
            raise OrderValidationError("strategy cancellation outputs must be CancelOrderIntent instances.")
        if not isinstance(intent.request_id, str) or not intent.request_id.strip():
            raise OrderValidationError("cancel request_id must be a non-empty string.")
        if intent.request_id in self._seen_cancel_ids:
            raise OrderValidationError(f"cancel request_id {intent.request_id!r} was already used in this session.")
        self._seen_cancel_ids.add(intent.request_id)
        if not isinstance(intent.order_id, str) or not intent.order_id.strip():
            raise OrderValidationError("cancel order_id must be a non-empty string.")


@dataclass
class _MutableBook:
    bid_price: Decimal
    bid_available: Decimal
    ask_price: Decimal
    ask_available: Decimal


@dataclass
class _WorkingOrder:
    intent: OrderIntent
    remaining: Decimal
    filled: Decimal = Decimal(0)
    decision_ordinal: int | None = None
    decision_timestamp_ns: int | None = None
    accepted_timestamp_ns: int | None = None
    arrival_timestamp_ns: int | None = None
    last_update_timestamp_ns: int | None = None
    commission_total: Decimal = Decimal(0)
    commission_currency: str | None = None


class L1Matcher:
    """Conservative L1 matcher with virtual arrival and auditable assumptions."""

    def __init__(self, catalog: Catalog, policy: MatchingPolicy | None = None):
        self._instruments = {str(item.get("id")): item for item in catalog.instruments}
        self.policy = policy or MatchingPolicy()
        self._books: dict[str, _MutableBook] = {}
        self._working: OrderedDict[str, _WorkingOrder] = OrderedDict()
        self._outcomes: dict[str, OrderOutcome] = {}
        self._fill_numbers: dict[str, int] = {}
        self._cancellations: list[CancellationOutcome] = []
        self._seen_cancel_ids: set[str] = set()
        self._last_virtual_timestamp_ns: int | None = None
        self._finished = False

    def process(
        self,
        event: MarketEvent | None,
        book: TopOfBook | None,
        intents: Iterable[OrderIntent],
        cancellations: Iterable[CancelOrderIntent] = (),
        rejected_orders: Iterable[tuple[OrderIntent, str]] = (),
        rejected_cancellations: Iterable[tuple[CancelOrderIntent, str]] = (),
        *,
        decision_timestamp_ns: int | None = None,
    ) -> tuple[Fill, ...]:
        if event is None:
            return ()
        if self._finished:
            raise RuntimeError("A finished matcher cannot accept more events.")
        event_time = _event_timestamp_ns(event)
        if event_time is not None:
            if self._last_virtual_timestamp_ns is None or event_time > self._last_virtual_timestamp_ns:
                self._last_virtual_timestamp_ns = event_time
        current_time = event_time if event_time is not None else decision_timestamp_ns

        for intent, reason in rejected_orders:
            self.reject(intent, reason, event.arrival_ordinal, decision_timestamp_ns, current_time)

        fills: list[Fill] = []
        valid_quote = event.event_type == "quote" and book is not None and event.decision_usable
        if event.event_type == "quote" and not valid_quote:
            self._books.pop(event.instrument_id, None)
        elif valid_quote:
            assert book is not None
            self._books[event.instrument_id] = _MutableBook(
                bid_price=book.bid_price,
                bid_available=book.bid_quantity,
                ask_price=book.ask_price,
                ask_available=book.ask_quantity,
            )
            fills.extend(self._match_working(event, book, current_time))

        # The quote itself predates the callback that emitted these cancels.
        # Existing orders get evaluated against it first; cancels then apply to
        # any remainder before new orders from this decision are accepted.
        for cancellation in cancellations:
            self._cancel(cancellation, current_time, event.arrival_ordinal)
        for intent, reason in rejected_cancellations:
            self.reject_cancel(intent, reason, current_time, event.arrival_ordinal)

        # Existing orders consume a displayed quote before new orders from this
        # same decision, which gives FIFO order within a session.
        for intent in intents:
            working = self._accept(intent, event, decision_timestamp_ns, current_time)
            if working is not None and valid_quote and self._arrived(working, current_time):
                assert book is not None
                fills.extend(self._try_fill(working, event, book, current_time))
                self._update_after_match(working, current_time)
            elif working is not None:
                self._outcomes[intent.order_id] = self._outcome(
                    working, "accepted_pending_arrival", None
                )
        return tuple(fills)

    def reject(
        self,
        intent: OrderIntent,
        reason: str,
        decision_ordinal: int | None,
        decision_timestamp_ns: int | None,
        rejection_timestamp_ns: int | None,
    ) -> None:
        if not isinstance(intent, OrderIntent) or not isinstance(intent.order_id, str) or not intent.order_id:
            return
        if intent.order_id in self._outcomes:
            return
        self._outcomes[intent.order_id] = OrderOutcome(
            order_id=intent.order_id,
            instrument_id=intent.instrument_id if isinstance(intent.instrument_id, str) else "",
            status="rejected",
            filled_quantity=Decimal(0),
            remaining_quantity=intent.quantity if isinstance(intent.quantity, Decimal) else Decimal(0),
            reason=reason,
            decision_ordinal=decision_ordinal,
            decision_timestamp_ns=decision_timestamp_ns,
            last_update_timestamp_ns=rejection_timestamp_ns,
            completion_timestamp_ns=rejection_timestamp_ns,
            policy_version=self.policy.version,
        )

    def _accept(
        self,
        intent: OrderIntent,
        event: MarketEvent,
        decision_timestamp_ns: int | None,
        accepted_timestamp_ns: int | None,
    ) -> _WorkingOrder | None:
        if intent.order_id in self._outcomes or intent.order_id in self._working:
            self.reject(
                intent,
                "order_id is already present in matcher state",
                event.arrival_ordinal,
                decision_timestamp_ns,
                accepted_timestamp_ns,
            )
            return None
        if decision_timestamp_ns is None:
            decision_timestamp_ns = _event_timestamp_ns(event)
        arrival_timestamp_ns = (
            None if decision_timestamp_ns is None
            else decision_timestamp_ns + self.policy.latency.decision_to_arrival_ns
        )
        instrument = self._instruments[intent.instrument_id]
        commission_currency = self.policy.commission.currency or self._instrument_currency(instrument)
        working = _WorkingOrder(
            intent=intent,
            remaining=intent.quantity,
            decision_ordinal=event.arrival_ordinal,
            decision_timestamp_ns=decision_timestamp_ns,
            accepted_timestamp_ns=accepted_timestamp_ns,
            arrival_timestamp_ns=arrival_timestamp_ns,
            last_update_timestamp_ns=accepted_timestamp_ns,
            commission_currency=commission_currency,
        )
        self._working[intent.order_id] = working
        self._outcomes[intent.order_id] = self._outcome(working, "accepted_pending_arrival", None)
        return working

    def _cancel(
        self, intent: CancelOrderIntent, timestamp_ns: int | None, ordinal: int
    ) -> None:
        if intent.request_id in self._seen_cancel_ids:
            self._cancellations.append(CancellationOutcome(
                intent.request_id, intent.order_id, "rejected", timestamp_ns,
                "cancel request_id was already used", ordinal,
            ))
            return
        self._seen_cancel_ids.add(intent.request_id)
        working = self._working.pop(intent.order_id, None)
        if working is None:
            self._cancellations.append(CancellationOutcome(
                intent.request_id, intent.order_id, "rejected", timestamp_ns,
                "order is unknown or already terminal", ordinal,
            ))
            return
        working.last_update_timestamp_ns = timestamp_ns
        self._outcomes[intent.order_id] = self._outcome(working, "cancelled", "cancelled by strategy")
        self._cancellations.append(CancellationOutcome(
            intent.request_id, intent.order_id, "accepted", timestamp_ns, None, ordinal
        ))

    def reject_cancel(
        self,
        intent: CancelOrderIntent,
        reason: str,
        timestamp_ns: int | None,
        ordinal: int | None = None,
    ) -> None:
        if isinstance(intent.request_id, str) and intent.request_id in self._seen_cancel_ids:
            reason = f"{reason}; cancel request_id was already used"
        elif isinstance(intent.request_id, str):
            self._seen_cancel_ids.add(intent.request_id)
        self._cancellations.append(CancellationOutcome(
            intent.request_id if isinstance(intent.request_id, str) else "",
            intent.order_id if isinstance(intent.order_id, str) else "",
            "rejected",
            timestamp_ns,
            reason,
            ordinal,
        ))

    def _match_working(
        self,
        event: MarketEvent,
        book: TopOfBook,
        event_time_ns: int | None,
    ) -> tuple[Fill, ...]:
        if not self._working:
            return ()
        fills: list[Fill] = []
        for order_id, working in tuple(self._working.items()):
            if working.intent.instrument_id != event.instrument_id:
                continue
            if not self._arrived(working, event_time_ns):
                self._outcomes[order_id] = self._outcome(working, "accepted_pending_arrival", None)
                continue
            fills.extend(self._try_fill(working, event, book, event_time_ns))
            self._update_after_match(working, event_time_ns)
        return tuple(fills)

    def _try_fill(
        self,
        working: _WorkingOrder,
        event: MarketEvent,
        book_view: TopOfBook,
        event_time_ns: int | None,
    ) -> list[Fill]:
        instrument_id = working.intent.instrument_id
        book = self._books.get(instrument_id)
        if book is None:
            return []
        intent = working.intent
        if intent.side == "buy":
            reference_price = book.ask_price
            available = book.ask_available
            displayed_side = "ask"
        else:
            reference_price = book.bid_price
            available = book.bid_available
            displayed_side = "bid"
        instrument = self._instruments[instrument_id]
        tick = instrument.get("price_tick")
        if not isinstance(tick, Decimal) or tick <= 0:
            raise OrderValidationError(f"instrument {instrument_id!r} has an invalid price_tick.")
        slippage = tick * self.policy.slippage.ticks
        price = reference_price + slippage if intent.side == "buy" else reference_price - slippage
        marketable = intent.order_type == "market" or (
            intent.limit_price is not None and (
                intent.limit_price >= reference_price if intent.side == "buy"
                else intent.limit_price <= reference_price
            )
        )
        if intent.order_type == "limit" and intent.limit_price is not None:
            marketable = marketable and (
                price <= intent.limit_price if intent.side == "buy" else price >= intent.limit_price
            )
        if not marketable or available <= 0:
            return []

        increment = instrument.get("quantity_increment")
        if not isinstance(increment, Decimal) or increment <= 0:
            raise OrderValidationError(f"instrument {instrument_id!r} has an invalid quantity_increment.")
        raw_quantity = min(working.remaining, available)
        quantity = (raw_quantity // increment) * increment
        if quantity <= 0:
            return []
        if displayed_side == "ask":
            book.ask_available -= quantity
        else:
            book.bid_available -= quantity
        working.remaining -= quantity
        working.filled += quantity
        working.last_update_timestamp_ns = event_time_ns
        fill_number = self._fill_numbers.get(intent.order_id, 0) + 1
        self._fill_numbers[intent.order_id] = fill_number
        currency = self._instrument_currency(instrument)
        commission_currency = self.policy.commission.currency or currency
        commission = self._commission_for(quantity, price, instrument)
        working.commission_total += commission
        return [
            Fill(
                fill_id=f"{intent.order_id}:{fill_number}",
                order_id=intent.order_id,
                instrument_id=instrument_id,
                side=intent.side,
                quantity=quantity,
                price=price,
                currency=currency,
                arrival_ordinal=event.arrival_ordinal,
                decision_ordinal=working.decision_ordinal,
                decision_timestamp_ns=working.decision_timestamp_ns,
                order_arrival_timestamp_ns=working.arrival_timestamp_ns,
                match_timestamp_ns=event_time_ns,
                bid_at_match=book_view.bid_price,
                ask_at_match=book_view.ask_price,
                spread_at_match=book_view.ask_price - book_view.bid_price,
                slippage=slippage,
                commission=commission,
                commission_currency=commission_currency,
                liquidity_source=f"displayed_l1_{displayed_side}",
                matching_rule=f"{self.policy.queue_model}+{self.policy.spread_model}",
                policy_version=self.policy.version,
                match_provider_id=event.provider_id,
                match_channel=event.channel,
                match_sequence=event.sequence,
                bid_quantity_at_match=book_view.bid_quantity,
                ask_quantity_at_match=book_view.ask_quantity,
                available_quantity_before_fill=available,
            )
        ]

    def _commission_for(
        self, quantity: Decimal, price: Decimal, instrument: Mapping[str, Any]
    ) -> Decimal:
        model = self.policy.commission
        multiplier = instrument.get("contract_multiplier")
        if multiplier is None:
            multiplier = instrument.get("future_contract", {}).get("contract_multiplier", Decimal(1))
        if not isinstance(multiplier, Decimal) or multiplier <= 0:
            multiplier = Decimal(1)
        amount = quantity * model.per_unit + abs(quantity * price * multiplier) * model.notional_rate
        if model.currency_precision is not None:
            quantum = Decimal(1).scaleb(-model.currency_precision)
            amount = amount.quantize(quantum, rounding=_ROUNDING_MODES[model.rounding])
        return amount

    @staticmethod
    def _instrument_currency(instrument: Mapping[str, Any]) -> str:
        if instrument["asset_class"] == "future":
            return str(instrument["future_contract"]["settlement_currency"])
        return str(instrument["quote_currency"])

    @staticmethod
    def _arrived(working: _WorkingOrder, timestamp_ns: int | None) -> bool:
        return working.arrival_timestamp_ns is None or (
            timestamp_ns is not None and timestamp_ns >= working.arrival_timestamp_ns
        )

    def _update_after_match(self, working: _WorkingOrder, timestamp_ns: int | None) -> None:
        if working.remaining == 0:
            status, reason = "filled", None
            working.last_update_timestamp_ns = timestamp_ns
            self._working.pop(working.intent.order_id, None)
        elif working.intent.order_type == "market":
            status = "partially_filled_cancelled" if working.filled else "cancelled"
            reason = "no additional displayed L1 liquidity" if working.filled else "no displayed L1 liquidity"
            working.last_update_timestamp_ns = timestamp_ns
            self._working.pop(working.intent.order_id, None)
        else:
            status = "partially_filled_working" if working.filled else "pending"
            reason = None
        self._outcomes[working.intent.order_id] = self._outcome(working, status, reason)

    def _outcome(self, working: _WorkingOrder, status: str, reason: str | None) -> OrderOutcome:
        return OrderOutcome(
            order_id=working.intent.order_id,
            instrument_id=working.intent.instrument_id,
            status=status,
            filled_quantity=working.filled,
            remaining_quantity=working.remaining,
            reason=reason,
            decision_ordinal=working.decision_ordinal,
            decision_timestamp_ns=working.decision_timestamp_ns,
            accepted_timestamp_ns=working.accepted_timestamp_ns,
            arrival_timestamp_ns=working.arrival_timestamp_ns,
            last_update_timestamp_ns=working.last_update_timestamp_ns,
            completion_timestamp_ns=(
                working.last_update_timestamp_ns
                if status in {"filled", "cancelled", "partially_filled_cancelled", "rejected"}
                else None
            ),
            commission_total=working.commission_total,
            commission_currency=working.commission_currency,
            policy_version=self.policy.version,
        )

    def finish(self, timestamp_ns: int | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        close_time = self._last_virtual_timestamp_ns if timestamp_ns is None else timestamp_ns
        for order_id, working in tuple(self._working.items()):
            reason = (
                "session_closed_before_arrival"
                if close_time is None or not self._arrived(working, close_time)
                else "session_closed_unfilled"
            )
            working.last_update_timestamp_ns = close_time
            self._outcomes[order_id] = self._outcome(working, "cancelled", reason)
        self._working.clear()

    @property
    def cancellations(self) -> tuple[CancellationOutcome, ...]:
        return tuple(self._cancellations)

    def snapshot(self) -> tuple[OrderOutcome, ...]:
        for order_id, working in self._working.items():
            status = (
                "accepted_pending_arrival"
                if not self._arrived(working, self._last_virtual_timestamp_ns)
                else "partially_filled_working" if working.filled > 0 else "pending"
            )
            self._outcomes[order_id] = self._outcome(working, status, None)
        return tuple(self._outcomes.values())

class Portfolio:
    """Append-only accounting ledger with deterministic state reconstruction."""

    def __init__(self, catalog: Catalog, policy: PortfolioPolicy | None = None):
        self._instruments = {str(item.get("id")): item for item in catalog.instruments}
        self.policy = policy or PortfolioPolicy()
        for limit in self.policy.exposure_limits:
            if limit.scope == "instrument" and limit.key not in self._instruments:
                raise ValueError(f"Exposure limit references unknown instrument {limit.key!r}.")
        for rule in self.policy.margin_rules:
            if rule.instrument_id not in self._instruments:
                raise ValueError(f"Margin rule references unknown instrument {rule.instrument_id!r}.")
        self._ledger: list[LedgerEntry] = []
        self._entry_ids: set[str] = set()
        self._corporate_action_keys: set[tuple] = set()
        self._fill_ids: set[str] = set()
        self._fill_entries: dict[str, Fill] = {}
        self._commissions_by_fill: dict[str, Decimal] = {}
        self._entry_sequence = 0
        self._cash: dict[str, Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._average_cost: dict[str, Decimal] = {}
        self._marks: dict[str, ValuationMark] = {}
        self._realized_pnl: dict[str, Decimal] = {}
        self._commissions_paid: dict[str, Decimal] = {}
        self._dividends: dict[str, Decimal] = {}
        self._financing: dict[str, Decimal] = {}
        self._conversion_rates: list[ConversionRate] = []
        self._version = 0
        self._as_of_ordinal: int | None = None
        self._as_of_timestamp_ns: int | None = None

    @classmethod
    def from_ledger(
        cls,
        catalog: Catalog,
        entries: Iterable[LedgerEntry],
        policy: PortfolioPolicy | None = None,
    ) -> "Portfolio":
        portfolio = cls(catalog, policy)
        for entry in entries:
            portfolio.apply_ledger_entry(entry)
        for fill_entry_id, fill in portfolio._fill_entries.items():
            expected = fill.commission
            actual = portfolio._commissions_by_fill.get(fill_entry_id, Decimal(0))
            if actual != expected:
                raise ValueError(
                    f"Ledger commission for fill {fill.fill_id!r} is {actual}; expected {expected}."
                )
        return portfolio

    @property
    def ledger(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._ledger)

    def ledger_jsonl(self) -> str:
        """Serialize the append-only facts as canonical JSONL outside the hot path."""
        return ledger_to_jsonl(self._ledger)

    @classmethod
    def from_ledger_jsonl(
        cls, catalog: Catalog, content: str, policy: PortfolioPolicy | None = None
    ) -> "Portfolio":
        if not isinstance(content, str):
            raise TypeError("ledger JSONL content must be a string.")
        entries: list[LedgerEntry] = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, parse_float=lambda raw: (_ for _ in ()).throw(ValueError("JSON numbers are not allowed for financial values.")))
                entries.append(LedgerEntry.from_mapping(value))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid ledger JSONL at line {line_number}: {exc}") from exc
        return cls.from_ledger(catalog, entries, policy)

    def _next_id(self, kind: str) -> str:
        return f"{kind}:{self._entry_sequence + 1:012d}"

    def apply(self, fill: Fill) -> LedgerEntry:
        """Record a fill fact and a separate commission event, if any."""

        instrument = self._instruments.get(fill.instrument_id)
        if instrument is None:
            raise ValueError(f"Unknown instrument {fill.instrument_id!r} in fill.")
        if fill.side not in {"buy", "sell"} or fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("Fill side, quantity, or price is invalid for accounting.")
        signed_quantity = fill.quantity if fill.side == "buy" else -fill.quantity
        flows: list[CashFlow] = []
        asset_class = instrument["asset_class"]
        if asset_class == "fx":
            base = instrument["fx_convention"]["base_currency"]
            flows.append(CashFlow(base, signed_quantity, "fx_base_trade"))
        if asset_class != "future":
            quote = instrument["quote_currency"]
            flows.append(CashFlow(quote, -(signed_quantity * fill.price), "trade_notional"))
        realized = self._fill_realized(fill, instrument)
        if asset_class == "future" and realized:
            flows.append(CashFlow(self._pnl_currency(instrument), realized, "futures_variation_on_close"))
        entry = LedgerEntry(
            entry_id=self._next_id("fill"),
            kind="fill",
            timestamp_ns=fill.match_timestamp_ns,
            arrival_ordinal=fill.arrival_ordinal,
            instrument_id=fill.instrument_id,
            currency=fill.currency,
            quantity=fill.quantity,
            price=fill.price,
            cash_flows=tuple(flows),
            realized_pnl=(
                CashFlow(self._pnl_currency(instrument), realized, "fill_realized_pnl")
                if realized else None
            ),
            fill=fill,
            source=fill.liquidity_source,
            reference_id=fill.order_id,
        )
        self.apply_ledger_entry(entry)
        if fill.commission:
            currency = fill.commission_currency or fill.currency
            commission_entry = LedgerEntry(
                entry_id=self._next_id("commission"),
                kind="commission",
                timestamp_ns=fill.match_timestamp_ns,
                arrival_ordinal=fill.arrival_ordinal,
                instrument_id=fill.instrument_id,
                currency=currency,
                amount=fill.commission,
                cash_flows=(CashFlow(currency, -fill.commission, "commission"),),
                reference_id=entry.entry_id,
                source=fill.policy_version,
            )
            self.apply_ledger_entry(commission_entry)
        return entry

    def apply_ledger_entry(self, entry: LedgerEntry) -> None:
        """Apply one immutable event; duplicate event IDs are rejected."""

        if not isinstance(entry, LedgerEntry):
            raise TypeError("entry must be a LedgerEntry.")
        if entry.entry_id in self._entry_ids:
            raise ValueError(f"Duplicate ledger entry_id {entry.entry_id!r}.")
        action_key = None
        if entry.kind in {"split", "dividend"}:
            if self.policy.equity_price_basis != "raw":
                raise ValueError("Adjusted equity prices cannot be combined with cash dividend or position split events")
            if entry.timestamp_ns is None:
                raise ValueError("Corporate actions require an explicit effective timestamp")
            action_key = (entry.kind, entry.instrument_id, entry.timestamp_ns)
            if action_key in self._corporate_action_keys:
                raise ValueError("Duplicate corporate action at the same instrument/effective timestamp")
        if entry.kind == "fill":
            if entry.fill is None or entry.instrument_id not in self._instruments:
                raise ValueError("Fill ledger entries require a known instrument and immutable Fill.")
            if entry.fill.fill_id in self._fill_ids:
                raise ValueError(f"Duplicate immutable fill_id {entry.fill.fill_id!r}.")
            self._validate_fill_entry(entry)
            self._apply_fill_fact(entry)
            self._fill_ids.add(entry.fill.fill_id)
            self._fill_entries[entry.entry_id] = entry.fill
        elif entry.kind == "commission":
            if entry.currency is None or entry.amount is None or entry.amount <= 0:
                raise ValueError("Commission entries require a positive amount and currency.")
            if entry.reference_id is None or entry.reference_id not in self._fill_entries:
                raise ValueError("Commission entries must reference a prior fill fact.")
            if entry.reference_id in self._commissions_by_fill:
                raise ValueError("A fill may have only one aggregate commission entry.")
            referenced_fill = self._fill_entries[entry.reference_id]
            expected_currency = referenced_fill.commission_currency or referenced_fill.currency
            if entry.amount != referenced_fill.commission or entry.currency != expected_currency:
                raise ValueError("Commission entry does not reconcile to its referenced fill.")
            if entry.cash_flows != (CashFlow(entry.currency, -entry.amount, "commission"),):
                raise ValueError("Commission cash flow must debit the exact commission amount and currency.")
            self._commissions_paid[entry.currency] = self._commissions_paid.get(entry.currency, Decimal(0)) + entry.amount
            self._commissions_by_fill[entry.reference_id] = entry.amount
        elif entry.kind == "cash_adjustment":
            if entry.currency is None or entry.amount is None:
                raise ValueError("Cash adjustments require an explicit amount and currency.")
            if entry.cash_flows != (CashFlow(entry.currency, entry.amount, "cash_adjustment"),):
                raise ValueError("Cash adjustment flow must equal its recorded amount and currency.")
        elif entry.kind == "split":
            if entry.cash_flows:
                raise ValueError("Split entries cannot change cash balances.")
            if entry.instrument_id not in self._instruments or entry.amount is None or entry.amount <= 0:
                raise ValueError("Split entries require a known instrument and positive split ratio.")
            instrument = self._instruments[entry.instrument_id]
            if instrument["asset_class"] != "equity":
                raise ValueError("Split entries are supported only for equities.")
            current = self._positions.get(entry.instrument_id, Decimal(0))
            if entry.quantity != current:
                raise ValueError("Split entry must record the position quantity immediately before the split.")
            if current:
                self._positions[entry.instrument_id] = current * entry.amount
                if entry.instrument_id in self._average_cost:
                    self._average_cost[entry.instrument_id] /= entry.amount
                self._marks.pop(entry.instrument_id, None)
        elif entry.kind in {"dividend", "funding", "futures_settlement"}:
            if entry.instrument_id not in self._instruments:
                raise ValueError(f"{entry.kind} entry requires a known instrument.")
            instrument = self._instruments[entry.instrument_id]
            if entry.kind == "dividend" and instrument["asset_class"] != "equity":
                raise ValueError("Dividend entries are supported only for equities.")
            if entry.kind == "funding" and instrument["asset_class"] != "fx":
                raise ValueError("Funding entries are supported only for FX instruments.")
            if entry.kind == "futures_settlement" and instrument["asset_class"] != "future":
                raise ValueError("Futures settlement entries require a futures contract.")
            if entry.currency is None or entry.amount is None:
                raise ValueError(f"{entry.kind} entries require amount and currency.")
            if entry.kind == "futures_settlement" and entry.currency != self._pnl_currency(instrument):
                raise ValueError("Futures settlement currency must match the contract settlement currency.")
            expected_category = {
                "dividend": "dividend",
                "funding": "fx_financing",
                "futures_settlement": "futures_variation_margin",
            }[entry.kind]
            if entry.cash_flows != (CashFlow(entry.currency, entry.amount, expected_category),):
                raise ValueError(f"{entry.kind} cash flow must equal its recorded amount and currency.")
            if entry.kind == "dividend":
                current = self._positions.get(entry.instrument_id, Decimal(0))
                if entry.quantity != current or entry.price is None:
                    raise ValueError("Dividend entry must record the current position and cash per unit.")
                expected_amount = current * entry.price * self._multiplier(instrument)
                if entry.amount != expected_amount:
                    raise ValueError("Dividend amount does not reconcile to the recorded position and per-unit amount.")
                self._dividends[entry.currency] = self._dividends.get(entry.currency, Decimal(0)) + entry.amount
            elif entry.kind == "funding":
                self._financing[entry.currency] = self._financing.get(entry.currency, Decimal(0)) + entry.amount
            else:
                current = self._positions.get(entry.instrument_id, Decimal(0))
                if entry.quantity != current or entry.price is None or entry.price <= 0:
                    raise ValueError("Futures settlement must record the current position and a positive settlement price.")
                basis = self._average_cost.get(entry.instrument_id, entry.price)
                expected_amount = current * (entry.price - basis) * self._multiplier(instrument)
                if entry.amount != expected_amount:
                    raise ValueError("Futures variation does not reconcile to position, basis, settlement, and multiplier.")
                self._realized_pnl[entry.currency] = self._realized_pnl.get(entry.currency, Decimal(0)) + entry.amount
                if entry.instrument_id is not None and entry.price is not None:
                    if self._positions.get(entry.instrument_id, Decimal(0)):
                        self._average_cost[entry.instrument_id] = entry.price
                        if entry.kind == "futures_settlement":
                            self._marks[entry.instrument_id] = ValuationMark(
                                instrument_id=entry.instrument_id,
                                price=entry.price,
                                side="settlement",
                                bid_price=entry.price,
                                ask_price=entry.price,
                                timestamp_ns=entry.timestamp_ns,
                                arrival_ordinal=entry.arrival_ordinal or 0,
                                source=entry.source or "contract_settlement",
                            )
        elif entry.kind == "conversion_rate":
            if entry.cash_flows:
                raise ValueError("Conversion-rate entries cannot change cash balances.")
            if entry.conversion_rate is None:
                raise ValueError("Conversion-rate entries require a ConversionRate.")
            if entry.timestamp_ns != entry.conversion_rate.timestamp_ns or entry.currency != entry.conversion_rate.to_currency:
                raise ValueError("Conversion-rate ledger metadata must match the captured rate.")
            self._conversion_rates.append(entry.conversion_rate)
        elif entry.kind == "mark":
            if entry.cash_flows:
                raise ValueError("Mark entries cannot change cash balances.")
            if (
                entry.instrument_id is None or entry.instrument_id not in self._instruments
                or entry.bid_price is None or entry.ask_price is None
                or entry.arrival_ordinal is None
            ):
                raise ValueError("Mark entries require a known instrument and both quote sides.")
            if entry.bid_price <= 0 or entry.ask_price <= 0 or entry.bid_price > entry.ask_price:
                raise ValueError("Mark entries require a positive, non-crossed L1 quote.")
            quantity = self._positions.get(entry.instrument_id, Decimal(0))
            if quantity:
                side = "bid" if quantity > 0 else "ask"
                price = entry.bid_price if quantity > 0 else entry.ask_price
                self._marks[entry.instrument_id] = ValuationMark(
                    instrument_id=entry.instrument_id,
                    price=price,
                    side=side,
                    bid_price=entry.bid_price,
                    ask_price=entry.ask_price,
                    timestamp_ns=entry.timestamp_ns,
                    arrival_ordinal=entry.arrival_ordinal or 0,
                    provider_id=entry.provider_id,
                    channel=entry.channel,
                    sequence=entry.sequence,
                    source=entry.source or "observed_l1",
                )
        elif entry.kind == "checkpoint":
            if entry.cash_flows:
                raise ValueError("Checkpoint entries cannot change cash balances.")
            if entry.portfolio_version is None or entry.arrival_ordinal is None:
                raise ValueError("Checkpoint entries require portfolio_version and arrival_ordinal.")
            self._as_of_ordinal = entry.arrival_ordinal
            self._as_of_timestamp_ns = entry.timestamp_ns
            self._version = entry.portfolio_version
        else:
            raise ValueError(f"Unsupported ledger entry kind {entry.kind!r}.")

        for flow in entry.cash_flows:
            self._cash[flow.currency] = self._cash.get(flow.currency, Decimal(0)) + flow.amount
        if entry.kind != "checkpoint":
            if entry.timestamp_ns is not None:
                self._as_of_timestamp_ns = entry.timestamp_ns
            if entry.arrival_ordinal is not None:
                self._as_of_ordinal = entry.arrival_ordinal
            self._version += 1
        if action_key is not None:
            self._corporate_action_keys.add(action_key)
        self._entry_ids.add(entry.entry_id)
        self._entry_sequence += 1
        self._ledger.append(entry)

    def _validate_fill_entry(self, entry: LedgerEntry) -> None:
        fill = entry.fill
        assert fill is not None
        if fill.instrument_id != entry.instrument_id or fill.side not in {"buy", "sell"}:
            raise ValueError("Fill ledger identity and side must match the embedded fill fact.")
        if entry.currency != fill.currency:
            raise ValueError("Fill ledger currency must match the embedded fill fact.")
        if fill.quantity <= 0 or fill.price <= 0 or entry.quantity != fill.quantity or entry.price != fill.price:
            raise ValueError("Fill ledger quantity and price must match positive embedded fill values.")
        instrument = self._instruments[fill.instrument_id]
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        expected: list[CashFlow] = []
        if instrument["asset_class"] == "fx":
            expected.append(CashFlow(instrument["fx_convention"]["base_currency"], signed, "fx_base_trade"))
        if instrument["asset_class"] != "future":
            expected.append(CashFlow(instrument["quote_currency"], -(signed * fill.price), "trade_notional"))
        realized = self._fill_realized(fill, instrument)
        if instrument["asset_class"] == "future" and realized:
            expected.append(CashFlow(self._pnl_currency(instrument), realized, "futures_variation_on_close"))
        if entry.cash_flows != tuple(expected):
            raise ValueError("Fill cash flows do not reconcile to the catalog convention and fill fact.")
        expected_realized = None if realized == 0 else CashFlow(self._pnl_currency(instrument), realized, "fill_realized_pnl")
        if entry.realized_pnl != expected_realized:
            raise ValueError("Recorded realized P&L does not reconcile to the position and fill fact.")

    def _apply_fill_fact(self, entry: LedgerEntry) -> None:
        fill = entry.fill
        assert fill is not None
        instrument = self._instruments[fill.instrument_id]
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        old = self._positions.get(fill.instrument_id, Decimal(0))
        old_cost = self._average_cost.get(fill.instrument_id, Decimal(0))
        new = old + signed
        multiplier = self._multiplier(instrument)
        if old == 0 or (old > 0) == (signed > 0):
            total = abs(old) + abs(signed)
            self._average_cost[fill.instrument_id] = (abs(old) * old_cost + abs(signed) * fill.price) / total
        else:
            closed = min(abs(old), abs(signed))
            direction = Decimal(1) if old > 0 else Decimal(-1)
            currency = self._pnl_currency(instrument)
            realized = closed * (fill.price - old_cost) * direction * multiplier
            self._realized_pnl[currency] = self._realized_pnl.get(currency, Decimal(0)) + realized
            if new == 0:
                self._average_cost.pop(fill.instrument_id, None)
            elif (new > 0) != (old > 0):
                self._average_cost[fill.instrument_id] = fill.price
        self._positions[fill.instrument_id] = new
        if not new:
            self._marks.pop(fill.instrument_id, None)
        previous_mark = self._marks.get(fill.instrument_id)
        if new and previous_mark is not None:
            side = "bid" if new > 0 else "ask"
            self._marks[fill.instrument_id] = ValuationMark(
                instrument_id=fill.instrument_id,
                price=previous_mark.bid_price if new > 0 else previous_mark.ask_price,
                side=side,
                bid_price=previous_mark.bid_price,
                ask_price=previous_mark.ask_price,
                timestamp_ns=previous_mark.timestamp_ns,
                arrival_ordinal=previous_mark.arrival_ordinal,
                provider_id=previous_mark.provider_id,
                channel=previous_mark.channel,
                sequence=previous_mark.sequence,
                source=previous_mark.source,
            )

    def _fill_realized(self, fill: Fill, instrument: Mapping[str, Any]) -> Decimal:
        old = self._positions.get(fill.instrument_id, Decimal(0))
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        if old == 0 or (old > 0) == (signed > 0):
            return Decimal(0)
        closed = min(abs(old), abs(signed))
        direction = Decimal(1) if old > 0 else Decimal(-1)
        return closed * (fill.price - self._average_cost.get(fill.instrument_id, Decimal(0))) * direction * self._multiplier(instrument)

    def record_split(
        self, event_id: str, instrument_id: str, ratio: Decimal, timestamp_ns: int,
        source: str, arrival_ordinal: int | None = None,
    ) -> LedgerEntry:
        return self._record_action(LedgerEntry(
            entry_id=event_id, kind="split", timestamp_ns=timestamp_ns,
            arrival_ordinal=arrival_ordinal, instrument_id=instrument_id,
            amount=ratio, quantity=self._positions.get(instrument_id, Decimal(0)), source=source,
        ))

    def record_dividend(
        self, event_id: str, instrument_id: str, cash_per_unit: Decimal, currency: str,
        timestamp_ns: int, source: str, arrival_ordinal: int | None = None,
    ) -> LedgerEntry:
        instrument = self._require_asset(instrument_id, "equity")
        quantity = self._positions.get(instrument_id, Decimal(0))
        amount = quantity * cash_per_unit * self._multiplier(instrument)
        entry = LedgerEntry(
            entry_id=event_id, kind="dividend", timestamp_ns=timestamp_ns,
            arrival_ordinal=arrival_ordinal, instrument_id=instrument_id,
            currency=currency, amount=amount, quantity=quantity, price=cash_per_unit,
            cash_flows=(CashFlow(currency, amount, "dividend"),), source=source,
        )
        return self._record_action(entry)

    def record_funding(
        self, event_id: str, instrument_id: str, currency: str, amount: Decimal,
        timestamp_ns: int, source: str, arrival_ordinal: int | None = None,
    ) -> LedgerEntry:
        self._require_asset(instrument_id, "fx")
        entry = LedgerEntry(
            entry_id=event_id, kind="funding", timestamp_ns=timestamp_ns,
            arrival_ordinal=arrival_ordinal, instrument_id=instrument_id,
            currency=currency, amount=amount,
            cash_flows=(CashFlow(currency, amount, "fx_financing"),), source=source,
        )
        return self._record_action(entry)

    def record_cash_adjustment(
        self, event_id: str, currency: str, amount: Decimal, timestamp_ns: int,
        source: str, arrival_ordinal: int | None = None,
    ) -> LedgerEntry:
        """Record an explicit capital contribution or withdrawal, outside P&L."""
        entry = LedgerEntry(
            entry_id=event_id, kind="cash_adjustment", timestamp_ns=timestamp_ns,
            arrival_ordinal=arrival_ordinal, currency=currency, amount=amount,
            cash_flows=(CashFlow(currency, amount, "cash_adjustment"),), source=source,
        )
        return self._record_action(entry)

    def record_futures_settlement(
        self, event_id: str, instrument_id: str, settlement_price: Decimal,
        timestamp_ns: int, source: str, arrival_ordinal: int | None = None,
    ) -> LedgerEntry:
        instrument = self._require_asset(instrument_id, "future")
        quantity = self._positions.get(instrument_id, Decimal(0))
        basis = self._average_cost.get(instrument_id, settlement_price)
        amount = quantity * (settlement_price - basis) * self._multiplier(instrument)
        currency = self._pnl_currency(instrument)
        entry = LedgerEntry(
            entry_id=event_id, kind="futures_settlement", timestamp_ns=timestamp_ns,
            arrival_ordinal=arrival_ordinal, instrument_id=instrument_id,
            currency=currency, amount=amount, quantity=quantity, price=settlement_price,
            cash_flows=(CashFlow(currency, amount, "futures_variation_margin"),), source=source,
        )
        return self._record_action(entry)

    def update_conversion_rate(self, rate: ConversionRate, *, entry_id: str | None = None) -> LedgerEntry:
        if not isinstance(rate, ConversionRate):
            raise TypeError("rate must be a ConversionRate.")
        return self._record_action(LedgerEntry(
            entry_id=entry_id or self._next_id("conversion_rate"),
            kind="conversion_rate", timestamp_ns=rate.timestamp_ns,
            arrival_ordinal=rate.arrival_ordinal, currency=rate.to_currency,
            conversion_rate=rate, source=rate.source,
        ))

    def _record_action(self, entry: LedgerEntry) -> LedgerEntry:
        self.apply_ledger_entry(entry)
        return entry

    def _require_asset(self, instrument_id: str, asset_class: str) -> Mapping[str, Any]:
        instrument = self._instruments.get(instrument_id)
        if instrument is None or instrument["asset_class"] != asset_class:
            raise ValueError(f"Instrument {instrument_id!r} must be a known {asset_class}.")
        return instrument

    def mark_to_market(self, book: TopOfBook, event: MarketEvent | None = None) -> LedgerEntry | None:
        """Mark longs to bid and shorts to ask, retaining the observed quote source."""

        if not self._positions.get(book.instrument_id, Decimal(0)):
            return None
        return self._record_action(LedgerEntry(
            entry_id=self._next_id("mark"), kind="mark",
            timestamp_ns=None if event is None else _event_timestamp_ns(event),
            arrival_ordinal=book.arrival_ordinal,
            instrument_id=book.instrument_id,
            bid_price=book.bid_price, ask_price=book.ask_price,
            provider_id=None if event is None else event.provider_id,
            channel=None if event is None else event.channel,
            sequence=None if event is None else event.sequence,
            source="observed_l1",
        ))

    def complete_event(self, arrival_ordinal: int, timestamp_ns: int | None = None) -> None:
        self._as_of_ordinal = arrival_ordinal
        self._as_of_timestamp_ns = timestamp_ns
        self._version += 1
        self.apply_ledger_entry(LedgerEntry(
            entry_id=self._next_id("checkpoint"), kind="checkpoint",
            timestamp_ns=timestamp_ns, arrival_ordinal=arrival_ordinal,
            portfolio_version=self._version,
        ))

    def _pnl_currency(self, instrument: Mapping[str, Any]) -> str:
        if instrument["asset_class"] == "future":
            contract = instrument.get("future_contract", {})
            return str(contract.get("settlement_currency", instrument["quote_currency"]))
        return str(instrument["quote_currency"])

    @staticmethod
    def _multiplier(instrument: Mapping[str, Any]) -> Decimal:
        value = instrument.get("contract_multiplier")
        if value is None:
            value = instrument.get("future_contract", {}).get("contract_multiplier", Decimal(1))
        return value if isinstance(value, Decimal) else Decimal(1)

    def _current_conversion(self, currency: str) -> ConversionRate | None:
        target = self.policy.reporting_currency
        if target is None:
            return None
        if currency == target:
            timestamp = self._as_of_timestamp_ns or 0
            return ConversionRate(currency, target, Decimal(1), timestamp, "identity")
        eligible = [
            rate for rate in self._conversion_rates
            if rate.from_currency == currency
            and rate.to_currency == target
            and (self._as_of_timestamp_ns is None or rate.timestamp_ns <= self._as_of_timestamp_ns)
            and (self._as_of_ordinal is None or rate.arrival_ordinal is None or rate.arrival_ordinal <= self._as_of_ordinal)
        ]
        if not eligible:
            return None
        rate = max(eligible, key=lambda item: (item.timestamp_ns, -1 if item.arrival_ordinal is None else item.arrival_ordinal))
        if self.policy.max_conversion_age_ns is not None and self._as_of_timestamp_ns is not None:
            if self._as_of_timestamp_ns - rate.timestamp_ns > self.policy.max_conversion_age_ns:
                return None
        return rate

    def _convert_map(
        self, values: Mapping[str, Decimal], used: dict[str, ConversionRate], missing: set[str]
    ) -> Decimal | None:
        if self.policy.reporting_currency is None:
            return None
        total = Decimal(0)
        for currency, amount in values.items():
            rate = self._current_conversion(currency)
            if rate is None:
                if amount != 0:
                    missing.add(currency)
                continue
            used[currency] = rate
            total += amount * rate.rate
        return None if missing else total

    def _risk_snapshots(self, positions: Mapping[str, Decimal]) -> tuple[ExposureSnapshot, MarginSnapshot]:
        exposures: dict[str, InstrumentExposure] = {}
        gross: dict[str, Decimal] = {}
        unpriced: list[str] = []
        margin_initial: dict[str, Decimal] = {}
        margin_maintenance: dict[str, Decimal] = {}
        unconfigured_margin: list[str] = []
        for instrument_id, quantity in positions.items():
            instrument = self._instruments[instrument_id]
            mark = self._marks.get(instrument_id)
            currency = self._pnl_currency(instrument)
            base_currency = None
            base_exposure = None
            if instrument["asset_class"] == "fx":
                base_currency = instrument["fx_convention"]["base_currency"]
                base_exposure = quantity
                gross[base_currency] = gross.get(base_currency, Decimal(0)) + abs(quantity)
            if mark is None:
                unpriced.append(instrument_id)
                exposures[instrument_id] = InstrumentExposure(
                    instrument_id=instrument_id, quantity=quantity,
                    mark_price=None, signed_notional=None,
                    notional_currency=currency, base_currency_exposure=base_exposure,
                    base_currency=base_currency, mark=None,
                )
            else:
                multiplier = self._multiplier(instrument)
                signed_notional = quantity * mark.price * multiplier
                exposures[instrument_id] = InstrumentExposure(
                    instrument_id=instrument_id, quantity=quantity,
                    mark_price=mark.price, signed_notional=signed_notional,
                    notional_currency=currency, base_currency_exposure=base_exposure,
                    base_currency=base_currency, mark=mark,
                )
                gross[currency] = gross.get(currency, Decimal(0)) + abs(signed_notional)
            rule = next((item for item in self.policy.margin_rules if item.instrument_id == instrument_id), None)
            if rule is None:
                if instrument["asset_class"] == "future":
                    unconfigured_margin.append(instrument_id)
                continue
            if rule.model == "per_unit":
                initial = abs(quantity) * rule.initial_requirement
                maintenance = abs(quantity) * rule.maintenance_requirement
            elif mark is not None:
                notional_currency = self._pnl_currency(instrument)
                if rule.currency != notional_currency:
                    unconfigured_margin.append(instrument_id)
                    continue
                notional = abs(quantity * mark.price * self._multiplier(instrument))
                initial = notional * rule.initial_requirement
                maintenance = notional * rule.maintenance_requirement
            else:
                unconfigured_margin.append(instrument_id)
                continue
            margin_initial[rule.currency] = margin_initial.get(rule.currency, Decimal(0)) + initial
            margin_maintenance[rule.currency] = margin_maintenance.get(rule.currency, Decimal(0)) + maintenance
        breaches: list[ExposureLimitBreach] = []
        for limit in self.policy.exposure_limits:
            if limit.scope == "instrument":
                exposure = exposures.get(limit.key)
                actual = Decimal(0) if exposure is None else (
                    None if exposure.signed_notional is None else abs(exposure.signed_notional)
                )
                currency = self._pnl_currency(self._instruments[limit.key]) if limit.key in self._instruments else ""
            else:
                actual = gross.get(limit.key, Decimal(0))
                currency = limit.key
            if actual is not None and actual > limit.max_abs_notional:
                breaches.append(ExposureLimitBreach(limit.scope, limit.key, actual, limit.max_abs_notional, currency))
        return (
            ExposureSnapshot(MappingProxyType(exposures), MappingProxyType(gross), tuple(sorted(unpriced)), tuple(breaches)),
            MarginSnapshot(MappingProxyType(margin_initial), MappingProxyType(margin_maintenance), tuple(sorted(set(unconfigured_margin)))),
        )

    def snapshot(self) -> PortfolioSnapshot:
        positions = {key: value for key, value in self._positions.items() if value != 0}
        cash = {key: value for key, value in self._cash.items() if value != 0}
        unrealized: dict[str, Decimal] = {}
        for instrument_id, quantity in positions.items():
            mark = self._marks.get(instrument_id)
            average_cost = self._average_cost.get(instrument_id)
            if mark is None or average_cost is None:
                continue
            instrument = self._instruments[instrument_id]
            value = quantity * (mark.price - average_cost) * self._multiplier(instrument)
            currency = self._pnl_currency(instrument)
            unrealized[currency] = unrealized.get(currency, Decimal(0)) + value
        exposure, margin = self._risk_snapshots(positions)
        used_rates: dict[str, ConversionRate] = {}
        missing: set[str] = set()

        def convert(values: Mapping[str, Decimal]) -> Decimal | None:
            absent: set[str] = set()
            value = self._convert_map(values, used_rates, absent)
            missing.update(absent)
            return value

        cash_reported = convert(cash)
        realized_reported = convert(self._realized_pnl)
        unrealized_reported = convert(unrealized)
        commissions_reported = convert(self._commissions_paid)
        dividends_reported = convert(self._dividends)
        financing_reported = convert(self._financing)
        pnl_missing = set()
        for values in (self._realized_pnl, unrealized, self._commissions_paid, self._dividends, self._financing):
            for currency, amount in values.items():
                if amount != 0 and self._current_conversion(currency) is None:
                    pnl_missing.add(currency)
        if self.policy.reporting_currency is None or pnl_missing:
            net_pnl = None
        else:
            net_pnl = (
                (realized_reported or Decimal(0)) + (unrealized_reported or Decimal(0))
                + (dividends_reported or Decimal(0)) + (financing_reported or Decimal(0))
                - (commissions_reported or Decimal(0))
            )
        converted = ConvertedMetrics(
            reporting_currency=self.policy.reporting_currency,
            conversion_rates_used=MappingProxyType(used_rates),
            cash_balance=cash_reported,
            realized_pnl=realized_reported,
            unrealized_pnl=unrealized_reported,
            commissions_paid=commissions_reported,
            dividends=dividends_reported,
            financing=financing_reported,
            net_pnl=net_pnl,
            missing_conversion_currencies=tuple(sorted(missing)),
        )
        return PortfolioSnapshot(
            cash_balances=MappingProxyType(cash), positions=MappingProxyType(positions),
            realized_pnl=MappingProxyType({key: value for key, value in self._realized_pnl.items() if value != 0}),
            unrealized_pnl=MappingProxyType({key: value for key, value in unrealized.items() if value != 0}),
            version=self._version, as_of_ordinal=self._as_of_ordinal,
            commissions_paid=MappingProxyType({key: value for key, value in self._commissions_paid.items() if value != 0}),
            dividends_by_currency=MappingProxyType({key: value for key, value in self._dividends.items() if value != 0}),
            financing_by_currency=MappingProxyType({key: value for key, value in self._financing.items() if value != 0}),
            as_of_timestamp_ns=self._as_of_timestamp_ns,
            marks=MappingProxyType(dict(self._marks)), exposure=exposure, margin=margin,
            converted=converted, ledger_entry_count=len(self._ledger),
        )


def _event_timestamp_ns(event: MarketEvent) -> int | None:
    """Use captured event time first, then captured UTC receive time; never wall time."""

    source_time = event.source_timestamp_ns
    if (
        isinstance(source_time, int)
        and not isinstance(source_time, bool)
        and event.timestamp_semantics != "unknown"
    ):
        return source_time
    receive_time = event.receive_time_utc_ns
    if isinstance(receive_time, int) and not isinstance(receive_time, bool):
        return receive_time
    return None
