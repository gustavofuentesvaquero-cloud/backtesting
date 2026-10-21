"""Fail-closed validation of the non-trading deployment profile."""

from __future__ import annotations

import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "deployment_profile.schema.json"
_HOST_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)$")
_TRADING_PERMISSION_TERMS = {"trade", "trading", "orders.write", "order_entry", "execution", "brokerage"}


class DeploymentProfileError(ValueError):
    """Deployment profile violates the fail-closed, simulation-only contract."""


def validate_deployment_profile(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the JSON shape and enforce restrictions on execution and egress."""

    if not isinstance(value, Mapping):
        raise DeploymentProfileError("$: profile must be a JSON object.")
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeploymentProfileError(f"Cannot read deployment profile schema: {exc}") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" if isinstance(part, str) else f"[{part}]" for part in error.absolute_path)
        raise DeploymentProfileError(f"{path}: {error.message}")
    if value["live_execution_enabled"] is not False:
        raise DeploymentProfileError("$.live_execution_enabled must remain false.")
    if value["execution_adapter"] is not None:
        raise DeploymentProfileError("$.execution_adapter must be null; this engine has no live execution adapter.")
    network = value["outbound_network"]
    if network["default_policy"] != "deny":
        raise DeploymentProfileError("$.outbound_network.default_policy must be 'deny'.")
    for index, host in enumerate(network["allowed_hosts"]):
        if host == "*" or not _HOST_PATTERN.fullmatch(host) or ".." in host:
            raise DeploymentProfileError(f"$.outbound_network.allowed_hosts[{index}] must be one explicit hostname or IP, without a wildcard or URL.")
    credentials = value["credentials"]
    if credentials["secrets_in_config"] is not False or credentials["redact_from_logs"] is not True:
        raise DeploymentProfileError("$.credentials must keep secret values out of config and redact them from logs.")
    permissions = {item.lower() for item in credentials["permissions"]}
    unsafe = permissions & _TRADING_PERMISSION_TERMS
    if unsafe:
        raise DeploymentProfileError(f"$.credentials.permissions contains trading-capable permission(s): {sorted(unsafe)}.")
    if not permissions or any(not item.endswith(".read") for item in permissions):
        raise DeploymentProfileError("$.credentials.permissions must contain read-only scopes ending in '.read'.")
    return MappingProxyType(dict(value))


def load_deployment_profile(path: str | Path) -> Mapping[str, Any]:
    """Load and validate a deployment profile, reporting the file path on errors."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentProfileError(f"Cannot load deployment profile {path}: {exc}") from exc
    try:
        return validate_deployment_profile(value)
    except DeploymentProfileError as exc:
        raise DeploymentProfileError(f"{path}: {exc}") from exc

