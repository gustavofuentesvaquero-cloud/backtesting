"""Source-symbol resolution and canonical event normalization."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
import sys
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .events import EVENT_SCHEMA_VERSION, MarketEvent, RawFrame
from .models import Catalog

EVENT_SCHEMA_FILENAME = "market_event.schema.json"
EVENT_TYPES = {"quote", "trade", "book_snapshot", "book_delta", "trading_status"}
TIMESTAMP_SEMANTICS = {"event_time", "transaction_time", "publication_time", "unknown"}
DEPTH_TYPES = {"price_aggregated", "order_by_order"}
SEQUENCE_SCOPES = {"instrument_channel", "channel", "provider", "unknown"}


class NormalizationError(ValueError):
    """A source frame cannot be represented as a complete canonical event."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        provider_id: str,
        channel: str,
        instrument_id: str | None = None,
        arrival_ordinal: int | None = None,
        event_type: str | None = None,
    ):
        self.code = code
        self.path = path
        self.provider_id = provider_id
        self.channel = channel
        self.instrument_id = instrument_id
        self.arrival_ordinal = arrival_ordinal
        self.event_type = event_type
        super().__init__(f"{path}: {message}")


def _schema_path() -> Path:
    source_path = Path(__file__).resolve().parents[2] / "schemas" / EVENT_SCHEMA_FILENAME
    if source_path.is_file():
        return source_path
    installed_path = Path(sys.prefix) / "share" / "market-catalog" / "schemas" / EVENT_SCHEMA_FILENAME
    if installed_path.is_file():
        return installed_path
    raise FileNotFoundError(f"Could not locate {EVENT_SCHEMA_FILENAME}")


@lru_cache(maxsize=1)
def _event_validator() -> Draft202012Validator:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _path(parts: Any) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


class JsonFixtureNormalizer:
    """Normalize the documented fixture wire shape against a Phase 1 catalog.

    Real provider adapters should implement their own decoder and produce the
    same ``MarketEvent`` contract. This normalizer is intentionally not a claim
    about any provider's wire protocol.
    """

    def __init__(self, catalog: Catalog, *, preserve_raw: bool = True):
        self._preserve_raw = preserve_raw
        self._symbols: dict[tuple[str, str], str | None] = {}
        for instrument in catalog.instruments:
            instrument_id = instrument.get("id")
            for mapping in instrument.get("provider_symbols", ()):
                key = (mapping.get("provider_id"), mapping.get("symbol"))
                if key in self._symbols and self._symbols[key] != instrument_id:
                    self._symbols[key] = None
                else:
                    self._symbols[key] = instrument_id

    def _error(
        self,
        frame: RawFrame,
        code: str,
        path: str,
        message: str,
        *,
        instrument_id: str | None = None,
        event_type: str | None = None,
    ) -> NormalizationError:
        return NormalizationError(
            code,
            path,
            message,
            provider_id=frame.provider_id,
            channel=frame.channel,
            instrument_id=instrument_id,
            arrival_ordinal=frame.arrival_ordinal,
            event_type=event_type,
        )

    def _decimal(
        self,
        value: Any,
        path: str,
        frame: RawFrame,
        *,
        positive: bool = False,
        instrument_id: str,
        event_type: str,
    ) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise self._error(
                frame,
                "incomplete_update",
                path,
                "Expected an exact decimal string or JSON decimal number.",
                instrument_id=instrument_id,
                event_type=event_type,
            )
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError):
            raise self._error(
                frame,
                "incomplete_update",
                path,
                "Value is not a valid decimal.",
                instrument_id=instrument_id,
                event_type=event_type,
            ) from None
        if not result.is_finite() or (positive and result <= 0):
            message = "Quantity must be finite and greater than zero." if positive else "Price must be finite."
            raise self._error(
                frame,
                "incomplete_update",
                path,
                message,
                instrument_id=instrument_id,
                event_type=event_type,
            )
        return result

    def _require_mapping(
        self,
        value: Any,
        path: str,
        frame: RawFrame,
        instrument_id: str,
        event_type: str,
    ) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise self._error(
                frame,
                "incomplete_update",
                path,
                "Expected an object with the required fields.",
                instrument_id=instrument_id,
                event_type=event_type,
            )
        return value

    def _level(
        self,
        value: Any,
        path: str,
        frame: RawFrame,
        instrument_id: str,
        event_type: str,
        *,
        depth_type: str | None = None,
    ) -> dict[str, Any]:
        source = self._require_mapping(value, path, frame, instrument_id, event_type)
        if "price" not in source or "quantity" not in source:
            raise self._error(frame, "incomplete_update", path, "Book level requires price and quantity.", instrument_id=instrument_id, event_type=event_type)
        result: dict[str, Any] = {
            "price": self._decimal(source["price"], f"{path}.price", frame, instrument_id=instrument_id, event_type=event_type),
            "quantity": self._decimal(source["quantity"], f"{path}.quantity", frame, positive=True, instrument_id=instrument_id, event_type=event_type),
        }
        order_id = source.get("order_id")
        if depth_type == "order_by_order":
            if not isinstance(order_id, str) or not order_id:
                raise self._error(frame, "incomplete_update", f"{path}.order_id", "Order-by-order data requires order_id.", instrument_id=instrument_id, event_type=event_type)
            result["order_id"] = order_id
        elif order_id is not None:
            raise self._error(frame, "incomplete_update", f"{path}.order_id", "Price-aggregated data must not identify individual orders.", instrument_id=instrument_id, event_type=event_type)
        return result

    def _normalize_payload(
        self,
        event_type: str,
        payload: Any,
        frame: RawFrame,
        instrument_id: str,
    ) -> dict[str, Any]:
        data = self._require_mapping(payload, "$.payload", frame, instrument_id, event_type)
        if event_type == "quote":
            result: dict[str, Any] = {}
            for side in ("bid", "ask"):
                level = data.get(side)
                if level is None:
                    raise self._error(frame, "incomplete_update", f"$.payload.{side}", "A complete L1 quote requires both bid and ask sides.", instrument_id=instrument_id, event_type=event_type)
                level_data = self._require_mapping(level, f"$.payload.{side}", frame, instrument_id, event_type)
                result[side] = {
                    "price": self._decimal(level_data.get("price"), f"$.payload.{side}.price", frame, instrument_id=instrument_id, event_type=event_type),
                    "quantity": self._decimal(level_data.get("quantity"), f"$.payload.{side}.quantity", frame, positive=True, instrument_id=instrument_id, event_type=event_type),
                }
            return result

        if event_type == "trade":
            if "price" not in data or "quantity" not in data:
                raise self._error(frame, "incomplete_update", "$.payload", "Trade requires price and quantity.", instrument_id=instrument_id, event_type=event_type)
            aggressor = data.get("aggressor_side", "unknown")
            if not isinstance(aggressor, str) or aggressor not in {"buy", "sell", "unknown"}:
                raise self._error(frame, "incomplete_update", "$.payload.aggressor_side", "Expected buy, sell, or unknown.", instrument_id=instrument_id, event_type=event_type)
            return {
                "price": self._decimal(data["price"], "$.payload.price", frame, instrument_id=instrument_id, event_type=event_type),
                "quantity": self._decimal(data["quantity"], "$.payload.quantity", frame, positive=True, instrument_id=instrument_id, event_type=event_type),
                "aggressor_side": aggressor,
            }

        if event_type == "book_snapshot":
            depth_type = data.get("depth_type")
            if not isinstance(depth_type, str) or depth_type not in DEPTH_TYPES:
                raise self._error(frame, "incomplete_update", "$.payload.depth_type", "Expected price_aggregated or order_by_order.", instrument_id=instrument_id, event_type=event_type)
            result = {"depth_type": depth_type}
            for side in ("bids", "asks"):
                entries = data.get(side)
                if not isinstance(entries, list):
                    raise self._error(frame, "incomplete_update", f"$.payload.{side}", "Snapshot side must be an array.", instrument_id=instrument_id, event_type=event_type)
                result[side] = [
                    self._level(level, f"$.payload.{side}[{index}]", frame, instrument_id, event_type, depth_type=depth_type)
                    for index, level in enumerate(entries)
                ]
            return result

        if event_type == "book_delta":
            depth_type = data.get("depth_type")
            action = data.get("action")
            side = data.get("side")
            if (
                not isinstance(depth_type, str)
                or depth_type not in DEPTH_TYPES
                or not isinstance(action, str)
                or action not in {"add", "update", "delete"}
                or not isinstance(side, str)
                or side not in {"bid", "ask"}
            ):
                raise self._error(frame, "incomplete_update", "$.payload", "Book delta requires a known depth_type, action, and side.", instrument_id=instrument_id, event_type=event_type)
            if "price" not in data:
                raise self._error(frame, "incomplete_update", "$.payload.price", "Book delta requires a price.", instrument_id=instrument_id, event_type=event_type)
            quantity: Decimal | None
            if action == "delete":
                if data.get("quantity") is not None:
                    raise self._error(frame, "incomplete_update", "$.payload.quantity", "Delete operations must use a null quantity.", instrument_id=instrument_id, event_type=event_type)
                quantity = None
            else:
                if "quantity" not in data:
                    raise self._error(frame, "incomplete_update", "$.payload.quantity", "Add and update operations require quantity.", instrument_id=instrument_id, event_type=event_type)
                quantity = self._decimal(data["quantity"], "$.payload.quantity", frame, positive=True, instrument_id=instrument_id, event_type=event_type)
            order_id = data.get("order_id")
            if depth_type == "order_by_order" and (not isinstance(order_id, str) or not order_id):
                raise self._error(frame, "incomplete_update", "$.payload.order_id", "Order-by-order deltas require order_id.", instrument_id=instrument_id, event_type=event_type)
            if depth_type == "price_aggregated" and order_id is not None:
                raise self._error(frame, "incomplete_update", "$.payload.order_id", "Price-aggregated deltas must not identify individual orders.", instrument_id=instrument_id, event_type=event_type)
            result = {
                "depth_type": depth_type,
                "action": action,
                "side": side,
                "price": self._decimal(data["price"], "$.payload.price", frame, instrument_id=instrument_id, event_type=event_type),
                "quantity": quantity,
            }
            if order_id is not None:
                result["order_id"] = order_id
            return result

        if event_type == "trading_status":
            status = data.get("status")
            if not isinstance(status, str) or status not in {"open", "halted", "closed", "unknown"}:
                raise self._error(frame, "incomplete_update", "$.payload.status", "Expected open, halted, closed, or unknown.", instrument_id=instrument_id, event_type=event_type)
            reason = data.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise self._error(frame, "incomplete_update", "$.payload.reason", "Reason must be a string or null.", instrument_id=instrument_id, event_type=event_type)
            return {"status": status, "reason": reason}

        raise self._error(frame, "unsupported_event", "$.event_type", f"Unsupported event type {event_type!r}.", instrument_id=instrument_id, event_type=event_type)

    def normalize(self, frame: RawFrame) -> MarketEvent:
        try:
            message = json.loads(frame.raw_payload.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(frame, "malformed_frame", "$", f"Frame is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(message, Mapping):
            raise self._error(frame, "incomplete_update", "$", "Wire message must be a JSON object.")

        event_type = message.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise self._error(frame, "unsupported_event", "$.event_type", f"Unsupported event type {event_type!r}.", event_type=event_type if isinstance(event_type, str) else None)
        symbol = message.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise self._error(frame, "incomplete_update", "$.symbol", "Wire message requires a provider symbol.", event_type=event_type)
        instrument_id = self._symbols.get((frame.provider_id, symbol))
        if instrument_id is None:
            raise self._error(frame, "unknown_instrument", "$.symbol", f"Symbol {symbol!r} has no unique mapping in the catalog for provider {frame.provider_id!r}.", event_type=event_type)

        sequence = message.get("sequence")
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0):
            raise self._error(frame, "incomplete_update", "$.sequence", "Sequence must be a non-negative integer or null.", instrument_id=instrument_id, event_type=event_type)
        sequence_scope = message.get("sequence_scope", "unknown")
        if not isinstance(sequence_scope, str) or sequence_scope not in SEQUENCE_SCOPES:
            raise self._error(frame, "incomplete_update", "$.sequence_scope", "Expected instrument_channel, channel, provider, or unknown.", instrument_id=instrument_id, event_type=event_type)

        timestamp = message.get("source_timestamp")
        if timestamp is None:
            timestamp = {}
        if not isinstance(timestamp, Mapping):
            raise self._error(frame, "incomplete_update", "$.source_timestamp", "Source timestamp must be an object or omitted.", instrument_id=instrument_id, event_type=event_type)
        source_ns = timestamp.get("unix_ns")
        if source_ns is not None and (isinstance(source_ns, bool) or not isinstance(source_ns, int) or source_ns < 0):
            raise self._error(frame, "incomplete_update", "$.source_timestamp.unix_ns", "unix_ns must be a non-negative integer or null.", instrument_id=instrument_id, event_type=event_type)
        semantics = timestamp.get("semantics", "unknown")
        if not isinstance(semantics, str) or semantics not in TIMESTAMP_SEMANTICS:
            raise self._error(frame, "incomplete_update", "$.source_timestamp.semantics", "Unknown source timestamp semantics.", instrument_id=instrument_id, event_type=event_type)
        raw_timestamp = timestamp.get("raw")
        if raw_timestamp is not None and not isinstance(raw_timestamp, str):
            raise self._error(frame, "incomplete_update", "$.source_timestamp.raw", "Raw source timestamp must be a string or null.", instrument_id=instrument_id, event_type=event_type)

        try:
            payload = self._normalize_payload(event_type, message.get("payload"), frame, instrument_id)
        except NormalizationError:
            raise

        flags: list[str] = []
        if source_ns is None or semantics == "unknown":
            flags.append("timestamp_unverified")
        if sequence is None:
            flags.append("sequence_unavailable")

        event = MarketEvent(
            provider_id=frame.provider_id,
            channel=frame.channel,
            instrument_id=instrument_id,
            event_type=event_type,
            source_timestamp_raw=raw_timestamp,
            source_timestamp_ns=source_ns,
            timestamp_semantics=semantics,
            receive_time_utc_ns=frame.receive_time_utc_ns,
            receive_time_monotonic_ns=frame.receive_time_monotonic_ns,
            arrival_ordinal=frame.arrival_ordinal,
            sequence=sequence,
            sequence_scope=sequence_scope,
            payload=payload,
            quality_flags=tuple(flags),
            decision_usable=True,
            book_state="not_applicable",
            raw_frame=frame.raw_payload if self._preserve_raw else None,
        )
        errors = list(_event_validator().iter_errors(event.to_mapping()))
        if errors:
            error = min(errors, key=lambda item: tuple(str(part) for part in item.absolute_path))
            raise self._error(
                frame,
                "incomplete_update",
                _path(error.absolute_path),
                error.message,
                instrument_id=instrument_id,
                event_type=event_type,
            )
        return event
