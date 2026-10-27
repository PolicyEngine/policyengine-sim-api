"""The routing state's Modal Dict names, shape, validation and next state."""

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from packaging.version import InvalidVersion, Version
from src.modal.routing_publisher.bundles import build_bundle_manifest_metadata

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


class LatestVersions(TypedDict, total=False):
    """The versions that unpinned requests route to, per route kind.

    A kind has no entry until a release of that kind is routed.
    """

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
    # Each policyengine version's bundle manifest, as publishing writes it
    # (BundleManifestMetadata). A stored state is read back unvalidated, so
    # its manifests are only known to be mappings until validate_routing_state
    # checks them against their routes.
    bundles: dict[str, Mapping[str, Any]]
    # When each policyengine version's route was first published, as an ISO
    # 8601 UTC time. Republishing a version keeps its date. States written
    # before this field existed have no entry until their next publish.
    published_at: NotRequired[dict[str, str]]
    # When latest.policyengine last changed: a publish that advanced it, a
    # forced latest or a rollback.
    latest_since: NotRequired[str]


def utc_now() -> str:
    """Return the current UTC time as the routing state records it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_newer_version(candidate: str, current: str | None) -> bool:
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


def empty_routing_state() -> RoutingState:
    """Return a routing state with no routes, latest versions or bundles."""
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


def coerce_routing_state(value: object) -> RoutingState:
    """Read ``value`` as a routing state, dropping fields of the wrong shape."""
    if not isinstance(value, dict):
        return empty_routing_state()

    latest = value.get("latest")
    routes = value.get("routes")
    bundles = value.get("bundles")

    coerced = empty_routing_state()
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

    published_at = _routing_map(value.get("published_at"))
    if published_at:
        coerced["published_at"] = published_at
    latest_since = value.get("latest_since")
    if isinstance(latest_since, str) and latest_since:
        coerced["latest_since"] = latest_since

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
    now: str | None = None,
) -> RoutingState:
    """Return ``current_state`` with routes and the bundle manifest for a new app added.

    ``latest`` advances only to a newer policyengine version, unless
    ``force_latest`` (an intentional rollback). Raises ValueError when the US
    or UK version differs from the installed bundle's model version.

    A publish that adds a policyengine version's route records
    ``published_at`` for it, and a change of ``latest.policyengine`` records
    ``latest_since``; both take ``now`` (default: the current UTC time).
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

    state = coerce_routing_state(current_state)
    newly_routed = policyengine_version not in state["routes"]["policyengine"]
    state["generation"] = f"{policyengine_version}:{app_name}"
    state["routes"]["policyengine"][policyengine_version] = app_name
    state["routes"]["us"][us_version] = app_name
    state["routes"]["uk"][uk_version] = app_name
    state["bundles"][policyengine_version] = manifest
    timestamp = now or utc_now()
    # Dates are recorded only when this publish is the event: a route that did
    # not exist, or a change of latest. A state from before dates were recorded
    # has no date for what it already routes, rather than a later, wrong one.
    if newly_routed:
        state.setdefault("published_at", {})[policyengine_version] = timestamp

    previous_latest = state["latest"].get("policyengine")
    should_advance = (
        is_newer_version(policyengine_version, previous_latest) or force_latest
    )
    if should_advance:
        state["latest"] = {
            "policyengine": policyengine_version,
            "us": us_version,
            "uk": uk_version,
        }
        if previous_latest != policyengine_version:
            state["latest_since"] = timestamp

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
    now: str | None = None,
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

    A rollback that moves ``latest.policyengine`` records ``latest_since``
    (``now``, default: the current UTC time).

    To undo a release completely (its routes as well as ``latest``), restore
    the routing plan its deployment recorded (:func:`restore_routing_plan`):
    which app served a model version before cannot be read from the state.
    """
    state = coerce_routing_state(current_state)
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
    if current_latest != policyengine_version:
        state["latest_since"] = now or utc_now()
    state["generation"] = f"rollback:{policyengine_version}:{app_name}"
    validate_routing_state(
        state,
        required_policyengine_manifests=(policyengine_version,),
    )
    return state


def latest_release_identity(state: object) -> dict[str, str]:
    """Describe what unpinned requests route to: version, app, UK model and dataset."""
    routing = coerce_routing_state(state)
    version = routing["latest"].get("policyengine") or ""
    manifest = routing["bundles"].get(version) or {}
    uk = manifest.get("uk") or {}
    return {
        "policyengine_version": version,
        "app_name": routing["routes"]["policyengine"].get(version, ""),
        "uk_model_version": routing["latest"].get("uk") or "",
        "uk_dataset_sha256": uk.get("default_dataset_sha256") or "",
    }
