"""Resolve a request's Modal app from the active routing state."""

from dataclasses import dataclass

SUPPORTED_ROUTE_KINDS = ("policyengine", "us", "uk")


@dataclass(frozen=True)
class RouteResolution:
    """The Modal app a request routes to, and the provenance the gateway reports.

    ``response_version`` is the version echoed to the caller, and
    ``bundle_manifest`` is empty when the route came from the legacy version
    dicts. ``route_provenance`` marks pre-canonical routes, which SPM
    resolution treats differently.
    """

    app_name: str
    response_version: str
    policyengine_version: str | None
    bundle_manifest: dict
    route_provenance: str | None = None
    # The country model version this route names, when it names one. A
    # country route's key is that version; a policyengine-keyed route's is
    # not, and echoing a wrapper version there reads as a model version.
    country_model_version: str | None = None


def routing_state_routes(state: dict, kind: str) -> dict:
    """Return the state's route map for ``kind``, or an empty dict."""
    routes = state.get("routes")
    if not isinstance(routes, dict):
        return {}
    route_map = routes.get(kind)
    return route_map if isinstance(route_map, dict) else {}


def routing_state_latest(state: dict, kind: str) -> str | None:
    """Return the state's latest version for ``kind``, or None."""
    latest = state.get("latest")
    if not isinstance(latest, dict):
        return None
    value = latest.get(kind)
    return value if isinstance(value, str) else None


def routing_state_bundles(state: dict) -> dict:
    """Return the state's bundle manifests by policyengine version, or an empty dict."""
    bundles = state.get("bundles")
    return bundles if isinstance(bundles, dict) else {}


def routing_state_published_at(state: dict) -> dict[str, str]:
    """Return when each policyengine version's route was first published."""
    published_at = state.get("published_at")
    if not isinstance(published_at, dict):
        return {}
    return {
        version: timestamp
        for version, timestamp in published_at.items()
        if isinstance(version, str) and isinstance(timestamp, str)
    }


def routing_state_latest_since(state: dict) -> str | None:
    """Return when latest.policyengine last changed, or None."""
    latest_since = state.get("latest_since")
    return latest_since if isinstance(latest_since, str) and latest_since else None


def _bundle_manifest(state: dict, policyengine_version: str | None) -> dict:
    if policyengine_version is None:
        return {}
    manifest = routing_state_bundles(state).get(policyengine_version)
    return manifest if isinstance(manifest, dict) else {}


def _policyengine_version_for_app(
    state: dict, app_name: str, *, country: str, model_version: str
) -> str | None:
    """Return the wrapper version serving this country route, or None.

    Behaviour change from the pre-canonical gateway, deliberate: a country
    route whose one candidate bundle states a *different* model version is
    now rejected with a 400 instead of resolving. Publishing only ever adds
    country routes and overwrites the bundle manifest for the wrapper
    version it deploys (``update_version_registry``), so re-publishing one
    wrapper with an upgraded country model leaves the old country route
    pointing at an app whose manifest now states the new model. Before, that
    route resolved and the 202 body contradicted itself -- ``version`` from
    the stale route, ``policyengine_bundle.model_version`` from the live
    manifest -- and with canonical SPM the caller would also have been
    reading a capability the requested model never had. Nothing prunes the
    stale route, so the gateway refuses it instead.

    "States a different model version" and "states none" are one
    classification, computed once below: an absent country entry, an absent,
    non-string, empty or whitespace ``model_version`` all contradict
    nothing and still resolve.
    """
    candidates = {
        version
        for version, routed_app in routing_state_routes(state, "policyengine").items()
        if isinstance(version, str) and version != "latest" and routed_app == app_name
    } | {
        version
        for version, manifest in routing_state_bundles(state).items()
        if isinstance(version, str)
        and version != "latest"
        and isinstance(manifest, dict)
        and manifest.get("app_name") == app_name
    }
    matching = []
    unclassified = []
    for version in candidates:
        manifest = _bundle_manifest(state, version)
        country_bundle = manifest.get(country)
        candidate_model = (
            country_bundle.get("model_version")
            if isinstance(country_bundle, dict)
            else None
        )
        if not isinstance(candidate_model, str) or not candidate_model.strip():
            unclassified.append(version)
        elif candidate_model == model_version:
            matching.append(version)
    if len(matching) == 1 and not unclassified:
        return matching[0]
    if len(candidates) > 1:
        msg = (
            f"Ambiguous bundle for {country} version {model_version}; pass "
            "policyengine_version to select a bundle explicitly"
        )
        raise ValueError(msg)
    if not candidates:
        return None
    version = next(iter(candidates))
    if not unclassified:
        # Reuse the classification above rather than re-deriving it: the
        # shared validator reads any string as a stated model version, so
        # calling it unconditionally rejected a blank one here while the
        # ambiguity check above treated blank as unstated.
        _validate_legacy_version_matches_bundle(
            country=country,
            requested_version=model_version,
            manifest=_bundle_manifest(state, version),
        )
    return version


def policyengine_version_from_app_name(app_name: str) -> str | None:
    """Return the policyengine.py version an app name encodes, or None."""
    prefix = "policyengine-simulation-py"
    if not app_name.startswith(prefix):
        return None
    suffix = app_name.removeprefix(prefix)
    if not suffix:
        return None
    return suffix.replace("-", ".")


def _resolve_policyengine_route(
    state: dict,
    *,
    policyengine_version: str,
    response_version: str | None = None,
) -> RouteResolution:
    app_name = routing_state_routes(state, "policyengine").get(policyengine_version)
    if not isinstance(app_name, str):
        msg = f"Unknown policyengine.py version {policyengine_version}"
        raise ValueError(msg)  # noqa: TRY004  # an unknown version is a request error; route resolution callers catch ValueError
    return RouteResolution(
        app_name=app_name,
        response_version=response_version or policyengine_version,
        policyengine_version=policyengine_version,
        bundle_manifest=_bundle_manifest(state, policyengine_version),
    )


def _resolve_country_route(
    state: dict,
    *,
    country: str,
    version: str,
) -> RouteResolution | None:
    app_name = routing_state_routes(state, country).get(version)
    if not isinstance(app_name, str):
        return None
    policyengine_version = _policyengine_version_for_app(
        state, app_name, country=country, model_version=version
    )
    if policyengine_version is None:
        # The legacy seed producer infers these same wrapper routes. Missing
        # registry metadata must not erase a known future app version.
        policyengine_version = policyengine_version_from_app_name(app_name)
    return RouteResolution(
        app_name=app_name,
        response_version=version,
        policyengine_version=policyengine_version,
        bundle_manifest=_bundle_manifest(state, policyengine_version),
        country_model_version=version,
        # A country route the registry ties to no wrapper bundle at all is
        # pre-canonical by construction: every publish writes the wrapper
        # route and the bundle manifest for the app it deploys, and the
        # legacy seed only infers wrapper routes for prefixed app names. The
        # registry's ``generation`` marker cannot carry this — the next
        # publish rewrites it (update_version_registry).
        route_provenance=(
            "legacy-country-route"
            if policyengine_version is None and state.get("schema_version") == 1
            else None
        ),
    )


def _validate_legacy_version_matches_bundle(
    *,
    country: str,
    requested_version: str,
    manifest: dict,
) -> None:
    country_bundle = manifest.get(country)
    if not isinstance(country_bundle, dict):
        return
    model_version = country_bundle.get("model_version")
    if isinstance(model_version, str) and requested_version != model_version:
        msg = (
            f"Requested {country} version {requested_version} does not match "
            f"policyengine.py bundle {manifest.get('policyengine_version')} "
            f"({country} model version {model_version})"
        )
        raise ValueError(msg)


def resolve_from_active_state(
    *,
    state: dict,
    country: str,
    version: str | None,
    policyengine_version: str | None,
) -> RouteResolution:
    """Resolve a request's route from the active routing state."""
    if policyengine_version is not None:
        return _resolve_pinned_policyengine_route(
            state,
            country=country,
            version=version,
            policyengine_version=policyengine_version,
        )
    if version is None:
        return _resolve_latest_route(state, country=country)
    return _resolve_requested_version_route(state, country=country, version=version)


def _resolve_pinned_policyengine_route(
    state: dict,
    *,
    country: str,
    version: str | None,
    policyengine_version: str,
) -> RouteResolution:
    """Route to an explicit wrapper version, checking any requested model version."""
    resolution = _resolve_policyengine_route(
        state,
        policyengine_version=policyengine_version,
    )
    if version is not None:
        _validate_legacy_version_matches_bundle(
            country=country,
            requested_version=version,
            manifest=resolution.bundle_manifest,
        )
    return resolution


def _resolve_latest_route(state: dict, *, country: str) -> RouteResolution:
    """Route to the latest wrapper version, else the latest country model."""
    latest_policyengine = routing_state_latest(state, "policyengine")
    if latest_policyengine is not None:
        return _resolve_policyengine_route(
            state,
            policyengine_version=latest_policyengine,
        )
    latest_country_version = routing_state_latest(state, country)
    if latest_country_version is not None:
        country_resolution = _resolve_country_route(
            state,
            country=country,
            version=latest_country_version,
        )
        if country_resolution is not None:
            return country_resolution
    msg = "Routing state does not define a latest route"
    raise ValueError(msg)


def _resolve_requested_version_route(
    state: dict,
    *,
    country: str,
    version: str,
) -> RouteResolution:
    """Route ``version`` as a country model or wrapper version, refusing ambiguity."""
    country_resolution = _resolve_country_route(
        state,
        country=country,
        version=version,
    )
    policyengine_app = routing_state_routes(state, "policyengine").get(version)
    if country_resolution is not None and isinstance(policyengine_app, str):
        if country_resolution.app_name != policyengine_app:
            msg = (
                f"Ambiguous version {version} for country {country}; pass "
                "policyengine_version to select a bundle explicitly"
            )
            raise ValueError(msg)
        return _resolve_policyengine_route(
            state,
            policyengine_version=version,
        )
    if country_resolution is not None:
        return country_resolution
    if isinstance(policyengine_app, str):
        return _resolve_policyengine_route(
            state,
            policyengine_version=version,
        )

    msg = f"Unknown version {version} for country {country}"
    raise ValueError(msg)
