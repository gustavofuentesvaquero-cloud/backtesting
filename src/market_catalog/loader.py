"""Read and validate catalog JSON files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import Catalog, CatalogLoadError, CatalogValidationError
from .validation import validate_catalog


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-standard JSON constant {value!r} is not permitted.")


def load_catalog(path: str | Path) -> Catalog:
    """Load a catalog JSON file and raise a detailed error if it is invalid."""

    source = Path(path)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except OSError as exc:
        raise CatalogLoadError(f"Cannot read catalog {source}: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise CatalogLoadError(f"Cannot parse catalog {source}: {exc}") from exc

    if not isinstance(document, dict):
        raise CatalogLoadError(f"Catalog root in {source} must be a JSON object.")

    issues = validate_catalog(document)
    if issues:
        raise CatalogValidationError(issues)
    return Catalog.from_mapping(document)
