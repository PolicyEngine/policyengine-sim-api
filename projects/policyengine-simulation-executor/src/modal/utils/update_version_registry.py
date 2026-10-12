"""Publish the Modal routing state after a simulation API deployment."""

import argparse
import hashlib
import json
from copy import deepcopy
from typing import Iterable, NotRequired, TypedDict

import modal
from packaging.version import InvalidVersion, Version

POLICYENGINE_VERSION_DICT_NAME = "simulation-api-policyengine-versions"
US_VERSION_DICT_NAME = "simulation-api-us-versions"
UK_VERSION_DICT_NAME = "simulation-api-uk-versions"
APP_RELEASE_BUNDLES_DICT_NAME = "simulation-api-app-release-bundles"
ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
ROUTING_STATE_ACTIVE_KEY = "active"
# The Modal Dict intentionally stores one "active" key whose value is the full
# routing snapshot. Publishing replaces that single key after validation, so the
# gateway does not observe half-updated latest/routes/bundles fields if CI exits
# midway through deployment.


class CountryBundleMetadata(TypedDict):
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
    app_name: str
    policyengine_version: str
    us: CountryBundleMetadata
    uk: CountryBundleMetadata
    spm: NotRequired[dict]


class LatestVersions(TypedDict):
    policyengine: str
    us: str
    uk: str


class RouteMaps(TypedDict):
    policyengine: dict[str, str]
    us: dict[str, str]
    uk: dict[str, str]


class RoutingState(TypedDict):
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
    from policyengine_simulation_executor.release_bundle import (
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
    from policyengine_simulation_executor.spm import runtime_spm_capability

    capability = runtime_spm_capability()
    metadata: BundleManifestMetadata = {
        "app_name": app_name,
        "policyengine_version": policyengine_version,
        "us": _country_bundle_metadata("us"),
        "uk": _country_bundle_metadata("uk"),
    }
    if capability is not None:
        metadata["spm"] = capability.model_dump(mode="json")
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
    if state.get("schema_version") != 1:
        raise ValueError("Routing state schema_version must be 1")

    required_manifests = set(required_policyengine_manifests)
    required_latest_routes = ["us", "uk"]
    if state["routes"]["policyengine"] or required_manifests:
        required_latest_routes.append("policyengine")

    for route_name in required_latest_routes:
        latest = state["latest"].get(route_name)
        if not latest:
            raise ValueError(f"Routing state latest.{route_name} is missing")
        if latest not in state["routes"][route_name]:
            raise ValueError(
                f"Routing state latest.{route_name}={latest!r} has no route"
            )

    for policyengine_version, app_name in state["routes"]["policyengine"].items():
        manifest = state["bundles"].get(policyengine_version)
        if not isinstance(manifest, dict):
            if policyengine_version in required_manifests:
                raise ValueError(
                    "Routing state policyengine route "
                    f"{policyengine_version!r} has no bundle manifest"
                )
            continue
        if manifest.get("policyengine_version") != policyengine_version:
            raise ValueError(
                "Routing state bundle manifest key does not match "
                f"policyengine_version for {policyengine_version!r}"
            )
        if manifest.get("app_name") != app_name:
            raise ValueError(
                "Routing state bundle manifest app_name does not match route "
                f"for {policyengine_version!r}"
            )


def _assert_version_matches_manifest(
    *,
    requested_name: str,
    requested_version: str,
    manifest_version: str,
) -> None:
    if requested_version != manifest_version:
        raise ValueError(
            f"{requested_name} {requested_version!r} does not match the "
            f"policyengine.py bundle manifest value {manifest_version!r}"
        )


def build_next_routing_state(
    *,
    current_state: object,
    app_name: str,
    policyengine_version: str,
    us_version: str,
    uk_version: str,
    force_latest: bool = False,
) -> RoutingState:
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
        raise ValueError(
            f"latest.policyengine is {current_latest!r}, not {expected_latest!r}; "
            "another release changed routing, so this rollback was not applied"
        )
    app_name = state["routes"]["policyengine"].get(policyengine_version)
    manifest = state["bundles"].get(policyengine_version)
    if not app_name or not isinstance(manifest, dict):
        raise ValueError(
            f"policyengine {policyengine_version!r} has no published route and "
            "bundle manifest to restore"
        )
    if manifest.get("app_name") != app_name:
        raise ValueError(
            f"policyengine {policyengine_version!r}'s manifest names app "
            f"{manifest.get('app_name')!r}, but its route is {app_name!r}"
        )
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


class RoutingConflict(RuntimeError):
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
    app_name: str,
    policyengine_version: str,
    us_version: str,
    uk_version: str,
    force_latest: bool = False,
) -> dict:
    """The routing state before and after publishing a release, with digests.

    ``advances_latest`` says whether unpinned requests route to this release
    once the plan is applied.
    """

    before = _json_copy(current_state) if current_state is not None else None
    after = _json_copy(
        build_next_routing_state(
            current_state=deepcopy(current_state),
            app_name=app_name,
            policyengine_version=policyengine_version,
            us_version=us_version,
            uk_version=uk_version,
            force_latest=force_latest,
        )
    )
    return {
        "schema": ROUTING_PLAN_SCHEMA,
        "policyengine_version": policyengine_version,
        "app_name": app_name,
        "before": before,
        "after": after,
        "before_sha256": routing_state_digest(before),
        "after_sha256": routing_state_digest(after),
        "advances_latest": after["latest"]["policyengine"] == policyengine_version,
    }


def validate_routing_plan(plan: object) -> dict:
    """A plan whose digests match its states, or ``ValueError``."""

    if not isinstance(plan, dict) or plan.get("schema") != ROUTING_PLAN_SCHEMA:
        raise ValueError(f"not a {ROUTING_PLAN_SCHEMA} routing plan")
    for side in ("before", "after"):
        if routing_state_digest(plan.get(side)) != plan.get(f"{side}_sha256"):
            raise ValueError(f"the plan's {side} state does not match its digest")
    if not isinstance(plan["after"], dict):
        raise ValueError("the plan publishes no routing state")
    return plan


def _routing_store(environment: str, *, create_if_missing: bool):
    try:
        return modal.Dict.from_name(
            ROUTING_STATE_DICT_NAME,
            environment_name=environment,
            create_if_missing=create_if_missing,
        )
    except KeyError:
        return None
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return None
        raise


def apply_routing_plan(*, environment: str, plan: dict) -> None:
    """Publish ``plan``'s state if the live state is still its starting point."""

    validate_routing_plan(plan)
    store = _routing_store(environment, create_if_missing=True)
    current = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(current) != plan["before_sha256"]:
        raise RoutingConflict(
            "the routing state changed after this release was planned; "
            "nothing was published"
        )
    store[ROUTING_STATE_ACTIVE_KEY] = plan["after"]
    written = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(written) != plan["after_sha256"]:
        raise RoutingConflict("the published routing state reads back differently")


def restore_routing_plan(*, environment: str, plan: dict) -> str:
    """Undo ``plan``: ``restored``, ``unchanged`` (never applied) or a conflict."""

    validate_routing_plan(plan)
    store = _routing_store(environment, create_if_missing=False)
    current = store.get(ROUTING_STATE_ACTIVE_KEY) if store is not None else None
    digest = routing_state_digest(current)
    if digest == plan["before_sha256"]:
        return "unchanged"
    if digest != plan["after_sha256"]:
        raise RoutingConflict(
            "the routing state is neither what this release replaced nor what it "
            "published; another change was made, so nothing was restored"
        )
    if plan["before"] is None:
        store.pop(ROUTING_STATE_ACTIVE_KEY)
    else:
        store[ROUTING_STATE_ACTIVE_KEY] = plan["before"]
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["before_sha256"]
    ):
        raise RoutingConflict("the restored routing state reads back differently")
    return "restored"


def latest_release_identity(state: object) -> dict[str, str]:
    """What unpinned requests are routed to: version, app, UK model and dataset."""

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
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
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
    app_name: str,
    policyengine_version: str,
    us_version: str,
    uk_version: str,
    force_latest: bool = False,
) -> RoutingState:
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
    next_state = build_next_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        app_name=app_name,
        policyengine_version=policyengine_version,
        us_version=us_version,
        uk_version=uk_version,
        force_latest=force_latest,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def read_latest_policyengine_version(*, environment: str) -> str | None:
    """The policyengine.py version unpinned requests are routed to now."""

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
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=False,
    )
    next_state = build_restored_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        policyengine_version=policyengine_version,
        expected_latest=expected_latest,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def main():
    parser = argparse.ArgumentParser(
        description="Publish the active routing state after Modal deployment"
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
        "--restore-plan",
        metavar="FILE",
        help=(
            "Undo a plan: restore its starting state while the live state is "
            "still what it published."
        ),
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
    args = parser.parse_args()

    def active_state():
        snapshot = _modal_dict_snapshot(
            name=ROUTING_STATE_DICT_NAME, environment=args.environment
        )
        return snapshot.get(ROUTING_STATE_ACTIVE_KEY)

    if args.print_latest:
        print(read_latest_policyengine_version(environment=args.environment) or "")
        return

    if args.print_identity:
        for key, value in latest_release_identity(active_state()).items():
            print(f"{key}={value}")
        return

    if args.apply_plan or args.restore_plan:
        with open(args.apply_plan or args.restore_plan) as handle:
            plan = json.load(handle)
        if args.apply_plan:
            apply_routing_plan(environment=args.environment, plan=plan)
            print(
                f"Published policyengine {plan['policyengine_version']} routing "
                f"({plan['after_sha256']}) in Modal environment {args.environment}"
            )
        else:
            outcome = restore_routing_plan(environment=args.environment, plan=plan)
            print(
                f"Routing for policyengine {plan['policyengine_version']}: {outcome} "
                f"({plan['before_sha256']}) in Modal environment {args.environment}"
            )
        return

    if args.restore_latest:
        state = restore_routing_state(
            environment=args.environment,
            policyengine_version=args.restore_latest,
            expected_latest=args.expected_latest,
        )
        print(f"Restored routing state in Modal environment: {args.environment}")
        print(f"  latest.policyengine: {state['latest']['policyengine']}")
        print(f"  latest.us: {state['latest']['us']}")
        print(f"  latest.uk: {state['latest']['uk']}")
        return
    if args.expected_latest:
        parser.error("--expected-latest needs --restore-latest")

    if args.seed_active_from_legacy:
        print(
            "Seeding active routing state from legacy Modal dicts in "
            f"environment: {args.environment}"
        )
        state = seed_active_routing_state_from_legacy(environment=args.environment)
        print()
        print(f"  {ROUTING_STATE_DICT_NAME}[{ROUTING_STATE_ACTIVE_KEY}]: updated")
        print(f"  policyengine routes: {len(state['routes']['policyengine'])}")
        print(f"  US routes: {len(state['routes']['us'])}")
        print(f"  UK routes: {len(state['routes']['uk'])}")
        print(f"  bundle manifests: {len(state['bundles'])}")
        print()
        print("Legacy routing state seed completed successfully.")
        return

    required_args = {
        "--app-name": args.app_name,
        "--policyengine-version": args.policyengine_version,
        "--us-version": args.us_version,
        "--uk-version": args.uk_version,
    }
    missing_args = [name for name, value in required_args.items() if not value]
    if missing_args:
        parser.error(f"the following arguments are required: {', '.join(missing_args)}")

    if args.plan_out:
        plan = plan_routing_publication(
            current_state=active_state(),
            app_name=args.app_name,
            policyengine_version=args.policyengine_version,
            us_version=args.us_version,
            uk_version=args.uk_version,
            force_latest=args.force_latest,
        )
        with open(args.plan_out, "w") as handle:
            json.dump(plan, handle, indent=2, sort_keys=True)
        print(f"before_sha256={plan['before_sha256']}")
        print(f"after_sha256={plan['after_sha256']}")
        print(f"advances_latest={'true' if plan['advances_latest'] else 'false'}")
        return

    print(f"Publishing routing state in Modal environment: {args.environment}")
    print(f"  App name: {args.app_name}")
    print(f"  policyengine.py version: {args.policyengine_version}")
    print(f"  US version: {args.us_version}")
    print(f"  UK version: {args.uk_version}")
    print()

    state = publish_routing_state(
        environment=args.environment,
        app_name=args.app_name,
        policyengine_version=args.policyengine_version,
        us_version=args.us_version,
        uk_version=args.uk_version,
        force_latest=args.force_latest,
    )
    print()
    print(f"  {ROUTING_STATE_DICT_NAME}[{ROUTING_STATE_ACTIVE_KEY}]: updated")
    print(f"  latest.policyengine: {state['latest']['policyengine']}")
    print(f"  latest.us: {state['latest']['us']}")
    print(f"  latest.uk: {state['latest']['uk']}")
    print()
    print("Routing state published successfully.")


if __name__ == "__main__":
    main()
