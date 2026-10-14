#!/usr/bin/env python3
"""ServiceDesk Plus -> Microsoft Entra employee onboarding, one Dagu step per command.

Usage:
    python onboard.py <step> <ticket_id> [--config configs/onboarding.yaml] [--dry-run true|false]

Steps (run in this order; each refuses to start until the previous one completed):
    fetch                 fetch the ticket from ServiceDesk Plus (or --input-file for local tests)
    validate              required fields + business rules + directory pre-flight checks
    create-user           generate first.last@domain (or first.last<staff no>@domain) and create the user
    assign-license        assign the requested license SKU(s)
    add-groups            add the user to the requested user groups
    assign-rule-bundles   add the user to the requested rule-bundle groups
    assign-intune-apps    add the user to the requested Intune app assignment groups
    generate-report       verify against Entra, write the report, note it on the ticket
    close-ticket          set the resolution and close the ticket

    error-report          Dagu failure handler: failure report + private note on the ticket
    run-all               run every step in sequence (local testing)

Utilities (no ticket id):
    login                 graph.auth_mode=device_code: sign in once as an M365 admin
    list-skus             show the tenant's license SKUs (skuPartNumber) and free seats

Required environment variables:
    ENTRA_TENANT_ID                                   always (tenant GUID or e.g. contoso.onmicrosoft.com)
    ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET              graph.auth_mode=app
    SDP_API_KEY (servicedesk.auth: apikey) or SDP_USERNAME / SDP_PASSWORD (basic)

Exit codes: 0 ok, 2 validation, 3 configuration, 4 API, 5 step order, 6 verification, 1 other.
Passwords and tokens are never written to logs, state or reports.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from helpers.config import data_directories, load_configuration  # noqa: E402
from helpers.auth import acquire_graph_token, auth_mode  # noqa: E402
from helpers.errors import ConfigurationError, LifecycleError  # noqa: E402
from helpers.http_client import RetryHttpClient  # noqa: E402
from helpers.state import StateStore, validate_ticket_id  # noqa: E402
from workflow import STEP_ORDER, StepContext, handle_failure, run_step  # noqa: E402

LOG = logging.getLogger("onboarding")
UTILITY_COMMANDS = ["login", "list-skus"]
DEFAULT_CONFIG = HERE / "configs" / "onboarding.yaml"


def str_to_bool(value: str) -> bool:
    if str(value).strip().lower() in {"1", "true", "yes", "y", "on"}:
        return True
    if str(value).strip().lower() in {"", "0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Employee onboarding: ServiceDesk Plus -> Microsoft Entra")
    parser.add_argument("command", choices=STEP_ORDER + ["error-report", "run-all"] + UTILITY_COMMANDS)
    parser.add_argument("ticket_id", nargs="?", help="ServiceDesk Plus request ID (not used by login / list-skus)")
    parser.add_argument("--config", default=os.getenv("ONBOARDING_CONFIG", str(DEFAULT_CONFIG)))
    parser.add_argument(
        "--dry-run", nargs="?", const=True, default=False, type=str_to_bool,
        help="Read from Graph/SDP but make no changes (accepts true/false)",
    )
    parser.add_argument("--input-file", help="fetch only: read a saved SDP JSON response instead of calling SDP")
    parser.add_argument("--log-level", default=os.getenv("ONBOARDING_LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def token_cache_path(dirs: dict[str, Path]) -> Path:
    return dirs["cache"] / "msal_token_cache.json"


def build_context(args: argparse.Namespace) -> StepContext:
    config, mapping = load_configuration(Path(args.config).resolve())
    http = RetryHttpClient(config.get("processing", {}).get("retry_policy", {}))
    dirs = data_directories(config)

    def graph_factory(correlation_id: str):
        from clients.graph_client import GraphClient

        return GraphClient(config, http, correlation_id, token_cache_path=token_cache_path(dirs))

    def sdp_factory():
        from clients.servicedesk_client import ServiceDeskClient

        return ServiceDeskClient(config, http)

    return StepContext(
        ticket_id=validate_ticket_id(args.ticket_id),
        config=config,
        mapping=mapping,
        store=StateStore(dirs["state"]),
        dirs=dirs,
        graph_factory=graph_factory,
        sdp_factory=sdp_factory,
        dry_run=bool(args.dry_run),
        input_file=Path(args.input_file) if args.input_file else None,
        dag_run_id=os.getenv("DAG_RUN_ID"),
    )


def run_utility(args: argparse.Namespace) -> int:
    config, _ = load_configuration(Path(args.config).resolve())
    dirs = data_directories(config)
    if args.command == "login":
        if auth_mode(config) != "device_code":
            raise ConfigurationError("login is only needed when graph.auth_mode is device_code")
        acquire_graph_token(config, token_cache_path(dirs), interactive=True)
        print("Signed in. Steps will now reuse this sign-in until it expires.")
        return 0

    # list-skus
    from clients.graph_client import GraphClient

    http = RetryHttpClient(config.get("processing", {}).get("retry_policy", {}))
    graph = GraphClient(config, http, "list-skus", token_cache_path=token_cache_path(dirs))
    rows = []
    for sku in sorted(graph.list_subscribed_skus(), key=lambda s: s.get("skuPartNumber", "")):
        enabled = int((sku.get("prepaidUnits") or {}).get("enabled", 0))
        consumed = int(sku.get("consumedUnits", 0))
        rows.append((sku.get("skuPartNumber", ""), sku.get("skuId", ""), enabled, consumed, enabled - consumed,
                     sku.get("capabilityStatus", "")))
    print(f"{'skuPartNumber':<34} {'skuId':<38} {'total':>6} {'used':>6} {'free':>6}  status")
    for part, sku_id, enabled, consumed, free, status in rows:
        print(f"{part:<34} {sku_id:<38} {enabled:>6} {consumed:>6} {free:>6}  {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.command in UTILITY_COMMANDS:
            return run_utility(args)
        if not args.ticket_id:
            raise ConfigurationError(f"'{args.command}' needs a ticket id: onboard.py {args.command} <ticket_id>")
        ctx = build_context(args)
        if args.command == "error-report":
            print(json.dumps(handle_failure(ctx), indent=2))
            return 0
        commands = STEP_ORDER if args.command == "run-all" else [args.command]
        for command in commands:
            details = run_step(command, ctx)
            print(json.dumps({"step": command, "ticket_id": ctx.ticket_id, "result": details}, default=str))
        return 0
    except LifecycleError as exc:
        LOG.error("%s: %s", exc.__class__.__name__, exc)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - any crash must still fail the Dagu step
        LOG.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
