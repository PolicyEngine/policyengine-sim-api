"""Build a routing state from the legacy per-version Modal Dicts."""

from copy import deepcopy

from src.modal.routing_publisher.state import (
    RoutingState,
    coerce_routing_state,
    empty_routing_state,
    is_newer_version,
    validate_routing_state,
)


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
        if is_newer_version(version, newest):
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
    state = empty_routing_state()
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


def merge_legacy_seed_into_current(
    *,
    current_state: object,
    legacy_seed: RoutingState,
) -> RoutingState:
    """Merge the legacy seed into the current state, whose routes and latest win."""
    current = coerce_routing_state(current_state)
    if not current["latest"] and not any(current["routes"].values()):
        return legacy_seed

    merged = deepcopy(legacy_seed)
    merged["generation"] = current["generation"] or legacy_seed["generation"]

    for kind in ("policyengine", "us", "uk"):
        merged["routes"][kind].update(current["routes"][kind])
        current_latest = current["latest"].get(kind)
        if current_latest is not None:
            merged["latest"][kind] = current_latest

    merged["bundles"].update(current["bundles"])
    if "published_at" in current:
        merged.setdefault("published_at", {}).update(current["published_at"])
    if "latest_since" in current:
        merged["latest_since"] = current["latest_since"]
    validate_routing_state(merged)
    return merged
