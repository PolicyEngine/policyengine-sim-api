"""Publish the Modal routing state after a simulation API deployment.

Routing writes are serialized by the repository-wide GitHub Actions concurrency
group ``simulation-deploy-main`` (with ``cancel-in-progress: false``). Library
writers and the CLI permit writes only from its deployment and rollback
workflows; read-only and plan modes remain available locally.
"""

import argparse
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

import modal
from packaging.version import InvalidVersion, Version

POLICYENGINE_VERSION_DICT_NAME = "simulation-api-policyengine-versions"
US_VERSION_DICT_NAME = "simulation-api-us-versions"
UK_VERSION_DICT_NAME = "simulation-api-uk-versions"
APP_RELEASE_BUNDLES_DICT_NAME = "simulation-api-app-release-bundles"
ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
ROUTING_STATE_ACTIVE_KEY = "active"
ROUTING_PUBLICATION_KEY = "publication"
# The Modal Dict intentionally stores one "active" key whose value is the full
# routing snapshot. Publishing replaces that single key after validation, so the
# gateway does not observe half-updated latest/routes/bundles fields if CI exits
# midway through deployment.


class CountryBundleMetadata(TypedDict):
    """One country's model package, data release and dataset URIs in a bundle."""

    country: str
    model_package_name: str
    model_version: str
    data_package_name: str
    data_package_version: str
    data_version: str
    data_artifact_revision: str
    default_dataset: str
    default_dataset_uri: str
    dataset_uris: dict[str, str]
    dataset_repo_types: dict[str, str]
    default_dataset_sha256: NotRequired[str | None]


class BundleManifestMetadata(TypedDict):
    """The release bundle served by one deployed app, keyed by its policyengine version.

    ``spm`` is present only when the runtime reports an SPM capability.
    """

    app_name: str
    policyengine_version: str
    us: CountryBundleMetadata
    uk: CountryBundleMetadata
    spm: NotRequired[dict]
    # Apps deployed before stage reporting have no key; the gateway then
    # reports no stage for their jobs.
    job_progress: NotRequired[int]
    # Present only for apps whose bundle routes UK area regions to a
    # local-area dataset; the gateway refuses breakdown=local_areas for any
    # other app, which would ignore the field and return a national result.
    local_area_breakdown: NotRequired[int]


class LatestVersions(TypedDict):
    """The versions that unpinned requests route to, per route kind."""

    policyengine: str
    us: str
    uk: str


class RouteMaps(TypedDict):
    """Version-to-app-name routes for each route kind."""

    policyengine: dict[str, str]
    us: dict[str, str]
    uk: dict[str, str]


class RoutingState(TypedDict):
    """The routing snapshot stored as one value under the routing Dict's active key."""

    schema_version: int
    generation: str
    latest: LatestVersions
    routes: RouteMaps
    bundles: dict[str, BundleManifestMetadata]


def _is_newer_version(candidate: str, current: str | None) -> bool:
    """Return True when ``candidate`` should replace ``current`` as 'latest'.

    If the current pointer is missing we always advance. If either version
    string is not PEP 440 parseable we fall back to a conservative rule:
    advance only when the strings differ and the operator has explicitly
    opted in via ``--force-latest``. That decision is made by the caller;
    this helper answers the strict-greater-than question for valid versions.
    """
    if current is None:
        return True
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


def _country_bundle_metadata(country: str) -> CountryBundleMetadata:
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # the executor package loads only when bundle metadata is built
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle(country)
    return {
        "country": bundle.country,
        "model_package_name": bundle.model_package_name,
        "model_version": bundle.model_version,
        "data_package_name": bundle.data_package_name,
        "data_package_version": bundle.data_package_version,
        "data_version": bundle.data_version,
        "data_artifact_revision": bundle.data_artifact_revision,
        "default_dataset": bundle.default_dataset,
        "default_dataset_uri": bundle.default_dataset_uri,
        "dataset_uris": dict(bundle.dataset_uris),
        "dataset_repo_types": dict(bundle.dataset_repo_types),
        "default_dataset_sha256": bundle.default_dataset_sha256,
    }


def build_bundle_manifest_metadata(
    *,
    app_name: str,
    policyengine_version: str,
) -> BundleManifestMetadata:
    """Describe the installed release bundle as served by ``app_name``."""
    from policyengine_simulation_contract.job_progress import (  # noqa: PLC0415  # the contract package loads only when bundle metadata is built
        JOB_PROGRESS_CAPABILITY_VERSION,
    )
    from policyengine_simulation_contract.local_area_breakdown import (  # noqa: PLC0415  # the contract package loads only when bundle metadata is built
        LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
    )
    from policyengine_simulation_executor.spm import (
        runtime_spm_capability,
    )
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the executor package loads only when bundle metadata is built
        uk_area_regions_routed,
    )

    capability = runtime_spm_capability()
    metadata: BundleManifestMetadata = {
        "app_name": app_name,
        "policyengine_version": policyengine_version,
        "us": _country_bundle_metadata("us"),
        "uk": _country_bundle_metadata("uk"),
        "job_progress": JOB_PROGRESS_CAPABILITY_VERSION,
    }
    if capability is not None:
        metadata["spm"] = capability.model_dump(mode="json")
    if uk_area_regions_routed():
        metadata["local_area_breakdown"] = LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION
    return metadata


def _empty_routing_state() -> RoutingState:
    return {
        "schema_version": 1,
        "generation": "",
        "latest": {},
        "routes": {
            "policyengine": {},
            "us": {},
            "uk": {},
        },
        "bundles": {},
    }


def _routing_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): value
        for key, value in value.items()
        if isinstance(value, str) and isinstance(key, str)
    }


def _coerce_routing_state(value: object) -> RoutingState:
    if not isinstance(value, dict):
        return _empty_routing_state()

    latest = value.get("latest")
    routes = value.get("routes")
    bundles = value.get("bundles")

    coerced = _empty_routing_state()
    coerced["generation"] = str(value.get("generation") or "")

    if isinstance(latest, dict):
        for key in ("policyengine", "us", "uk"):
            latest_value = latest.get(key)
            if isinstance(latest_value, str):
                coerced["latest"][key] = latest_value

    if isinstance(routes, dict):
        for key in ("policyengine", "us", "uk"):
            coerced["routes"][key] = _routing_map(routes.get(key))

    if isinstance(bundles, dict):
        coerced["bundles"] = {
            str(key): deepcopy(value)
            for key, value in bundles.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    return coerced


def validate_routing_state(
    state: RoutingState,
    *,
    required_policyengine_manifests: Iterable[str] = (),
) -> None:
    """Raise ValueError unless ``state`` is a consistent schema-1 routing snapshot.

    Each required ``latest`` entry must have a route, and every stored bundle
    manifest must match its policyengine route. A policyengine route may lack
    a manifest unless its version is in ``required_policyengine_manifests``.
    """
    if state.get("schema_version") != 1:
        msg = "Routing state schema_version must be 1"
        raise ValueError(msg)

    required_manifests = set(required_policyengine_manifests)
    _validate_latest_routes(
        state,
        require_policyengine=bool(
            state["routes"]["policyengine"] or required_manifests
        ),
    )
    _validate_bundle_manifests(state, required_manifests)


def _validate_latest_routes(state: RoutingState, *, require_policyengine: bool) -> None:
    """Require each US, UK and (when required) policyengine latest to be routed."""
    required_latest_routes = ["us", "uk"]
    if require_policyengine:
        required_latest_routes.append("policyengine")

    for route_name in required_latest_routes:
        latest = state["latest"].get(route_name)
        if not latest:
            msg = f"Routing state latest.{route_name} is missing"
            raise ValueError(msg)
        if latest not in state["routes"][route_name]:
            msg = f"Routing state latest.{route_name}={latest!r} has no route"
            raise ValueError(msg)


def _validate_bundle_manifests(
    state: RoutingState, required_manifests: set[str]
) -> None:
    """Require each stored manifest to match its policyengine route.

    A route may lack a manifest unless its version is in ``required_manifests``.
    """
    for policyengine_version, app_name in state["routes"]["policyengine"].items():
        manifest = state["bundles"].get(policyengine_version)
        if not isinstance(manifest, dict):
            if policyengine_version in required_manifests:
                msg = (
                    "Routing state policyengine route "
                    f"{policyengine_version!r} has no bundle manifest"
                )
                raise ValueError(msg)
            continue
        if manifest.get("policyengine_version") != policyengine_version:
            msg = (
                "Routing state bundle manifest key does not match "
                f"policyengine_version for {policyengine_version!r}"
            )
            raise ValueError(msg)
        if manifest.get("app_name") != app_name:
            msg = (
                "Routing state bundle manifest app_name does not match route "
                f"for {policyengine_version!r}"
            )
            raise ValueError(msg)


def _assert_version_matches_manifest(
    *,
    requested_name: str,
    requested_version: str,
    manifest_version: str,
) -> None:
    if requested_version != manifest_version:
        msg = (
            f"{requested_name} {requested_version!r} does not match the "
            f"policyengine.py bundle manifest value {manifest_version!r}"
        )
        raise ValueError(msg)


@dataclass(frozen=True)
class DeployedAppVersions:
    """A deployed simulation app and the package versions it serves."""

    app_name: str
    policyengine_version: str
    us_version: str
    uk_version: str


def build_next_routing_state(
    *,
    current_state: object,
    deployed: DeployedAppVersions,
    force_latest: bool = False,
) -> RoutingState:
    """Return ``current_state`` with routes and the bundle manifest for a new app added.

    ``latest`` advances only to a newer policyengine version, unless
    ``force_latest`` (an intentional rollback). Raises ValueError when the US
    or UK version differs from the installed bundle's model version.
    """
    app_name = deployed.app_name
    policyengine_version = deployed.policyengine_version
    us_version = deployed.us_version
    uk_version = deployed.uk_version
    manifest = build_bundle_manifest_metadata(
        app_name=app_name,
        policyengine_version=policyengine_version,
    )
    _assert_version_matches_manifest(
        requested_name="US version",
        requested_version=us_version,
        manifest_version=manifest["us"]["model_version"],
    )
    _assert_version_matches_manifest(
        requested_name="UK version",
        requested_version=uk_version,
        manifest_version=manifest["uk"]["model_version"],
    )

    state = _coerce_routing_state(current_state)
    state["generation"] = f"{policyengine_version}:{app_name}"
    state["routes"]["policyengine"][policyengine_version] = app_name
    state["routes"]["us"][us_version] = app_name
    state["routes"]["uk"][uk_version] = app_name
    state["bundles"][policyengine_version] = manifest

    previous_latest = state["latest"].get("policyengine")
    should_advance = (
        _is_newer_version(policyengine_version, previous_latest) or force_latest
    )
    if should_advance:
        state["latest"] = {
            "policyengine": policyengine_version,
            "us": us_version,
            "uk": uk_version,
        }

    validate_routing_state(
        state,
        required_policyengine_manifests=(policyengine_version,),
    )
    return state


def build_restored_routing_state(
    *,
    current_state: object,
    policyengine_version: str,
    expected_latest: str | None = None,
) -> RoutingState:
    """Point ``latest`` back at a policyengine.py version that was already published.

    The restored version must still have its route and bundle manifest in the
    state; ``latest.us`` and ``latest.uk`` become that manifest's model
    versions, and those model-version pins route to its app, so unpinned
    requests are served by exactly that bundle again. Every other route is
    left as it is: other releases stay pinnable.

    ``expected_latest`` makes the rollback conditional: it is refused unless
    ``latest.policyengine`` is still that version (the release being rolled
    back), so a rollback never overwrites a newer release published meanwhile.

    To undo a release completely (its routes as well as ``latest``), restore
    the routing plan its deployment recorded (:func:`restore_routing_plan`):
    which app served a model version before cannot be read from the state.
    """
    state = _coerce_routing_state(current_state)
    current_latest = state["latest"].get("policyengine")
    if expected_latest is not None and current_latest != expected_latest:
        msg = (
            f"latest.policyengine is {current_latest!r}, not {expected_latest!r}; "
            "another release changed routing, so this rollback was not applied"
        )
        raise ValueError(msg)
    app_name = state["routes"]["policyengine"].get(policyengine_version)
    manifest = state["bundles"].get(policyengine_version)
    if not app_name or not isinstance(manifest, dict):
        msg = (
            f"policyengine {policyengine_version!r} has no published route and "
            "bundle manifest to restore"
        )
        raise ValueError(msg)
    if manifest.get("app_name") != app_name:
        msg = (
            f"policyengine {policyengine_version!r}'s manifest names app "
            f"{manifest.get('app_name')!r}, but its route is {app_name!r}"
        )
        raise ValueError(msg)
    restored_models = {
        country: manifest[country]["model_version"] for country in ("us", "uk")
    }
    # As publishing does, the restored bundle serves its model-version pins.
    for country, model_version in restored_models.items():
        state["routes"][country][model_version] = app_name
    state["latest"] = {
        "policyengine": policyengine_version,
        "us": restored_models["us"],
        "uk": restored_models["uk"],
    }
    state["generation"] = f"rollback:{policyengine_version}:{app_name}"
    validate_routing_state(
        state,
        required_policyengine_manifests=(policyengine_version,),
    )
    return state


# --- Routing plans: publish exactly once, restore exactly what was replaced ---
#
# A deployment writes its plan (the routing state before and after publishing)
# before it changes anything, keeps it as a workflow artifact, then applies
# it. A failed release is undone by restoring the plan's "before" state, but
# only while the live state is still the plan's "after" state: a restore never
# overwrites a change made by anyone else, and a plan that was never applied
# restores nothing.

ROUTING_PLAN_SCHEMA = "policyengine-simulation.routing-plan/v1"


# Raised, as a RuntimeError, when a routing write or a plan's completion finds
# no routing-state Dict to write to.
UNOPENED_ROUTING_STORE = (
    "the routing state Modal Dict could not be opened; nothing was written"
)


class RoutingConflictError(RuntimeError):
    """The live routing state is not the one a plan expects."""


def routing_state_digest(state: object) -> str:
    """SHA-256 of a routing state's canonical JSON (``null`` when absent)."""
    text = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_copy(value: object) -> object:
    return json.loads(json.dumps(value))


def plan_routing_publication(
    *,
    current_state: object,
    deployed: DeployedAppVersions,
    force_latest: bool = False,
    publication_id: str | None = None,
) -> dict:
    """Plan the routing state before and after publishing a release, with digests.

    The published state's ``generation`` names this publication uniquely
    (``publication_id``, by default a random one), so a later publication of
    the same release is a different state and an old plan can never be
    restored over it. ``advances_latest`` says whether unpinned requests
    route to this release once the plan is applied.
    """
    publication_id = publication_id or uuid.uuid4().hex
    app_name = deployed.app_name
    policyengine_version = deployed.policyengine_version
    before = _json_copy(current_state) if current_state is not None else None
    after = _json_copy(
        build_next_routing_state(
            current_state=deepcopy(current_state),
            deployed=deployed,
            force_latest=force_latest,
        )
    )
    after["generation"] = f"{policyengine_version}:{app_name}:{publication_id}"
    return {
        "schema": ROUTING_PLAN_SCHEMA,
        "publication_id": publication_id,
        "policyengine_version": policyengine_version,
        "app_name": app_name,
        "before": before,
        "after": after,
        "before_sha256": routing_state_digest(before),
        "after_sha256": routing_state_digest(after),
        "advances_latest": after["latest"]["policyengine"] == policyengine_version,
    }


def validate_routing_plan(plan: object) -> dict:
    """Return a plan whose digests match its states, or raise ``ValueError``."""
    if not isinstance(plan, dict) or plan.get("schema") != ROUTING_PLAN_SCHEMA:
        msg = f"not a {ROUTING_PLAN_SCHEMA} routing plan"
        raise ValueError(msg)
    for side in ("before", "after"):
        if routing_state_digest(plan.get(side)) != plan.get(f"{side}_sha256"):
            msg = f"the plan's {side} state does not match its digest"
            raise ValueError(msg)
    if not isinstance(plan["after"], dict):
        msg = "the plan publishes no routing state"
        raise ValueError(msg)  # noqa: TRY004
    return plan


def _routing_store(environment: str, *, create_if_missing: bool) -> "modal.Dict | None":
    """Return the routing-state Dict, or None when it doesn't exist.

    modal's Dict.from_name returns a handle without contacting Modal, so a
    missing Dict would surface as NotFoundError at the first read. When the
    Dict must already exist, look it up now, so a missing one is reported here.
    """
    try:
        store = modal.Dict.from_name(
            ROUTING_STATE_DICT_NAME,
            environment_name=environment,
            create_if_missing=create_if_missing,
        )
        if not create_if_missing:
            store.hydrate()
    except KeyError:
        return None
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return None
        raise
    return store


CANONICAL_GITHUB_REPOSITORY = "PolicyEngine/policyengine-sim-api"
SERIALIZED_ROUTING_WORKFLOW_FILES = frozenset(
    {
        ".github/workflows/simulation-deploy.yml",
        ".github/workflows/stage12-deploy.yml",
        ".github/workflows/simulation-rollback.yml",
    }
)


def _require_serialized_routing_workflow() -> None:
    """Refuse routing writes outside the workflows that serialize them."""
    # GitHub associates a reusable workflow's github context with its caller,
    # so GITHUB_WORKFLOW_REF identifies the caller, not the reusable file:
    # https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#github-context
    # GITHUB_WORKFLOW_REF is owner/repo/path@ref:
    # https://docs.github.com/en/actions/reference/workflows-and-actions/variables
    workflow_path, separator, ref = os.environ.get("GITHUB_WORKFLOW_REF", "").partition(
        "@"
    )
    repository_prefix = f"{CANONICAL_GITHUB_REPOSITORY}/"
    workflow_file = (
        workflow_path.removeprefix(repository_prefix)
        if workflow_path.startswith(repository_prefix)
        else None
    )
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == CANONICAL_GITHUB_REPOSITORY
        and workflow_file in SERIALIZED_ROUTING_WORKFLOW_FILES
        and separator
        and ref
        and "@" not in ref
        and not any(character.isspace() for character in ref)
    ):
        return
    workflows = ", ".join(sorted(SERIALIZED_ROUTING_WORKFLOW_FILES))
    msg = (
        "Routing writes require GitHub Actions in "
        f"{CANONICAL_GITHUB_REPOSITORY} through {workflows}. "
        "Use those workflows, whose simulation-deploy-main concurrency group "
        "serializes routing writes. Read-only, --dry-run and --plan-out modes "
        "are available locally."
    )
    raise RuntimeError(msg)


def _unfinished_publication(store: "modal.Dict | None") -> dict | None:
    record = store.get(ROUTING_PUBLICATION_KEY) if store is not None else None
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("status") not in {
        "pending",
        "completed",
        "restored",
    }:
        msg = "invalid routing publication record; refusing routing writes"
        raise RoutingConflictError(msg)
    validate_routing_plan(record.get("plan"))
    return record["plan"] if record["status"] == "pending" else None


def _require_finished_publication(
    store: "modal.Dict | None", *, allowed_plans: Iterable[dict] = ()
) -> None:
    pending = _unfinished_publication(store)
    if pending is not None and not any(pending == plan for plan in allowed_plans):
        msg = (
            f"Unfinished routing publication {pending['publication_id']}: "
            "refusing new routing writes. Inspect --print-pending-plan and restore "
            "the recorded plan through simulation-rollback with target and "
            "resolve_pending_publication=true before deploying again."
        )
        raise RoutingConflictError(msg)


def _write_publication_record(store: "modal.Dict", *, plan: dict, status: str) -> None:
    record = {"status": status, "plan": _json_copy(plan)}
    store[ROUTING_PUBLICATION_KEY] = record
    if store.get(ROUTING_PUBLICATION_KEY) != record:
        msg = "the routing publication record reads back differently"
        raise RoutingConflictError(msg)


def record_routing_plan(*, environment: str, plan: dict) -> None:
    """Journal a pending release before its artifact is uploaded or routing changes.

    Its finalizer consumes this durable plan even if the runner loses the local
    file or uploading its artifact fails. Operator recovery can restore a plan
    if the finalizer itself is lost.
    """
    _require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = _routing_store(environment, create_if_missing=True)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    _require_finished_publication(store)
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["before_sha256"]
    ):
        msg = "the routing state changed before recording the plan"
        raise RoutingConflictError(msg)
    _write_publication_record(store, plan=plan, status="pending")


def complete_routing_plan(*, environment: str, plan: dict) -> None:
    """Acknowledge the owning release only after all its gates have passed."""
    _require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = _routing_store(environment, create_if_missing=False)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    if _unfinished_publication(store) != plan:
        msg = "no matching unfinished routing publication to complete"
        raise RoutingConflictError(msg)
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["after_sha256"]
    ):
        msg = "the completed release's routing state is not live"
        raise RoutingConflictError(msg)
    _write_publication_record(store, plan=plan, status="completed")


def apply_routing_plan(*, environment: str, plan: dict) -> None:
    """Publish ``plan``'s state if the live state is still its starting point."""
    _require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = _routing_store(environment, create_if_missing=True)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    _require_finished_publication(store, allowed_plans=[plan])
    current = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(current) != plan["before_sha256"]:
        msg = (
            "the routing state changed after this release was planned; "
            "nothing was published"
        )
        raise RoutingConflictError(msg)
    store[ROUTING_STATE_ACTIVE_KEY] = plan["after"]
    written = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(written) != plan["after_sha256"]:
        msg = "the published routing state reads back differently"
        raise RoutingConflictError(msg)


def restore_routing_plans(*, environment: str, plans: list[dict]) -> str:
    """Undo a deployment's plans (one per run attempt): ``restored`` or ``unchanged``.

    Repeatedly restores the starting state of a plan whose published state is
    live, newest attempt first, skipping plans that changed nothing, until no
    plan's published state is live. ``unchanged`` means the live state is a
    starting state no plan replaced (nothing was applied, or it was already
    undone). Any other live state is a conflict and nothing is changed.
    """
    _require_serialized_routing_workflow()
    for plan in plans:
        validate_routing_plan(plan)
    if not plans:
        msg = "no routing plan to restore"
        raise ValueError(msg)
    store = _routing_store(environment, create_if_missing=False)
    _require_finished_publication(store, allowed_plans=plans)
    pending = _unfinished_publication(store)
    if store is None:
        if all(plan["before"] is None for plan in plans):
            return "unchanged"
        msg = "there is no routing state to restore into"
        raise RoutingConflictError(msg)
    outcome = _restore_live_plans(store, plans)
    if pending is not None:
        _write_publication_record(store, plan=pending, status="restored")
    return outcome


def _restore_live_plans(store: "modal.Dict", plans: list[dict]) -> str:
    """Restore each live plan's starting state, newest attempt first.

    Returns ``restored``, or ``unchanged`` when the live state is a starting
    state no plan replaced; any other live state is a conflict.
    """
    remaining = [
        plan for plan in plans if plan["before_sha256"] != plan["after_sha256"]
    ]
    outcome = "unchanged"
    while True:
        digest = routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        live = [plan for plan in remaining if plan["after_sha256"] == digest]
        if not live:
            break
        plan = live[-1]
        remaining.remove(plan)
        _write_starting_state(store, plan)
        outcome = "restored"
    if outcome == "unchanged" and not any(
        plan["before_sha256"] == digest for plan in plans
    ):
        msg = (
            "the routing state is neither what this release replaced nor what "
            "it published; another change was made, so nothing was restored"
        )
        raise RoutingConflictError(msg)
    return outcome


def _write_starting_state(store: "modal.Dict", plan: dict) -> None:
    """Put back the state ``plan`` replaced, and check that it reads back."""
    if plan["before"] is None:
        store.pop(ROUTING_STATE_ACTIVE_KEY)
    else:
        store[ROUTING_STATE_ACTIVE_KEY] = plan["before"]
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["before_sha256"]
    ):
        msg = "the restored routing state reads back differently"
        raise RoutingConflictError(msg)


def restore_routing_plan(*, environment: str, plan: dict) -> str:
    """Undo one plan: ``restored``, ``unchanged`` (never applied) or a conflict."""
    _require_serialized_routing_workflow()
    return restore_routing_plans(environment=environment, plans=[plan])


# A workflow run's publication id is "<run id>-<run attempt>-<environment>".
_RUN_PUBLICATION_ID_PARTS = 3


def _publication_owned_by_release(
    plan: dict, *, publication_run: str | None, release_environment: str
) -> bool:
    """Match a workflow run across attempts, or the operator's selected store."""
    if publication_run is None:
        return True

    parts = str(plan.get("publication_id", "")).split("-")
    return (
        len(parts) == _RUN_PUBLICATION_ID_PARTS
        and parts[1].isdigit()
        and parts[2] == release_environment
        and parts[0] == publication_run
    )


def finalize_routing_publication(
    *,
    environment: str,
    release_environment: str,
    gates_passed: bool,
    publication_run: str | None = None,
) -> str:
    """Finish a release using its durable journal, without a plan artifact.

    A failed-jobs rerun owns every attempt of the same run and environment.
    Successful gates complete only the plan's live published state; every
    other owned pending publication is restored. Conflicts leave it pending.
    Omitting the run selects operator recovery in the chosen Modal store,
    which can only restore and does not depend on the publication id's format.
    Already finished records owned by this run return their recorded outcome
    without writing, so retrying a finalizer after output loss is safe.
    """
    _require_serialized_routing_workflow()
    if release_environment not in {"beta", "prod"}:
        msg = "release_environment must be beta or prod"
        raise ValueError(msg)
    if publication_run is None and gates_passed:
        msg = "operator recovery requires gates_passed=false"
        raise ValueError(msg)
    store = _routing_store(environment, create_if_missing=False)
    pending = _unfinished_publication(store)
    if pending is None:
        record = store.get(ROUTING_PUBLICATION_KEY) if store is not None else None
        if (
            record is not None
            and publication_run is not None
            and _publication_owned_by_release(
                record["plan"],
                publication_run=publication_run,
                release_environment=release_environment,
            )
            and (
                record["status"] != "completed"
                or routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
                == record["plan"]["after_sha256"]
            )
        ):
            return record["status"]
        return "no-pending"
    if not _publication_owned_by_release(
        pending,
        publication_run=publication_run,
        release_environment=release_environment,
    ):
        return "foreign-publication"
    if (
        gates_passed
        and routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        == pending["after_sha256"]
    ):
        complete_routing_plan(environment=environment, plan=pending)
        return "completed"
    restore_routing_plan(environment=environment, plan=pending)
    return "restored"


def latest_release_identity(state: object) -> dict[str, str]:
    """Describe what unpinned requests route to: version, app, UK model and dataset."""
    routing = _coerce_routing_state(state)
    version = routing["latest"].get("policyengine") or ""
    manifest = routing["bundles"].get(version) or {}
    uk = manifest.get("uk") or {}
    return {
        "policyengine_version": version,
        "app_name": routing["routes"]["policyengine"].get(version, ""),
        "uk_model_version": routing["latest"].get("uk") or "",
        "uk_dataset_sha256": uk.get("default_dataset_sha256") or "",
    }


def _modal_dict_snapshot(
    *,
    name: str,
    environment: str,
    create_if_missing: bool = False,
) -> dict:
    try:
        store = modal.Dict.from_name(
            name,
            environment_name=environment,
            create_if_missing=create_if_missing,
        )
    except KeyError:
        return {}
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return {}
        raise
    try:
        return dict(store.items())
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return {}
        raise


def _version_routes(snapshot: dict) -> dict[str, str]:
    return {
        str(version): app_name
        for version, app_name in snapshot.items()
        if version != "latest"
        and isinstance(version, str)
        and isinstance(app_name, str)
    }


def _legacy_latest(snapshot: dict) -> str | None:
    latest = snapshot.get("latest")
    return latest if isinstance(latest, str) else None


def _policyengine_version_from_app_name(app_name: str) -> str | None:
    prefix = "policyengine-simulation-py"
    if not app_name.startswith(prefix):
        return None
    suffix = app_name.removeprefix(prefix)
    if not suffix:
        return None
    return suffix.replace("-", ".")


def _infer_policyengine_routes(*route_maps: dict[str, str]) -> dict[str, str]:
    routes: dict[str, str] = {}
    for route_map in route_maps:
        for app_name in route_map.values():
            policyengine_version = _policyengine_version_from_app_name(app_name)
            if policyengine_version is not None:
                routes.setdefault(policyengine_version, app_name)
    return routes


def _newest_policyengine_route(routes: dict[str, str]) -> str | None:
    newest: str | None = None
    for version in routes:
        if _is_newer_version(version, newest):
            newest = version
    return newest


def _policyengine_route_for_latest_country_app(
    *,
    routes: dict[str, str],
    country_snapshots: tuple[dict, ...],
) -> str | None:
    app_to_policyengine = {app_name: version for version, app_name in routes.items()}
    inferred_versions: list[str] = []
    for snapshot in country_snapshots:
        latest = _legacy_latest(snapshot)
        if latest is None:
            continue
        app_name = snapshot.get(latest)
        if not isinstance(app_name, str):
            continue
        policyengine_version = app_to_policyengine.get(
            app_name
        ) or _policyengine_version_from_app_name(app_name)
        if policyengine_version is not None:
            inferred_versions.append(policyengine_version)

    if not inferred_versions:
        return None
    if len(set(inferred_versions)) == 1:
        return inferred_versions[0]
    return _newest_policyengine_route(
        {version: routes[version] for version in inferred_versions if version in routes}
    )


def _legacy_bundle_metadata(
    *,
    bundle_snapshot: dict,
    policyengine_version: str,
    app_name: str,
) -> dict | None:
    for key in (policyengine_version, app_name):
        metadata = bundle_snapshot.get(key)
        if isinstance(metadata, dict):
            metadata = deepcopy(metadata)
            metadata["policyengine_version"] = policyengine_version
            metadata["app_name"] = app_name
            return metadata
    return None


def build_legacy_seed_routing_state(
    *,
    policyengine_versions: dict,
    us_versions: dict,
    uk_versions: dict,
    app_release_bundles: dict | None = None,
) -> RoutingState:
    """Build a routing state from snapshots of the legacy per-kind version Dicts.

    Policyengine routes missing from the legacy Dict are inferred from
    country app names, and a missing policyengine ``latest`` is inferred from
    the countries' latest apps, falling back to the newest route.
    """
    state = _empty_routing_state()
    state["generation"] = "legacy-seed"
    state["routes"]["us"] = _version_routes(us_versions)
    state["routes"]["uk"] = _version_routes(uk_versions)
    state["routes"]["policyengine"] = {
        **_infer_policyengine_routes(
            state["routes"]["us"],
            state["routes"]["uk"],
        ),
        **_version_routes(policyengine_versions),
    }

    for kind, snapshot in (
        ("us", us_versions),
        ("uk", uk_versions),
    ):
        latest = _legacy_latest(snapshot)
        if latest is not None:
            state["latest"][kind] = latest

    policyengine_latest = _legacy_latest(
        policyengine_versions
    ) or _policyengine_route_for_latest_country_app(
        routes=state["routes"]["policyengine"],
        country_snapshots=(us_versions, uk_versions),
    )
    if policyengine_latest is None:
        policyengine_latest = _newest_policyengine_route(
            state["routes"]["policyengine"]
        )
    if policyengine_latest is not None:
        state["latest"]["policyengine"] = policyengine_latest

    bundle_snapshot = app_release_bundles or {}
    for policyengine_version, app_name in state["routes"]["policyengine"].items():
        metadata = _legacy_bundle_metadata(
            bundle_snapshot=bundle_snapshot,
            policyengine_version=policyengine_version,
            app_name=app_name,
        )
        if metadata is not None:
            state["bundles"][policyengine_version] = metadata

    validate_routing_state(state)
    return state


def _merge_legacy_seed_into_current(
    *,
    current_state: object,
    legacy_seed: RoutingState,
) -> RoutingState:
    current = _coerce_routing_state(current_state)
    if not current["latest"] and not any(current["routes"].values()):
        return legacy_seed

    merged = deepcopy(legacy_seed)
    merged["generation"] = current["generation"] or legacy_seed["generation"]

    for kind in ("policyengine", "us", "uk"):
        merged["routes"][kind].update(current["routes"][kind])
        if kind in current["latest"]:
            merged["latest"][kind] = current["latest"][kind]

    merged["bundles"].update(current["bundles"])
    validate_routing_state(merged)
    return merged


def seed_active_routing_state_from_legacy(*, environment: str) -> RoutingState:
    """Seed the active routing state from the legacy Dicts and store it.

    Routes and ``latest`` entries already in the active state win over the
    legacy seed.
    """
    _require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
    _require_finished_publication(store)
    legacy_seed = build_legacy_seed_routing_state(
        policyengine_versions=_modal_dict_snapshot(
            name=POLICYENGINE_VERSION_DICT_NAME,
            environment=environment,
        ),
        us_versions=_modal_dict_snapshot(
            name=US_VERSION_DICT_NAME,
            environment=environment,
        ),
        uk_versions=_modal_dict_snapshot(
            name=UK_VERSION_DICT_NAME,
            environment=environment,
        ),
        app_release_bundles=_modal_dict_snapshot(
            name=APP_RELEASE_BUNDLES_DICT_NAME,
            environment=environment,
        ),
    )
    next_state = _merge_legacy_seed_into_current(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        legacy_seed=legacy_seed,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def publish_routing_state(
    *,
    environment: str,
    deployed: DeployedAppVersions,
    force_latest: bool = False,
) -> RoutingState:
    """Add a deployed app to the active routing state and store it in one write."""
    _require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
    _require_finished_publication(store)
    next_state = build_next_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        deployed=deployed,
        force_latest=force_latest,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def read_latest_policyengine_version(*, environment: str) -> str | None:
    """Return the policyengine.py version unpinned requests are routed to now."""
    snapshot = _modal_dict_snapshot(
        name=ROUTING_STATE_DICT_NAME, environment=environment
    )
    state = _coerce_routing_state(snapshot.get(ROUTING_STATE_ACTIVE_KEY))
    return state["latest"].get("policyengine")


def restore_routing_state(
    *,
    environment: str,
    policyengine_version: str,
    expected_latest: str | None = None,
) -> RoutingState:
    """Point ``latest`` back at a published policyengine.py version and store it."""
    _require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=False,
    )
    _require_finished_publication(store)
    next_state = build_restored_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        policyengine_version=policyengine_version,
        expected_latest=expected_latest,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def _report(line: str = "") -> None:
    """Print one line of the command's progress report."""
    print(line)


def _parser() -> argparse.ArgumentParser:
    """Describe the routing publisher's modes and their options."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Publish the active routing state after Modal deployment.\n"
            "The simulation-deploy-main concurrency group serializes routing writes.\n"
            f"Writes require GitHub Actions in {CANONICAL_GITHUB_REPOSITORY}\n"
            "through one of these workflows:\n"
            + "\n".join(sorted(SERIALIZED_ROUTING_WORKFLOW_FILES))
            + "\nRead-only, --dry-run and --plan-out modes are available locally."
        ),
    )
    parser.add_argument(
        "--app-name",
        help="Versioned app name (e.g., policyengine-simulation-py4-10-0)",
    )
    parser.add_argument(
        "--policyengine-version",
        help="policyengine.py package version (e.g., 4.10.0)",
    )
    parser.add_argument(
        "--us-version",
        help="US package version (e.g., 1.459.0)",
    )
    parser.add_argument(
        "--uk-version",
        help="UK package version (e.g., 2.65.9)",
    )
    parser.add_argument(
        "--environment",
        required=True,
        help="Modal environment (staging or main)",
    )
    parser.add_argument(
        "--force-latest",
        action="store_true",
        help=(
            "Overwrite 'latest' even when the supplied version is older than "
            "the currently recorded latest (use for intentional rollbacks)."
        ),
    )
    parser.add_argument(
        "--seed-active-from-legacy",
        action="store_true",
        help=(
            "Create or merge simulation-api-routing-state[active] from the "
            "legacy policyengine/us/uk Modal dicts without publishing a new app."
        ),
    )
    parser.add_argument(
        "--print-latest",
        action="store_true",
        help="Print latest.policyengine (empty when unset) and change nothing.",
    )
    parser.add_argument(
        "--print-identity",
        action="store_true",
        help=(
            "Print what unpinned requests route to (policyengine_version, "
            "app_name, uk_model_version, uk_dataset_sha256) and change nothing."
        ),
    )
    parser.add_argument(
        "--print-pending-plan",
        action="store_true",
        help="Print the durable unfinished plan as JSON, including before upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With publishing arguments: print a routing plan's digests; change nothing."
        ),
    )
    parser.add_argument(
        "--plan-out",
        metavar="FILE",
        help=(
            "With the publishing arguments: write the routing plan (state "
            "before and after) to FILE and print its digests; change nothing."
        ),
    )
    parser.add_argument(
        "--apply-plan",
        metavar="FILE",
        help="Publish a plan, refusing unless the live state is its starting point.",
    )
    parser.add_argument(
        "--record-plan",
        metavar="FILE",
        help="Record a pending release before uploading its plan or publishing.",
    )
    parser.add_argument(
        "--complete-plan",
        metavar="FILE",
        help="Complete a recorded publication after all release gates pass.",
    )
    parser.add_argument(
        "--finalize-publication",
        action="store_true",
        help="Complete or restore the durable pending publication without an artifact.",
    )
    parser.add_argument(
        "--publication-run",
        help="With --finalize-publication: owning GitHub run id, ignoring its attempt.",
    )
    parser.add_argument(
        "--release-environment",
        choices=("beta", "prod"),
        help="With --finalize-publication: publication's release environment.",
    )
    parser.add_argument(
        "--gates-passed",
        choices=("true", "false"),
        help="With --finalize-publication: whether every release gate passed.",
    )
    parser.add_argument(
        "--github-output",
        metavar="FILE",
        help="With --finalize-publication: append outcome to a GitHub output file.",
    )
    parser.add_argument(
        "--restore-plan",
        metavar="FILE",
        nargs="+",
        help=(
            "Undo a deployment's plans (one per run attempt): restore each "
            "starting state while the live state is what that plan published."
        ),
    )
    parser.add_argument(
        "--publication-id",
        help="With --plan-out or --dry-run: a unique publication id (default random).",
    )
    parser.add_argument(
        "--restore-latest",
        metavar="POLICYENGINE_VERSION",
        help=(
            "Point latest back at this already-published policyengine.py "
            "version instead of publishing a new app."
        ),
    )
    parser.add_argument(
        "--expected-latest",
        metavar="POLICYENGINE_VERSION",
        help="With --restore-latest: refuse unless latest is still this version.",
    )
    return parser


class _UsageError(Exception):
    """A command-line mistake, reported as an argparse usage error."""


def _active_state(environment: str) -> object:
    snapshot = _modal_dict_snapshot(
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
        store = _routing_store(args.environment, create_if_missing=False)
        _report(json.dumps(_unfinished_publication(store), indent=2, sort_keys=True))
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
        _require_serialized_routing_workflow()
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
    store = _routing_store(args.environment, create_if_missing=False)
    pending = _unfinished_publication(store)
    if pending is None:
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
    _report(f"  latest.policyengine: {state['latest']['policyengine']}")
    _report(f"  latest.us: {state['latest']['us']}")
    _report(f"  latest.uk: {state['latest']['uk']}")


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
    _report(f"  latest.policyengine: {state['latest']['policyengine']}")
    _report(f"  latest.us: {state['latest']['us']}")
    _report(f"  latest.uk: {state['latest']['uk']}")
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
    parser = _parser()
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
