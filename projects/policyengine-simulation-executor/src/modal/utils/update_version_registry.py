"""Publish the Modal routing state after a simulation API deployment.

Routing writes are serialized by the repository-wide GitHub Actions concurrency
group ``simulation-deploy-main`` (with ``cancel-in-progress: false``). Library
writers and the CLI permit writes only from its deployment and rollback
workflows; read-only and plan modes remain available locally.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from src.modal.routing_publisher.arguments import routing_argument_parser
from src.modal.routing_publisher.bundles import (
    BundleManifestMetadata,
    CountryBundleMetadata,
    build_bundle_manifest_metadata,
)
from src.modal.routing_publisher.legacy import build_legacy_seed_routing_state
from src.modal.routing_publisher.plans import (
    ROUTING_PLAN_SCHEMA,
    RoutingConflictError,
    plan_routing_publication,
    routing_state_digest,
    unfinished_publication,
    validate_routing_plan,
)
from src.modal.routing_publisher.publication import (
    UNOPENED_ROUTING_STORE,
    apply_routing_plan,
    complete_routing_plan,
    finalize_routing_publication,
    record_routing_plan,
    restore_routing_plan,
    restore_routing_plans,
)
from src.modal.routing_publisher.state import (
    APP_RELEASE_BUNDLES_DICT_NAME,
    POLICYENGINE_VERSION_DICT_NAME,
    ROUTING_PUBLICATION_KEY,
    ROUTING_STATE_ACTIVE_KEY,
    ROUTING_STATE_DICT_NAME,
    UK_VERSION_DICT_NAME,
    US_VERSION_DICT_NAME,
    DeployedAppVersions,
    LatestVersions,
    RouteMaps,
    RoutingState,
    build_next_routing_state,
    build_restored_routing_state,
    latest_release_identity,
    validate_routing_state,
)
from src.modal.routing_publisher.stores import (
    modal_dict_snapshot,
    publish_routing_state,
    read_latest_policyengine_version,
    restore_routing_state,
    routing_store,
    seed_active_routing_state_from_legacy,
)
from src.modal.routing_publisher.writers import (
    CANONICAL_GITHUB_REPOSITORY,
    SERIALIZED_ROUTING_WORKFLOW_FILES,
    require_serialized_routing_workflow,
)

__all__ = [
    "APP_RELEASE_BUNDLES_DICT_NAME",
    "CANONICAL_GITHUB_REPOSITORY",
    "POLICYENGINE_VERSION_DICT_NAME",
    "ROUTING_PLAN_SCHEMA",
    "ROUTING_PUBLICATION_KEY",
    "ROUTING_STATE_ACTIVE_KEY",
    "ROUTING_STATE_DICT_NAME",
    "SERIALIZED_ROUTING_WORKFLOW_FILES",
    "UK_VERSION_DICT_NAME",
    "UNOPENED_ROUTING_STORE",
    "US_VERSION_DICT_NAME",
    "BundleManifestMetadata",
    "CountryBundleMetadata",
    "DeployedAppVersions",
    "LatestVersions",
    "RouteMaps",
    "RoutingConflictError",
    "RoutingState",
    "apply_routing_plan",
    "build_bundle_manifest_metadata",
    "build_legacy_seed_routing_state",
    "build_next_routing_state",
    "build_restored_routing_state",
    "complete_routing_plan",
    "finalize_routing_publication",
    "latest_release_identity",
    "main",
    "plan_routing_publication",
    "publish_routing_state",
    "read_latest_policyengine_version",
    "record_routing_plan",
    "restore_routing_plan",
    "restore_routing_plans",
    "restore_routing_state",
    "routing_state_digest",
    "seed_active_routing_state_from_legacy",
    "validate_routing_plan",
    "validate_routing_state",
]


def _report(line: str = "") -> None:
    """Print one line of the command's progress report."""
    print(line)


class _UsageError(Exception):
    """A command-line mistake, reported as an argparse usage error."""


def _active_state(environment: str) -> object:
    snapshot = modal_dict_snapshot(
        name=ROUTING_STATE_DICT_NAME, environment=environment
    )
    return snapshot.get(ROUTING_STATE_ACTIVE_KEY)


def _print_requested_view(args: argparse.Namespace) -> bool:
    """Print the requested read-only view, if any; return whether one was printed."""
    if args.print_latest:
        _report(read_latest_policyengine_version(environment=args.environment) or "")
    elif args.print_identity:
        for key, value in latest_release_identity(
            _active_state(args.environment)
        ).items():
            _report(f"{key}={value}")
    elif args.print_pending_plan:
        store = routing_store(args.environment, create_if_missing=False)
        _report(json.dumps(unfinished_publication(store), indent=2, sort_keys=True))
    else:
        return False
    return True


def _writes_routing(args: argparse.Namespace) -> bool:
    """Whether the requested mode can write routing; only planning modes cannot."""
    return bool(
        args.apply_plan
        or args.record_plan
        or args.complete_plan
        or args.finalize_publication
        or args.restore_plan
        or args.restore_latest
        or args.seed_active_from_legacy
        or not (args.plan_out or args.dry_run)
    )


def _require_writes_allowed(args: argparse.Namespace) -> None:
    """Refuse a writing mode outside the workflows that serialize routing writes."""
    if not _writes_routing(args):
        return
    try:
        require_serialized_routing_workflow()
    except RuntimeError as exc:
        raise _UsageError(str(exc)) from exc


def _read_plan(path: str) -> dict:
    with Path(path).open() as handle:
        return json.load(handle)


def _finalize_command(args: argparse.Namespace) -> None:
    if args.release_environment is None or args.gates_passed is None:
        msg = "--finalize-publication requires --release-environment and --gates-passed"
        raise _UsageError(msg)
    if args.publication_run is None and args.gates_passed != "false":
        msg = "operator recovery requires --gates-passed false"
        raise _UsageError(msg)
    store = routing_store(args.environment, create_if_missing=False)
    pending = unfinished_publication(store)
    if store is None or pending is None:
        _report("No pending routing publication; routing is unchanged.")
    else:
        _report("Pending routing publication:")
        _report(
            json.dumps(store.get(ROUTING_PUBLICATION_KEY), indent=2, sort_keys=True)
        )
    outcome = finalize_routing_publication(
        environment=args.environment,
        release_environment=args.release_environment,
        gates_passed=args.gates_passed == "true",
        publication_run=args.publication_run,
    )
    if outcome == "foreign-publication":
        _report(
            "Pending routing publication belongs to another run or "
            "environment; routing is unchanged."
        )
    _report(f"Routing publication finalization: {outcome}")
    if args.github_output:
        with Path(args.github_output).open("a") as handle:
            handle.write(f"outcome={outcome}\n")


def _journal_command(args: argparse.Namespace) -> None:
    plan = _read_plan(args.record_plan or args.complete_plan)
    operation = record_routing_plan if args.record_plan else complete_routing_plan
    operation(environment=args.environment, plan=plan)
    _report(
        f"Routing publication {plan['publication_id']}: "
        f"{'pending' if args.record_plan else 'completed'}"
    )


def _apply_plan_command(args: argparse.Namespace) -> None:
    plan = _read_plan(args.apply_plan)
    apply_routing_plan(
        environment=args.environment,
        plan=plan,
    )
    _report(
        f"Published policyengine {plan['policyengine_version']} routing "
        f"({plan['after_sha256']}) in Modal environment {args.environment}"
    )


def _restore_plans_command(args: argparse.Namespace) -> None:
    plans = [_read_plan(path) for path in args.restore_plan]
    outcome = restore_routing_plans(
        environment=args.environment,
        plans=plans,
    )
    _report(
        f"Routing for policyengine {plans[-1]['policyengine_version']}: "
        f"{outcome} in Modal environment {args.environment}"
    )


def _restore_latest_command(args: argparse.Namespace) -> None:
    state = restore_routing_state(
        environment=args.environment,
        policyengine_version=args.restore_latest,
        expected_latest=args.expected_latest,
    )
    _report(f"Restored routing state in Modal environment: {args.environment}")
    _report(f"  latest.policyengine: {state['latest'].get('policyengine')}")
    _report(f"  latest.us: {state['latest'].get('us')}")
    _report(f"  latest.uk: {state['latest'].get('uk')}")


def _expected_latest_without_restore(_args: argparse.Namespace) -> None:
    msg = "--expected-latest needs --restore-latest"
    raise _UsageError(msg)


def _seed_command(args: argparse.Namespace) -> None:
    _report(
        "Seeding active routing state from legacy Modal dicts in "
        f"environment: {args.environment}"
    )
    state = seed_active_routing_state_from_legacy(environment=args.environment)
    _report()
    _report(f"  {ROUTING_STATE_DICT_NAME}[{ROUTING_STATE_ACTIVE_KEY}]: updated")
    _report(f"  policyengine routes: {len(state['routes']['policyengine'])}")
    _report(f"  US routes: {len(state['routes']['us'])}")
    _report(f"  UK routes: {len(state['routes']['uk'])}")
    _report(f"  bundle manifests: {len(state['bundles'])}")
    _report()
    _report("Legacy routing state seed completed successfully.")


def _deployed_app(args: argparse.Namespace) -> DeployedAppVersions:
    """Read the deployed app's versions, which publishing and planning require."""
    required_args = {
        "--app-name": args.app_name,
        "--policyengine-version": args.policyengine_version,
        "--us-version": args.us_version,
        "--uk-version": args.uk_version,
    }
    missing_args = [name for name, value in required_args.items() if not value]
    if missing_args:
        msg = f"the following arguments are required: {', '.join(missing_args)}"
        raise _UsageError(msg)
    return DeployedAppVersions(
        app_name=args.app_name,
        policyengine_version=args.policyengine_version,
        us_version=args.us_version,
        uk_version=args.uk_version,
    )


def _plan_command(args: argparse.Namespace, deployed: DeployedAppVersions) -> None:
    plan = plan_routing_publication(
        current_state=_active_state(args.environment),
        deployed=deployed,
        force_latest=args.force_latest,
        publication_id=args.publication_id,
    )
    if args.plan_out:
        with Path(args.plan_out).open("w") as handle:
            json.dump(plan, handle, indent=2, sort_keys=True)
    _report(f"before_sha256={plan['before_sha256']}")
    _report(f"after_sha256={plan['after_sha256']}")
    _report(f"advances_latest={'true' if plan['advances_latest'] else 'false'}")


def _publish_command(args: argparse.Namespace) -> None:
    deployed = _deployed_app(args)
    if args.plan_out or args.dry_run:
        _plan_command(args, deployed)
        return

    _report(f"Publishing routing state in Modal environment: {args.environment}")
    _report(f"  App name: {args.app_name}")
    _report(f"  policyengine.py version: {args.policyengine_version}")
    _report(f"  US version: {args.us_version}")
    _report(f"  UK version: {args.uk_version}")
    _report()

    state = publish_routing_state(
        environment=args.environment,
        deployed=deployed,
        force_latest=args.force_latest,
    )
    _report()
    _report(f"  {ROUTING_STATE_DICT_NAME}[{ROUTING_STATE_ACTIVE_KEY}]: updated")
    _report(f"  latest.policyengine: {state['latest'].get('policyengine')}")
    _report(f"  latest.us: {state['latest'].get('us')}")
    _report(f"  latest.uk: {state['latest'].get('uk')}")
    _report()
    _report("Routing state published successfully.")


def _requested_command(
    args: argparse.Namespace,
) -> Callable[[argparse.Namespace], None]:
    """Return the first requested writing mode's command, or publishing."""
    commands = (
        (args.finalize_publication, _finalize_command),
        (args.record_plan or args.complete_plan, _journal_command),
        (args.apply_plan, _apply_plan_command),
        (args.restore_plan, _restore_plans_command),
        (args.restore_latest, _restore_latest_command),
        (args.expected_latest, _expected_latest_without_restore),
        (args.seed_active_from_legacy, _seed_command),
    )
    return next(
        (command for requested, command in commands if requested), _publish_command
    )


def main() -> None:
    """Publish a deployed app's routes, or seed the state from legacy Dicts."""
    parser = routing_argument_parser()
    args = parser.parse_args()
    if _print_requested_view(args):
        return
    try:
        _require_writes_allowed(args)
        _requested_command(args)(args)
    except _UsageError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
