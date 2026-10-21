"""Small, language-neutral catalog models and validation errors."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


_DECIMAL_FIELDS = {
    "price_tick",
    "minimum_quantity",
    "quantity_increment",
    "pip_size",
    "contract_multiplier",
}


def _decode_decimals(value: Any, field_name: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {key: _decode_decimals(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_decimals(item) for item in value]
    if field_name in _DECIMAL_FIELDS and isinstance(value, str):
        return Decimal(value)
    return value


def _encode_decimals(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {key: _encode_decimals(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_decimals(item) for item in value]
    return value


@dataclass(frozen=True)
class ValidationIssue:
    """A machine-readable catalog validation problem."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class Catalog:
    """Parsed catalog. Nested records remain JSON-compatible mappings."""

    schema_version: str
    catalog_version: str
    instruments: tuple[Mapping[str, Any], ...]
    calendars: tuple[Mapping[str, Any], ...]
    continuous_futures: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Catalog":
        return cls(
            schema_version=value.get("schema_version", ""),
            catalog_version=value.get("catalog_version", ""),
            instruments=tuple(_decode_decimals(item) for item in value.get("instruments", ())),
            calendars=tuple(_decode_decimals(item) for item in value.get("calendars", ())),
            continuous_futures=tuple(
                _decode_decimals(item) for item in value.get("continuous_futures", ())
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "instruments": _encode_decimals(self.instruments),
            "calendars": _encode_decimals(self.calendars),
            "continuous_futures": _encode_decimals(self.continuous_futures),
        }


class CatalogLoadError(ValueError):
    """Raised when a catalog file cannot be read or parsed."""


class CatalogValidationError(ValueError):
    """Raised by ``load_catalog`` when a parsed catalog is invalid."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("Catalog validation failed:\n" + "\n".join(map(str, issues)))
