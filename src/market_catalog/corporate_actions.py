"""Explicit, mutually exclusive price and corporate-action accounting conventions."""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EquityActionPolicy:
    price_basis: str = "unknown"
    dividends: str = "excluded"
    splits: str = "reject"
    version: str = "1.0.0"

    def __post_init__(self):
        allowed = {
            ("unknown", "excluded", "reject"),
            ("raw", "cash_events", "position_events"),
            ("split_adjusted", "excluded", "embedded"),
            ("total_return_adjusted", "embedded", "embedded"),
        }
        if self.version != "1.0.0" or (self.price_basis, self.dividends, self.splits) not in allowed:
            raise ValueError("Incompatible corporate-action policy: adjustments and events must not be combined")

    def to_mapping(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict) or set(value) != {"version", "price_basis", "dividends", "splits"}:
            raise ValueError("Corporate-action policy has missing or unknown fields")
        return cls(**value)

    def validate_daily(self, bars: pd.DataFrame) -> dict:
        if self.dividends == "cash_events":
            raise ValueError("Daily engines do not apply cash/position events; use the raw-price ledger")
        observed = {}
        for name in ("dividend", "dividends", "split", "splits", "stock splits"):
            if name not in bars:
                continue
            values = pd.to_numeric(bars[name], errors="coerce").astype(float)
            if not np.isfinite(values).all() or (values < 0).any():
                raise ValueError(f"Invalid corporate-action column: {name}")
            active = values.ne(0)
            if name in {"split", "splits", "stock splits"}:
                active &= values.ne(1)
            observed[name] = int(active.sum())
            if active.any() and self.price_basis == "unknown":
                raise ValueError("Corporate actions require an explicit price basis")
        return {"policy": self.to_mapping(), "price_column": "close",
                "return_basis": ("unverified_legacy" if self.price_basis == "unknown" else
                                 "adjusted_series" if self.dividends == "embedded" else "price_return"),
                "dividend_cash_applied": False, "split_positions_applied": False,
                "observed_events": observed, "price_basis_status": "declared" if self.price_basis != "unknown" else "unknown"}


INITIAL_EQUITY_POLICY = EquityActionPolicy("split_adjusted", "excluded", "embedded")
