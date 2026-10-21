"""Command-line entry point for catalog validation."""

from __future__ import annotations

import argparse
import sys

from .loader import load_catalog
from .models import CatalogLoadError, CatalogValidationError
from .recording import RecordingError, SessionRecording
from .deployment import DeploymentProfileError, load_deployment_profile
from .observability import AlertPolicy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-catalog")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a market catalog JSON file")
    validate.add_argument("path", help="path to the catalog JSON file")
    verify = commands.add_parser("verify-session", help="verify session manifest and segment checksums")
    verify.add_argument("path", help="path to a recorded session directory")
    deployment = commands.add_parser("validate-deployment", help="validate a simulation-only deployment profile")
    deployment.add_argument("path", help="path to a deployment profile JSON file")
    alerts = commands.add_parser("validate-alerts", help="validate an observability alert policy")
    alerts.add_argument("path", help="path to an alert policy JSON file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate-deployment":
        try:
            profile = load_deployment_profile(args.path)
        except DeploymentProfileError as exc:
            print(str(exc), file=sys.stderr)
            return 4
        print(
            f"Valid simulation-only deployment profile {profile['profile_version']}: "
            f"execution disabled, {len(profile['outbound_network']['allowed_hosts'])} outbound hosts allowed."
        )
        return 0
    if args.command == "validate-alerts":
        try:
            policy = AlertPolicy.load(args.path)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 5
        print(f"Valid alert policy {policy.version}.")
        return 0
    if args.command == "verify-session":
        try:
            recording = SessionRecording(args.path)
        except RecordingError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        manifest = recording.manifest
        print(
            f"Verified session {manifest['session_id']} "
            f"({manifest['recording_mode']}): "
            f"{len(manifest['segments'])} segments, "
            f"{manifest['recording_metrics']['written_records']} records."
        )
        return 0
    try:
        catalog = load_catalog(args.path)
    except CatalogValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except CatalogLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        f"Valid catalog {catalog.catalog_version} "
        f"(schema {catalog.schema_version}): {len(catalog.instruments)} instruments, "
        f"{len(catalog.calendars)} calendars, {len(catalog.continuous_futures)} continuous futures series."
    )
    return 0
