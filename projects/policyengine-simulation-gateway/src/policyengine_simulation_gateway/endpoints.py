"""
FastAPI endpoints for the Gateway API.
"""

import logging
from dataclasses import dataclass
from typing import Optional, TypedDict

import modal
from fastapi import APIRouter, Depends, HTTPException, Request
from policyengine_observability import ObservabilityRuntime

from policyengine_simulation_contract.budget_window_state import (
    build_batch_status_response,
    create_initial_batch_state,
    get_batch_job_seed,
    get_batch_job_state,
    put_batch_job_seed,
    put_batch_job_state,
)
from policyengine_simulation_gateway.auth import require_auth
from policyengine_simulation_contract.spm import (
    SPMCapability,
    SPMInputError,
    spm_error_detail,
    resolve_spm_selection,
)
from policyengine_simulation_observability.errors import log_and_redact_exception
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    generate_observability_id,
    normalize_observability_id,
)
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    HealthResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    PolicyEngineBundle,
    SimulationRequest,
    VersionMap,
    VersionsResponse,
)
from policyengine_simulation_gateway.responses import (
    batch_status_response,
    failed_job_response,
    running_job_response,
)
from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_contract.hf_dataset import (
    HuggingFaceDatasetReferenceError,
)
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    BUDGET_WINDOW_STAGES,
    Stage,
)

logger = logging.getLogger(__name__)

router = APIRouter()
JOB_METADATA_DICT_NAME = "simulation-api-job-metadata"
POLICYENGINE_VERSION_DICT_NAME = "simulation-api-policyengine-versions"
ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
ROUTING_STATE_ACTIVE_KEY = "active"
SUPPORTED_ROUTE_KINDS = ("policyengine", "us", "uk")


class VersionRoutingState(TypedDict, total=False):
    """Routing-state fields required by the public version endpoints."""

    latest: dict[str, str]
    routes: dict[str, dict[str, str]]


@dataclass(frozen=True)
class RouteResolution:
    app_name: str
    response_version: str
    policyengine_version: str | None
    bundle_manifest: dict
    route_provenance: str | None = None
    # The country model version this route names, when it names one. A
    # country route's key is that version; a policyengine-keyed route's is
    # not, and echoing a wrapper version there reads as a model version.
    country_model_version: str | None = None


def _job_metadata_store():
    return modal.Dict.from_name(JOB_METADATA_DICT_NAME, create_if_missing=True)


def _runtime(request: Request) -> ObservabilityRuntime:
    runtime = getattr(request.app.state, "policyengine_observability", None)
    if not isinstance(runtime, ObservabilityRuntime):
        raise RuntimeError("Simulation gateway observability is not configured")
    return runtime


def _record_not_found(runtime: ObservabilityRuntime, message: str) -> None:
    runtime.record_exception(LookupError(message), handled=True, status_code=404)


def _country_bundle_data_version(country_bundle: dict) -> str | None:
    data_version = country_bundle.get("data_version")
    return data_version if isinstance(data_version, str) else None


def _country_bundle_data_package_version(country_bundle: dict) -> str | None:
    package_version = country_bundle.get("data_package_version")
    if isinstance(package_version, str):
        return package_version
    return _country_bundle_data_version(country_bundle)


def _country_bundle_data_artifact_revision(country_bundle: dict) -> str | None:
    artifact_revision = country_bundle.get("data_artifact_revision")
    return artifact_revision if isinstance(artifact_revision, str) else None


def _revision_from_dataset_uri(dataset_uri: str | None) -> str | None:
    if not isinstance(dataset_uri, str) or "@" not in dataset_uri:
        return None
    return dataset_uri.rsplit("@", maxsplit=1)[1]


def _bundle_response_data_version(
    *,
    country_bundle: dict,
    resolved_dataset: str | None,
) -> str | None:
    return _country_bundle_data_version(country_bundle) or _revision_from_dataset_uri(
        resolved_dataset
    )


def _resolve_dataset_uri_from_app_bundle(
    *,
    app_bundle: dict,
    country: str,
) -> str | None:
    country_bundle = app_bundle.get(country.lower())
    if not isinstance(country_bundle, dict):
        return None
    dataset_uri = country_bundle.get("default_dataset_uri")
    if not isinstance(dataset_uri, str):
        return None
    return runtime_dataset_uri(
        dataset_uri,
        default_revision=_country_bundle_data_package_version(country_bundle),
        artifact_revision=_country_bundle_data_artifact_revision(country_bundle),
        validate_hf=False,
    )


def _modal_exception_class(name: str):
    exception_module = getattr(modal, "exception", None)
    if exception_module is None:
        return None
    return getattr(exception_module, name, None)


def _is_modal_exception(exc: BaseException, name: str) -> bool:
    exception_class = _modal_exception_class(name)
    return exception_class is not None and isinstance(exc, exception_class)


def _is_modal_job_not_found(exc: BaseException) -> bool:
    return _is_modal_exception(exc, "NotFoundError") or _is_modal_exception(
        exc, "OutputExpiredError"
    )


def _optional_modal_dict(name: str):
    try:
        return modal.Dict.from_name(name)
    except KeyError:
        return None
    except Exception as exc:
        if _is_modal_exception(exc, "NotFoundError"):
            return None
        raise


def _active_routing_state() -> dict:
    store = _optional_modal_dict(ROUTING_STATE_DICT_NAME)
    if store is None:
        return {}
    state = store.get(ROUTING_STATE_ACTIVE_KEY)
    return state if isinstance(state, dict) else {}


def _routing_state_routes(state: dict, kind: str) -> dict:
    routes = state.get("routes")
    if not isinstance(routes, dict):
        return {}
    route_map = routes.get(kind)
    return route_map if isinstance(route_map, dict) else {}


def _routing_state_latest(state: dict, kind: str) -> str | None:
    latest = state.get("latest")
    if not isinstance(latest, dict):
        return None
    value = latest.get(kind)
    return value if isinstance(value, str) else None


def _routing_state_bundles(state: dict) -> dict:
    bundles = state.get("bundles")
    return bundles if isinstance(bundles, dict) else {}


def _bundle_manifest(state: dict, policyengine_version: str | None) -> dict:
    if policyengine_version is None:
        return {}
    manifest = _routing_state_bundles(state).get(policyengine_version)
    return manifest if isinstance(manifest, dict) else {}


def _policyengine_version_for_app(
    state: dict, app_name: str, *, country: str, model_version: str
) -> str | None:
    """The wrapper version serving this country route, or None.

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
        for version, routed_app in _routing_state_routes(state, "policyengine").items()
        if isinstance(version, str) and version != "latest" and routed_app == app_name
    } | {
        version
        for version, manifest in _routing_state_bundles(state).items()
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
        raise ValueError(
            f"Ambiguous bundle for {country} version {model_version}; pass "
            "policyengine_version to select a bundle explicitly"
        )
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


def _policyengine_version_from_app_name(app_name: str) -> str | None:
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
    app_name = _routing_state_routes(state, "policyengine").get(policyengine_version)
    if not isinstance(app_name, str):
        raise ValueError(f"Unknown policyengine.py version {policyengine_version}")
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
    app_name = _routing_state_routes(state, country).get(version)
    if not isinstance(app_name, str):
        return None
    policyengine_version = _policyengine_version_for_app(
        state, app_name, country=country, model_version=version
    )
    if policyengine_version is None:
        # The legacy seed producer infers these same wrapper routes. Missing
        # registry metadata must not erase a known future app version.
        policyengine_version = _policyengine_version_from_app_name(app_name)
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
        raise ValueError(
            f"Requested {country} version {requested_version} does not match "
            f"policyengine.py bundle {manifest.get('policyengine_version')} "
            f"({country} model version {model_version})"
        )


def _resolve_from_active_state(
    *,
    state: dict,
    country: str,
    version: str | None,
    policyengine_version: str | None,
) -> RouteResolution:
    if policyengine_version is not None:
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

    if version is None:
        latest_policyengine = _routing_state_latest(state, "policyengine")
        if latest_policyengine is not None:
            return _resolve_policyengine_route(
                state,
                policyengine_version=latest_policyengine,
            )
        latest_country_version = _routing_state_latest(state, country)
        if latest_country_version is not None:
            country_resolution = _resolve_country_route(
                state,
                country=country,
                version=latest_country_version,
            )
            if country_resolution is not None:
                return country_resolution
        raise ValueError("Routing state does not define a latest route")

    country_resolution = _resolve_country_route(
        state,
        country=country,
        version=version,
    )
    policyengine_app = _routing_state_routes(state, "policyengine").get(version)
    if country_resolution is not None and isinstance(policyengine_app, str):
        if country_resolution.app_name != policyengine_app:
            raise ValueError(
                f"Ambiguous version {version} for country {country}; pass "
                "policyengine_version to select a bundle explicitly"
            )
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

    raise ValueError(f"Unknown version {version} for country {country}")


def _resolve_from_legacy_dicts(
    *,
    country: str,
    version: str | None,
    policyengine_version: str | None,
) -> RouteResolution:
    if policyengine_version is not None:
        policyengine_versions = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        if policyengine_versions is None:
            raise ValueError(f"Unknown policyengine.py version {policyengine_version}")
        try:
            app_name = policyengine_versions[policyengine_version]
        except KeyError:
            raise ValueError(f"Unknown policyengine.py version {policyengine_version}")
        return RouteResolution(
            app_name=app_name,
            response_version=policyengine_version,
            policyengine_version=policyengine_version,
            bundle_manifest={},
        )

    country_versions = modal.Dict.from_name(f"simulation-api-{country}-versions")
    if version is None:
        resolved_version = country_versions["latest"]
    else:
        resolved_version = version

    try:
        app_name = country_versions[resolved_version]
    except KeyError:
        policyengine_versions = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        if policyengine_versions is not None and version is not None:
            try:
                app_name = policyengine_versions[version]
            except KeyError:
                pass
            else:
                return RouteResolution(
                    app_name=app_name,
                    response_version=version,
                    policyengine_version=version,
                    bundle_manifest={},
                )
        raise ValueError(f"Unknown version {resolved_version} for country {country}")

    return RouteResolution(
        app_name=app_name,
        response_version=resolved_version,
        policyengine_version=_policyengine_version_from_app_name(app_name),
        bundle_manifest={},
        country_model_version=resolved_version,
        route_provenance="legacy-country-dict",
    )


def _build_policyengine_bundle(
    country: str,
    resolution: RouteResolution,
) -> PolicyEngineBundle:
    app_bundle = resolution.bundle_manifest
    country_bundle = app_bundle.get(country.lower())
    if not isinstance(country_bundle, dict):
        country_bundle = {}
    resolved_dataset = _resolve_dataset_uri_from_app_bundle(
        app_bundle=app_bundle,
        country=country,
    )
    data_version = _bundle_response_data_version(
        country_bundle=country_bundle,
        resolved_dataset=resolved_dataset,
    )
    model_version = country_bundle.get("model_version") or resolution.response_version
    policyengine_version = app_bundle.get(
        "policyengine_version", resolution.policyengine_version
    )
    capability = app_bundle.get("spm") if country.lower() == "us" else None
    if capability is not None:
        try:
            capability = SPMCapability.model_validate(capability)
        except ValueError as exc:
            raise SPMInputError(
                "SPM_CONFIGURATION_UNAVAILABLE",
                "This worker bundle has invalid canonical SPM capability metadata",
            ) from exc
    return PolicyEngineBundle(
        model_version=str(model_version),
        policyengine_version=(
            str(policyengine_version) if isinstance(policyengine_version, str) else None
        ),
        data_version=str(data_version) if isinstance(data_version, str) else None,
        dataset=resolved_dataset,
        spm=capability,
    )


def _certified_model_version(country: str, route: RouteResolution) -> str | None:
    """The country model version the registry states for this route.

    ``PolicyEngineBundle.model_version`` falls back to the routing response
    version so the response always carries one. On a policyengine-keyed
    route with no manifest that fallback is a wrapper version, and reading
    it as a model version is how a legacy 5.2.0/5.3.0 route stopped
    resolving. An unstated model version contradicts nothing.
    """
    country_bundle = route.bundle_manifest.get(country.lower())
    if isinstance(country_bundle, dict):
        stated = country_bundle.get("model_version")
        if isinstance(stated, str) and stated.strip():
            return stated
    return route.country_model_version


def _resolve_request_spm(request, bundle, route):
    selection = resolve_spm_selection(
        request.country,
        request.spm,
        capability=bundle.spm,
        policyengine_version=bundle.policyengine_version,
        model_version=_certified_model_version(request.country, route),
        route_provenance=route.route_provenance,
    )
    if selection is not None:
        from policyengine_simulation_contract.spm import SPMSelection

        request.spm = SPMSelection.model_validate(selection)
    return selection


def _bundle_payload(bundle: PolicyEngineBundle, **dump_kwargs) -> dict:
    """Dump a bundle, omitting ``spm`` when the route has no capability.

    The 202 and 500 job bodies splat this dict in directly, bypassing the
    routes' ``response_model_exclude_none``. Every other optional bundle
    field was already emitted as an explicit null before canonical SPM, so
    only the new key is dropped: a legacy no-SPM body stays byte-identical.
    """
    return bundle.model_dump(
        exclude=None if bundle.spm is not None else {"spm"}, **dump_kwargs
    )


def _serialize_job_metadata(
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
    observability_id: str | None = None,
) -> dict:
    return {
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": _bundle_payload(bundle),
        "observability_id": observability_id,
    }


def _request_observability_id(
    request: SimulationRequest | BudgetWindowBatchRequest,
    http_request: Request,
) -> str:
    supplied = request.telemetry.observability_id if request.telemetry else None
    if isinstance(supplied, str) and supplied:
        return supplied
    return (
        normalize_observability_id(http_request.headers.get(OBSERVABILITY_ID_HEADER))
        or generate_observability_id()
    )


def _build_budget_window_parent_payload(
    request: BudgetWindowBatchRequest,
    *,
    resolved_version: str,
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
    observability_id: str,
) -> dict:
    payload = request.model_dump(
        exclude={"version", "policyengine_version", "telemetry"},
        mode="json",
        exclude_none=True,
    )
    if request.spm is not None:
        payload["spm"] = request.spm.model_dump(mode="json")
    payload["version"] = resolved_version
    telemetry = (
        request.telemetry.model_dump(mode="json")
        if request.telemetry is not None
        else {}
    )
    telemetry["observability_id"] = observability_id
    payload["_telemetry"] = telemetry
    payload["_metadata"] = {
        "resolved_version": resolved_version,
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": _bundle_payload(bundle, mode="json"),
    }
    return payload


def resolve_route(
    country: str,
    version: Optional[str],
    policyengine_version: Optional[str] = None,
) -> RouteResolution:
    """Resolve a country/package or policyengine.py version to a Modal app."""
    country_lower = country.lower()
    if country_lower not in ("us", "uk"):
        raise ValueError(f"Unknown country: {country}")

    state = _active_routing_state()
    if state:
        return _resolve_from_active_state(
            state=state,
            country=country_lower,
            version=version,
            policyengine_version=policyengine_version,
        )

    return _resolve_from_legacy_dicts(
        country=country_lower,
        version=version,
        policyengine_version=policyengine_version,
    )


def get_app_name(country: str, version: Optional[str]) -> tuple[str, str]:
    """Backward-compatible helper for tests and API v1 health checks."""
    resolution = resolve_route(country, version)
    return resolution.app_name, resolution.response_version


@router.post(
    "/simulate/economy/comparison",
    response_model=JobSubmitResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def submit_simulation(
    request: SimulationRequest,
    http_request: Request,
):
    """
    Submit a simulation job.

    Matches the existing Cloud Run API endpoint path.
    Routes to the appropriate app based on country and version params.
    Returns immediately with job_id for polling.
    """
    runtime = _runtime(http_request)
    observability_id = _request_observability_id(request, http_request)
    runtime.set_context(
        country=request.country,
        scope=request.scope,
        observability_id=observability_id,
    )
    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
    except ValueError as e:
        runtime.record_exception(e, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(e))

    runtime.set_context(
        resolved_app_name=route.app_name,
        resolved_version=route.response_version,
        policyengine_version=route.policyengine_version,
    )

    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.REQUEST_PARSE)):
        payload = request.model_dump(
            exclude={"version", "policyengine_version", "telemetry"},
            mode="json",
            exclude_none=True,
        )
    telemetry = (
        request.telemetry.model_dump(mode="json")
        if request.telemetry is not None
        else {}
    )
    telemetry["observability_id"] = observability_id
    payload["_telemetry"] = telemetry

    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.POLICYENGINE_BUNDLE)):
            bundle = _build_policyengine_bundle(
                request.country,
                route,
            )
        _resolve_request_spm(request, bundle, route)
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message, errors=[detail.model_dump()]
            )
        runtime.record_exception(exc, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.spm is not None:
        payload["spm"] = request.spm.model_dump(mode="json")
    payload["_observability_context"] = runtime.capture_context()

    logger.info(
        "Routing %s:%s to app %s (observability_id=%s)",
        request.country,
        route.response_version,
        route.app_name,
        observability_id,
    )

    # Spawn the job (returns immediately). ``Function.from_name`` is a lazy
    # handle — the control-plane RPC (hydration) happens inside ``spawn`` —
    # so both live under the spawn segment to time the real network cost.
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_FUNCTION_SPAWN)):
        sim_func = modal.Function.from_name(route.app_name, "run_simulation")
        call = sim_func.spawn(payload)

    runtime.set_context(job_id=call.object_id)

    job_metadata = _serialize_job_metadata(route.app_name, bundle, observability_id)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_METADATA_WRITE)):
        _job_metadata_store()[call.object_id] = job_metadata

    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
        return JobSubmitResponse(
            job_id=call.object_id,
            status="submitted",
            poll_url=f"/jobs/{call.object_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
            observability_id=observability_id,
        )


@router.post(
    "/simulate/economy/budget-window",
    response_model=BudgetWindowBatchSubmitResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def submit_budget_window_batch(
    request: BudgetWindowBatchRequest,
    http_request: Request,
):
    """
    Submit a budget-window batch job.
    """
    runtime = _runtime(http_request)
    observability_id = _request_observability_id(request, http_request)
    runtime.set_context(
        country=request.country,
        observability_id=observability_id,
    )
    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.ROUTE_RESOLUTION)):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
    except ValueError as e:
        runtime.record_exception(e, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(e))

    runtime.set_context(
        resolved_app_name=route.app_name,
        resolved_version=route.response_version,
        policyengine_version=route.policyengine_version,
    )

    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.POLICYENGINE_BUNDLE)):
            bundle = _build_policyengine_bundle(
                request.country,
                route,
            )
        _resolve_request_spm(request, bundle, route)
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message, errors=[detail.model_dump()]
            )
        runtime.record_exception(exc, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.REQUEST_PARSE)):
        payload = _build_budget_window_parent_payload(
            request,
            resolved_version=route.response_version,
            resolved_app_name=route.app_name,
            bundle=bundle,
            observability_id=observability_id,
        )

    # Lazy handle + spawn together: the RPC cost lands in ``spawn``.
    payload["_observability_context"] = runtime.capture_context()
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_FUNCTION_SPAWN)):
        batch_func = modal.Function.from_name(route.app_name, "run_budget_window_batch")
        call = batch_func.spawn(payload)
    batch_job_id = call.object_id
    runtime.set_context(batch_job_id=batch_job_id)

    seed_state = create_initial_batch_state(
        batch_job_id=batch_job_id,
        request=request,
        resolved_version=route.response_version,
        resolved_app_name=route.app_name,
        bundle=bundle,
        observability_id=observability_id,
    )
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_METADATA_WRITE)):
        put_batch_job_seed(seed_state)

    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
        return BudgetWindowBatchSubmitResponse(
            batch_job_id=batch_job_id,
            status=seed_state.status,
            poll_url=f"/budget-window-jobs/{batch_job_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
            observability_id=seed_state.observability_id,
        )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_job_status(job_id: str, request: Request):
    """
    Poll for job status.

    Returns:
        - 200 with status="complete" and result when done
        - 202 with status="running" while in progress
        - 500 with status="failed" and error on failure
        - 404 if job_id not found
    """
    runtime = _runtime(request)
    runtime.set_context(job_id=job_id)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_METADATA_READ)):
        job_metadata = _job_metadata_store().get(job_id)
    if job_metadata is None:
        _record_not_found(runtime, f"Job not found: {job_id}")
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    runtime.set_context(observability_id=job_metadata.get("observability_id"))

    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            call = modal.FunctionCall.from_id(job_id)
    except Exception as exc:
        if _is_modal_job_not_found(exc):
            runtime.record_exception(exc, handled=True, status_code=404)
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        runtime.record_exception(exc, handled=False, status_code=500)
        raise

    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            result = call.get(timeout=0)
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return JobStatusResponse(
                status="complete", result=result, **(job_metadata or {})
            )
    except TimeoutError:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return running_job_response(job_metadata)
    except Exception as exc:
        if _is_modal_job_not_found(exc):
            runtime.record_exception(exc, handled=True, status_code=404)
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message,
                errors=[detail.model_dump()],
                job_metadata=job_metadata,
            )
        redacted = log_and_redact_exception(
            exc,
            runtime=runtime,
            scope="simulation_job_status",
            context={"job_id": job_id},
        )
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return failed_job_response(error=redacted, job_metadata=job_metadata)


@router.get(
    "/budget-window-jobs/{batch_job_id}",
    response_model=BudgetWindowBatchStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_budget_window_job_status(batch_job_id: str, request: Request):
    """
    Poll for budget-window batch status.
    """
    runtime = _runtime(request)
    runtime.set_context(batch_job_id=batch_job_id)
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_LOAD)):
        state = get_batch_job_state(batch_job_id)
    if state is not None:
        runtime.set_context(observability_id=state.observability_id)
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(build_batch_status_response(state))

    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_LOAD)):
        seed_state = get_batch_job_seed(batch_job_id)
    if seed_state is None:
        _record_not_found(runtime, f"Budget-window job not found: {batch_job_id}")
        raise HTTPException(
            status_code=404, detail=f"Budget-window job not found: {batch_job_id}"
        )
    runtime.set_context(observability_id=seed_state.observability_id)

    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            call = modal.FunctionCall.from_id(batch_job_id)
    except Exception as exc:
        # The endpoint degrades gracefully to the seed state (a successful
        # 202/200 response), so this must NOT be recorded as a request
        # error — recording the exception would emit a request failure and
        # an error metric contradicting the status the client actually saw.
        runtime.event(
            "budget_window_parent_lookup_degraded",
            attributes={
                "batch_job_id": batch_job_id,
                "error_type": type(exc).__name__,
                "parent_call_not_found": _is_modal_job_not_found(exc),
            },
        )
        logger.warning(
            "Budget-window parent FunctionCall lookup failed; "
            "serving seed state (batch_job_id=%s)",
            batch_job_id,
            exc_info=exc,
        )
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(build_batch_status_response(seed_state))

    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            result = call.get(timeout=0)
    except TimeoutError:
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(build_batch_status_response(seed_state))
    except Exception as exc:
        # Persist the failure so subsequent polls don't resurrect the
        # "submitted" status from the seed store (#448). We deliberately
        # overwrite the main job store entry as well as the seed so either
        # lookup path observes the terminal failed state.
        detail = spm_error_detail(exc)
        message = (
            detail.message
            if detail
            else log_and_redact_exception(
                exc,
                runtime=runtime,
                scope="budget_window_parent_call",
                context={"batch_job_id": batch_job_id},
            )
        )
        seed_state.status = "failed"
        seed_state.errors = [detail] if detail else None
        seed_state.error = message
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)):
            put_batch_job_state(seed_state)
            put_batch_job_seed(seed_state)
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(build_batch_status_response(seed_state))

    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_RESULT_PARSE)):
        response = BudgetWindowBatchStatusResponse.model_validate(result)
    with runtime.span(
        BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
    ):
        return batch_status_response(response)


@router.get("/versions", response_model=VersionsResponse)
async def list_versions(request: Request) -> VersionsResponse:
    """List all available routing versions."""
    runtime = _runtime(request)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
        state = _active_routing_state()
    if state:
        capabilities = {}
        for name, bundle in _routing_state_bundles(state).items():
            if not isinstance(bundle, dict) or bundle.get("spm") is None:
                continue
            try:
                capabilities[name] = SPMCapability.model_validate(bundle["spm"])
            except ValueError:
                logger.warning("Omitting invalid SPM capability for bundle %s", name)
        return VersionsResponse(
            spm_capabilities=capabilities,
            policyengine=_version_map_from_state(state, "policyengine"),
            us=_version_map_from_state(state, "us"),
            uk=_version_map_from_state(state, "uk"),
        )

    # ``Dict.from_name`` is a lazy handle; the RPCs fire during the
    # ``dict(...)`` iterations, so those are what the segment must cover.
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
        policyengine_dict = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        us_dict = modal.Dict.from_name("simulation-api-us-versions")
        uk_dict = modal.Dict.from_name("simulation-api-uk-versions")
        return VersionsResponse(
            policyengine=VersionMap(
                dict(policyengine_dict) if policyengine_dict is not None else {}
            ),
            us=VersionMap(dict(us_dict)),
            uk=VersionMap(dict(uk_dict)),
        )


def _version_map_from_state(state: VersionRoutingState, kind: str) -> VersionMap:
    versions: dict[str, str] = dict(_routing_state_routes(state, kind))
    latest = _routing_state_latest(state, kind)
    if latest is not None:
        versions["latest"] = latest
    return VersionMap(versions)


@router.get("/versions/{kind}", response_model=VersionMap)
async def get_country_versions(kind: str, request: Request) -> VersionMap:
    """Get available versions for policyengine, US, or UK routing."""
    runtime = _runtime(request)
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROUTE_KINDS:
        _record_not_found(runtime, f"Unknown version kind: {kind}")
        raise HTTPException(status_code=404, detail=f"Unknown version kind: {kind}")

    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
        state = _active_routing_state()
    if state:
        return _version_map_from_state(state, kind_lower)

    # The ``dict(...)`` iteration is where the Modal Dict RPCs happen; the
    # from_name handle itself is lazy and free.
    if kind_lower == "policyengine":
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
            version_dict = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
            return VersionMap(dict(version_dict) if version_dict is not None else {})

    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
        version_dict = modal.Dict.from_name(f"simulation-api-{kind_lower}-versions")
        return VersionMap(dict(version_dict))


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse()


@router.post("/ping", response_model=PingResponse)
async def ping(request: PingRequest) -> PingResponse:
    """
    Verify the API is able to receive and process requests.
    Matches the policyengine_fastapi.ping endpoint for test compatibility.
    """
    return PingResponse(incremented=request.value + 1)
