"""
FastAPI endpoints for the Gateway API.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import modal
from fastapi import APIRouter, Depends, HTTPException
from policyengine_observability import (
    record_error,
    segment,
    set_attribute,
)

from src.modal.budget_window_state import (
    build_batch_status_response,
    create_initial_batch_state,
    get_batch_job_seed,
    get_batch_job_state,
    put_batch_job_seed,
    put_batch_job_state,
)
from src.modal.gateway.auth import require_auth
from src.modal.gateway.errors import log_and_redact_exception
from src.modal.gateway.models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    PolicyEngineBundle,
    SimulationRequest,
)
from src.modal.gateway.responses import (
    batch_status_response,
    failed_job_response,
    running_job_response,
)
from policyengine_api_simulation.dataset_uri import (
    runtime_dataset_uri,
    select_dataset_revision,
)
from policyengine_api_simulation.hf_dataset import (
    HuggingFaceDatasetReferenceError,
)
from policyengine_api_simulation.observability import SegmentName

logger = logging.getLogger(__name__)

router = APIRouter()
JOB_METADATA_DICT_NAME = "simulation-api-job-metadata"
POLICYENGINE_VERSION_DICT_NAME = "simulation-api-policyengine-versions"
ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
ROUTING_STATE_ACTIVE_KEY = "active"
SUPPORTED_ROUTE_KINDS = ("policyengine", "us", "uk")


@dataclass(frozen=True)
class RouteResolution:
    app_name: str
    response_version: str
    policyengine_version: str | None
    bundle_manifest: dict


def _job_metadata_store():
    return modal.Dict.from_name(JOB_METADATA_DICT_NAME, create_if_missing=True)


def _record_not_found(message: str) -> None:
    record_error(
        LookupError(message),
        handled=True,
        status_code=404,
        include_stack=False,
    )


def _split_requested_revision(requested_data: str) -> tuple[str, str | None]:
    if "@" not in requested_data:
        return requested_data, None
    dataset_name, revision = requested_data.rsplit("@", maxsplit=1)
    if not dataset_name or not revision:
        raise ValueError(f"Invalid dataset revision reference: {requested_data}")
    return dataset_name, revision


def _country_bundle_data_version(country_bundle: dict) -> str | None:
    data_version = country_bundle.get("data_version")
    return data_version if isinstance(data_version, str) else None


def _country_bundle_data_artifact_revision(country_bundle: dict) -> str | None:
    artifact_revision = country_bundle.get("data_artifact_revision")
    return artifact_revision if isinstance(artifact_revision, str) else None


def _without_revision(dataset_uri: str) -> str:
    return _split_requested_revision(dataset_uri)[0]


def _revision_from_dataset_uri(dataset_uri: str | None) -> str | None:
    if not isinstance(dataset_uri, str) or "@" not in dataset_uri:
        return None
    _, revision = _split_requested_revision(dataset_uri)
    return revision


def _bundle_response_data_version(
    *,
    country_bundle: dict,
    requested_dataset: str | None,
    requested_data_version: str | None,
    resolved_dataset: str | None,
) -> str | None:
    if requested_data_version is not None:
        return requested_data_version
    if _revision_from_dataset_uri(requested_dataset) is not None:
        return _revision_from_dataset_uri(resolved_dataset)
    data_version = country_bundle.get("data_version")
    if isinstance(data_version, str):
        return data_version
    return _revision_from_dataset_uri(resolved_dataset)


def _bundle_certified_hf_uri_roots(country_bundle: dict) -> set[str]:
    roots: set[str] = set()
    default_uri = country_bundle.get("default_dataset_uri")
    if isinstance(default_uri, str) and default_uri.startswith("hf://"):
        roots.add(_without_revision(default_uri))

    for key in ("dataset_uris", "dataset_aliases"):
        values = country_bundle.get(key)
        if not isinstance(values, dict):
            continue
        for value in values.values():
            if isinstance(value, str) and value.startswith("hf://"):
                roots.add(_without_revision(value))
    return roots


def _is_bundle_certified_hf_uri(country_bundle: dict, dataset_uri: str) -> bool:
    if not dataset_uri.startswith("hf://"):
        return False
    return _without_revision(dataset_uri) in _bundle_certified_hf_uri_roots(
        country_bundle
    )


def _resolve_dataset_uri_from_app_bundle(
    *,
    app_bundle: dict,
    country: str,
    requested_data: str | None,
    requested_data_version: str | None = None,
) -> str | None:
    country_bundle = app_bundle.get(country.lower())

    if requested_data is None:
        if not isinstance(country_bundle, dict):
            return None
        default_uri = country_bundle.get("default_dataset_uri")
        if not isinstance(default_uri, str):
            return None
        return runtime_dataset_uri(
            default_uri,
            default_revision=_country_bundle_data_version(country_bundle),
            override_revision=requested_data_version,
            artifact_revision=_country_bundle_data_artifact_revision(country_bundle),
            validate_hf=False,
        )

    requested_without_revision, requested_revision = _split_requested_revision(
        requested_data
    )
    revision = select_dataset_revision(
        requested_revision=requested_revision,
        requested_data_version=requested_data_version,
    )
    bundle_data_version = (
        _country_bundle_data_version(country_bundle)
        if isinstance(country_bundle, dict)
        else None
    )
    artifact_revision = (
        _country_bundle_data_artifact_revision(country_bundle)
        if isinstance(country_bundle, dict)
        else None
    )

    if "://" in requested_without_revision:
        runtime_input = (
            requested_data
            if requested_revision is not None and requested_data_version is None
            else requested_without_revision
        )
        if requested_without_revision.startswith("hf://"):
            validate_hf = not (
                isinstance(country_bundle, dict)
                and _is_bundle_certified_hf_uri(
                    country_bundle, requested_without_revision
                )
            )
            return runtime_dataset_uri(
                runtime_input,
                default_revision=bundle_data_version,
                override_revision=(
                    revision if requested_data_version is not None else None
                ),
                artifact_revision=artifact_revision,
                validate_hf=validate_hf,
            )
        if requested_without_revision.startswith("gs://"):
            return runtime_dataset_uri(
                runtime_input,
                default_revision=revision,
            )
        return requested_data

    if not isinstance(country_bundle, dict):
        return requested_data

    # Older Modal snapshots may contain aliases. Newly published bundle snapshots
    # resolve direct .py dataset names through dataset_uris instead.
    aliases = country_bundle.get("dataset_aliases")
    if not isinstance(aliases, dict):
        aliases = {}
    dataset_name = aliases.get(requested_without_revision, requested_without_revision)

    if "://" in dataset_name:
        return runtime_dataset_uri(
            dataset_name,
            default_revision=bundle_data_version,
            override_revision=revision,
            artifact_revision=artifact_revision,
            validate_hf=not _is_bundle_certified_hf_uri(country_bundle, dataset_name),
        )

    dataset_uris = country_bundle.get("dataset_uris")
    if not isinstance(dataset_uris, dict):
        return requested_data
    dataset_uri = dataset_uris.get(dataset_name)
    if not isinstance(dataset_uri, str):
        return requested_data
    return runtime_dataset_uri(
        dataset_uri,
        default_revision=bundle_data_version,
        override_revision=revision,
        artifact_revision=artifact_revision,
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


def _policyengine_version_for_app(state: dict, app_name: str) -> str | None:
    for policyengine_version, routed_app in _routing_state_routes(
        state, "policyengine"
    ).items():
        if routed_app == app_name:
            return (
                policyengine_version if isinstance(policyengine_version, str) else None
            )

    for policyengine_version, manifest in _routing_state_bundles(state).items():
        if isinstance(manifest, dict) and manifest.get("app_name") == app_name:
            return (
                policyengine_version if isinstance(policyengine_version, str) else None
            )

    return None


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
    policyengine_version = _policyengine_version_for_app(state, app_name)
    return RouteResolution(
        app_name=app_name,
        response_version=version,
        policyengine_version=policyengine_version,
        bundle_manifest=_bundle_manifest(state, policyengine_version),
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
    )


def _build_policyengine_bundle(
    country: str,
    resolution: RouteResolution,
    payload: dict,
) -> PolicyEngineBundle:
    app_bundle = resolution.bundle_manifest
    country_bundle = app_bundle.get(country.lower())
    if not isinstance(country_bundle, dict):
        country_bundle = {}
    dataset = payload.get("data")
    requested_data_version = payload.get("data_version")
    requested_dataset = dataset if isinstance(dataset, str) else None
    requested_data_version = (
        requested_data_version if isinstance(requested_data_version, str) else None
    )
    resolved_dataset = _resolve_dataset_uri_from_app_bundle(
        app_bundle=app_bundle,
        country=country,
        requested_data=requested_dataset,
        requested_data_version=requested_data_version,
    )
    data_version = _bundle_response_data_version(
        country_bundle=country_bundle,
        requested_dataset=requested_dataset,
        requested_data_version=requested_data_version,
        resolved_dataset=resolved_dataset,
    )
    model_version = country_bundle.get("model_version") or resolution.response_version
    policyengine_version = app_bundle.get(
        "policyengine_version", resolution.policyengine_version
    )
    return PolicyEngineBundle(
        model_version=str(model_version),
        policyengine_version=(
            str(policyengine_version) if isinstance(policyengine_version, str) else None
        ),
        data_version=str(data_version) if isinstance(data_version, str) else None,
        dataset=resolved_dataset,
    )


def _serialize_job_metadata(
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
    run_id: str | None = None,
) -> dict:
    return {
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": bundle.model_dump(),
        "run_id": run_id,
    }


def _build_budget_window_parent_payload(
    request: BudgetWindowBatchRequest,
    *,
    resolved_version: str,
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
) -> dict:
    payload = request.model_dump(
        exclude={"version", "policyengine_version", "telemetry"},
        mode="json",
        exclude_none=True,
    )
    payload["version"] = resolved_version
    if request.telemetry is not None:
        payload["_telemetry"] = request.telemetry.model_dump(mode="json")
    payload["_metadata"] = {
        "resolved_version": resolved_version,
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": bundle.model_dump(mode="json"),
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
async def submit_simulation(request: SimulationRequest):
    """
    Submit a simulation job.

    Matches the existing Cloud Run API endpoint path.
    Routes to the appropriate app based on country and version params.
    Returns immediately with job_id for polling.
    """
    set_attribute("country", request.country)
    set_attribute("scope", request.scope)
    set_attribute("run_id", request.telemetry.run_id if request.telemetry else None)
    try:
        with segment(SegmentName.ROUTE_RESOLUTION):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
    except ValueError as e:
        record_error(e, handled=True, status_code=400, include_stack=False)
        raise HTTPException(status_code=400, detail=str(e))

    set_attribute("resolved_app_name", route.app_name)
    set_attribute("resolved_version", route.response_version)
    set_attribute("policyengine_version", route.policyengine_version)

    with segment(SegmentName.REQUEST_PARSE):
        payload = request.model_dump(
            exclude={"version", "policyengine_version", "telemetry"},
            mode="json",
            exclude_none=True,
        )
    run_id = request.telemetry.run_id if request.telemetry else None
    if request.telemetry is not None:
        payload["_telemetry"] = request.telemetry.model_dump(mode="json")

    try:
        with segment(SegmentName.POLICYENGINE_BUNDLE):
            bundle = _build_policyengine_bundle(
                request.country,
                route,
                payload,
            )
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        record_error(exc, handled=True, status_code=400, include_stack=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "Routing %s:%s to app %s (run_id=%s)",
        request.country,
        route.response_version,
        route.app_name,
        run_id,
    )

    # Get function reference from the target app
    with segment(SegmentName.MODAL_FUNCTION_LOOKUP):
        sim_func = modal.Function.from_name(route.app_name, "run_simulation")

    # Spawn the job (returns immediately)
    with segment(SegmentName.MODAL_FUNCTION_SPAWN):
        call = sim_func.spawn(payload)

    set_attribute("job_id", call.object_id)

    job_metadata = _serialize_job_metadata(route.app_name, bundle, run_id)
    with segment(SegmentName.MODAL_JOB_METADATA_WRITE):
        _job_metadata_store()[call.object_id] = job_metadata

    with segment(SegmentName.RESPONSE_SERIALIZATION):
        return JobSubmitResponse(
            job_id=call.object_id,
            status="submitted",
            poll_url=f"/jobs/{call.object_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
            run_id=run_id,
        )


@router.post(
    "/simulate/economy/budget-window",
    response_model=BudgetWindowBatchSubmitResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def submit_budget_window_batch(request: BudgetWindowBatchRequest):
    """
    Submit a budget-window batch job.
    """
    set_attribute("country", request.country)
    set_attribute("run_id", request.telemetry.run_id if request.telemetry else None)
    try:
        with segment(SegmentName.ROUTE_RESOLUTION):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
    except ValueError as e:
        record_error(e, handled=True, status_code=400, include_stack=False)
        raise HTTPException(status_code=400, detail=str(e))

    set_attribute("resolved_app_name", route.app_name)
    set_attribute("resolved_version", route.response_version)
    set_attribute("policyengine_version", route.policyengine_version)

    try:
        with segment(SegmentName.POLICYENGINE_BUNDLE):
            bundle = _build_policyengine_bundle(
                request.country,
                route,
                request.model_dump(mode="json"),
            )
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        record_error(exc, handled=True, status_code=400, include_stack=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with segment(SegmentName.REQUEST_PARSE):
        payload = _build_budget_window_parent_payload(
            request,
            resolved_version=route.response_version,
            resolved_app_name=route.app_name,
            bundle=bundle,
        )

    with segment(SegmentName.MODAL_FUNCTION_LOOKUP):
        batch_func = modal.Function.from_name(route.app_name, "run_budget_window_batch")
    with segment(SegmentName.MODAL_FUNCTION_SPAWN):
        call = batch_func.spawn(payload)
    batch_job_id = call.object_id
    set_attribute("batch_job_id", batch_job_id)

    seed_state = create_initial_batch_state(
        batch_job_id=batch_job_id,
        request=request,
        resolved_version=route.response_version,
        resolved_app_name=route.app_name,
        bundle=bundle,
    )
    with segment(SegmentName.MODAL_JOB_METADATA_WRITE):
        put_batch_job_seed(seed_state)

    with segment(SegmentName.RESPONSE_SERIALIZATION):
        return BudgetWindowBatchSubmitResponse(
            batch_job_id=batch_job_id,
            status=seed_state.status,
            poll_url=f"/budget-window-jobs/{batch_job_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
            run_id=seed_state.run_id,
        )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_job_status(job_id: str):
    """
    Poll for job status.

    Returns:
        - 200 with status="complete" and result when done
        - 202 with status="running" while in progress
        - 500 with status="failed" and error on failure
        - 404 if job_id not found
    """
    set_attribute("job_id", job_id)
    with segment(SegmentName.MODAL_JOB_METADATA_READ):
        job_metadata = _job_metadata_store().get(job_id)
    if job_metadata is None:
        _record_not_found(f"Job not found: {job_id}")
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    try:
        with segment(SegmentName.MODAL_JOB_STATUS_POLL):
            call = modal.FunctionCall.from_id(job_id)
    except Exception as exc:
        if _is_modal_job_not_found(exc):
            record_error(exc, handled=True, status_code=404, include_stack=False)
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        record_error(exc, handled=False, status_code=500)
        raise

    try:
        with segment(SegmentName.MODAL_JOB_STATUS_POLL):
            result = call.get(timeout=0)
        with segment(SegmentName.RESPONSE_SERIALIZATION):
            return JobStatusResponse(
                status="complete", result=result, **(job_metadata or {})
            )
    except TimeoutError:
        with segment(SegmentName.RESPONSE_SERIALIZATION):
            return running_job_response(job_metadata)
    except Exception as exc:
        if _is_modal_job_not_found(exc):
            record_error(exc, handled=True, status_code=404, include_stack=False)
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        redacted = log_and_redact_exception(
            exc,
            scope="simulation_job_status",
            context={"job_id": job_id},
        )
        with segment(SegmentName.RESPONSE_SERIALIZATION):
            return failed_job_response(error=redacted, job_metadata=job_metadata)


@router.get(
    "/budget-window-jobs/{batch_job_id}",
    response_model=BudgetWindowBatchStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_budget_window_job_status(batch_job_id: str):
    """
    Poll for budget-window batch status.
    """
    set_attribute("batch_job_id", batch_job_id)
    with segment(SegmentName.BUDGET_WINDOW_STATE_LOAD):
        state = get_batch_job_state(batch_job_id)
    if state is not None:
        with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
            return batch_status_response(build_batch_status_response(state))

    with segment(SegmentName.BUDGET_WINDOW_STATE_LOAD):
        seed_state = get_batch_job_seed(batch_job_id)
    if seed_state is None:
        _record_not_found(f"Budget-window job not found: {batch_job_id}")
        raise HTTPException(
            status_code=404, detail=f"Budget-window job not found: {batch_job_id}"
        )

    try:
        with segment(SegmentName.MODAL_JOB_STATUS_POLL):
            call = modal.FunctionCall.from_id(batch_job_id)
    except Exception as exc:
        if _is_modal_job_not_found(exc):
            record_error(exc, handled=True, status_code=404, include_stack=False)
        else:
            record_error(exc, handled=True, status_code=500)
        with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
            return batch_status_response(build_batch_status_response(seed_state))

    try:
        with segment(SegmentName.MODAL_JOB_STATUS_POLL):
            result = call.get(timeout=0)
    except TimeoutError:
        with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
            return batch_status_response(build_batch_status_response(seed_state))
    except Exception as exc:
        # Persist the failure so subsequent polls don't resurrect the
        # "submitted" status from the seed store (#448). We deliberately
        # overwrite the main job store entry as well as the seed so either
        # lookup path observes the terminal failed state.
        redacted = log_and_redact_exception(
            exc,
            scope="budget_window_parent_call",
            context={"batch_job_id": batch_job_id},
        )
        seed_state.status = "failed"
        seed_state.error = redacted
        with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
            put_batch_job_state(seed_state)
            put_batch_job_seed(seed_state)
        with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
            return batch_status_response(build_batch_status_response(seed_state))

    with segment(SegmentName.BUDGET_WINDOW_RESULT_PARSE):
        response = BudgetWindowBatchStatusResponse.model_validate(result)
    with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
        return batch_status_response(response)


@router.get("/versions")
async def list_versions():
    """List all available routing versions."""
    with segment(SegmentName.ROUTE_RESOLUTION):
        state = _active_routing_state()
    if state:
        return {
            kind: _version_map_from_state(state, kind) for kind in SUPPORTED_ROUTE_KINDS
        }

    with segment(SegmentName.MODAL_DICT_READ):
        policyengine_dict = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        us_dict = modal.Dict.from_name("simulation-api-us-versions")
        uk_dict = modal.Dict.from_name("simulation-api-uk-versions")
    return {
        "policyengine": (
            dict(policyengine_dict) if policyengine_dict is not None else {}
        ),
        "us": dict(us_dict),
        "uk": dict(uk_dict),
    }


def _version_map_from_state(state: dict, kind: str) -> dict:
    versions = dict(_routing_state_routes(state, kind))
    latest = _routing_state_latest(state, kind)
    if latest is not None:
        versions["latest"] = latest
    return versions


@router.get("/versions/{kind}")
async def get_country_versions(kind: str):
    """Get available versions for policyengine, US, or UK routing."""
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROUTE_KINDS:
        _record_not_found(f"Unknown version kind: {kind}")
        raise HTTPException(status_code=404, detail=f"Unknown version kind: {kind}")

    with segment(SegmentName.ROUTE_RESOLUTION):
        state = _active_routing_state()
    if state:
        return _version_map_from_state(state, kind_lower)

    if kind_lower == "policyengine":
        with segment(SegmentName.MODAL_DICT_READ):
            version_dict = _optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        return dict(version_dict) if version_dict is not None else {}

    with segment(SegmentName.MODAL_DICT_READ):
        version_dict = modal.Dict.from_name(f"simulation-api-{kind_lower}-versions")
    return dict(version_dict)


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.post("/ping", response_model=PingResponse)
async def ping(request: PingRequest) -> PingResponse:
    """
    Verify the API is able to receive and process requests.
    Matches the policyengine_fastapi.ping endpoint for test compatibility.
    """
    return PingResponse(incremented=request.value + 1)
