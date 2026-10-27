"""The routes that submit simulation and budget-window jobs."""

from datetime import UTC, datetime

import modal
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.budget_window_state import (
    BatchRoute,
    create_initial_batch_state,
    put_batch_job_seed,
)
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchSubmitResponse,
    GatewayRequestBase,
    JobSubmitResponse,
    PolicyEngineBundle,
    SimulationRequest,
)
from policyengine_simulation_contract.hf_dataset import HuggingFaceDatasetReferenceError
from policyengine_simulation_contract.job_progress import (
    JOB_PROGRESS_CAPABILITY_KEY,
    supported_job_progress_capability,
)
from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_contract.local_area_breakdown import (
    LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY,
    supported_local_area_breakdown_capability,
)
from policyengine_simulation_contract.spm import (
    SPMRouteVersions,
    SPMSelection,
    resolve_spm_selection,
    spm_error_detail,
)
from policyengine_simulation_observability import scopes
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    resolve_observability_id,
)
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    BUDGET_WINDOW_STAGES,
    Stage,
)

from policyengine_simulation_gateway.auth import require_auth
from policyengine_simulation_gateway.bundle_payloads import (
    build_policyengine_bundle,
    bundle_payload,
)
from policyengine_simulation_gateway.modal_routing import (
    job_metadata_store,
    resolve_route,
)
from policyengine_simulation_gateway.responses import failed_job_response
from policyengine_simulation_gateway.route_resolution import RouteResolution
from policyengine_simulation_gateway.routes_common import (
    bind_observability_id,
    logger,
    observability_headers,
    observability_runtime,
    router,
)


def _certified_model_version(country: str, route: RouteResolution) -> str | None:
    """Return the country model version the registry states for this route.

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


def _resolve_request_spm(
    request: GatewayRequestBase,
    bundle: PolicyEngineBundle,
    route: RouteResolution,
) -> JsonObject | None:
    selection = resolve_spm_selection(
        request.country,
        request.spm,
        capability=bundle.spm,
        route_versions=SPMRouteVersions(
            policyengine_version=bundle.policyengine_version,
            model_version=_certified_model_version(request.country, route),
            route_provenance=route.route_provenance,
        ),
    )
    if selection is not None:
        request.spm = SPMSelection.model_validate(selection)
    return selection


def _serialize_job_metadata(
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
    observability_id: str | None = None,
    *,
    submitted_at: datetime | None = None,
    job_progress: int | None = None,
) -> dict:
    return {
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": bundle_payload(bundle),
        "observability_id": observability_id,
        "submitted_at": None if submitted_at is None else submitted_at.isoformat(),
        JOB_PROGRESS_CAPABILITY_KEY: job_progress,
    }


def _route_job_progress_capability(route: RouteResolution) -> int | None:
    manifest = route.bundle_manifest
    if not isinstance(manifest, dict):
        return None
    return supported_job_progress_capability(manifest.get(JOB_PROGRESS_CAPABILITY_KEY))


def _require_route_serves_breakdown(
    request: SimulationRequest, route: RouteResolution
) -> None:
    """Refuse a breakdown for a route whose worker does not declare serving it.

    Workers built before local-area breakdowns ignore the field and would
    return a national result, so only a bundle manifest that declares
    ``LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY`` may receive one. Routes from the
    legacy version dicts have no manifest and are refused too.
    """
    if request.breakdown is None:
        return
    manifest = route.bundle_manifest
    declared = (
        manifest.get(LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY)
        if isinstance(manifest, dict)
        else None
    )
    if supported_local_area_breakdown_capability(declared) is not None:
        return
    version = route.policyengine_version or "an unknown version"
    msg = (
        f"breakdown={request.breakdown} is not supported by the worker for "
        f"policyengine.py {version} ({route.app_name}); its routing manifest "
        f"declares no {LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY} capability"
    )
    raise ValueError(msg)


def _start_workflow_observability(
    http_request: Request,
    runtime: ObservabilityRuntime,
) -> str:
    observability_id = resolve_observability_id(
        getattr(http_request.state, "incoming_observability_id", None)
    )
    bind_observability_id(http_request, runtime, observability_id)
    return observability_id


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
    if request.spm is not None:
        payload["spm"] = request.spm.model_dump(mode="json")
    payload["version"] = resolved_version
    if request.telemetry is not None:
        payload["_telemetry"] = request.telemetry.model_dump(mode="json")
    payload["_metadata"] = {
        "resolved_version": resolved_version,
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": bundle_payload(bundle, mode="json"),
    }
    return payload


@router.post(
    "/simulate/economy/comparison",
    response_model=JobSubmitResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def submit_simulation(
    request: SimulationRequest,
    http_request: Request,
    response: Response,
) -> JobSubmitResponse | JSONResponse:
    """Submit a simulation job.

    Matches the existing Cloud Run API endpoint path.
    Routes to the appropriate app based on country and version params.
    Returns immediately with job_id for polling.
    """
    runtime = observability_runtime(http_request)
    observability_id = _start_workflow_observability(http_request, runtime)
    response.headers[OBSERVABILITY_ID_HEADER] = observability_id
    runtime.set_context(
        country=request.country,
        scope=request.scope,
        observability_id=observability_id,
        breakdown=request.breakdown,
    )
    try:
        with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
        _require_route_serves_breakdown(request, route)
    except ValueError as e:
        runtime.record_exception(e, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(e)) from e

    runtime.set_context(
        resolved_app_name=route.app_name,
        resolved_version=route.response_version,
        policyengine_version=route.policyengine_version,
    )

    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.REQUEST_PARSE)):
        payload = request.model_dump(
            exclude={"version", "policyengine_version", "telemetry"},
            mode="json",
            exclude_none=True,
        )
    if request.telemetry is not None:
        payload["_telemetry"] = request.telemetry.model_dump(mode="json")

    try:
        with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.POLICYENGINE_BUNDLE)):
            bundle = build_policyengine_bundle(
                request.country,
                route,
            )
        _resolve_request_spm(request, bundle, route)
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message,
                errors=[detail.model_dump()],
                headers=observability_headers(observability_id),
            )
        runtime.record_exception(exc, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.spm is not None:
        payload["spm"] = request.spm.model_dump(mode="json")
    observability_context = runtime.capture_context()

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
    submitted_at = datetime.now(UTC)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.MODAL_FUNCTION_SPAWN)):
        sim_func = modal.Function.from_name(route.app_name, "run_simulation")
        call = sim_func.spawn(
            payload,
            observability_context=observability_context,
        )

    runtime.set_context(job_id=call.object_id)

    job_metadata = _serialize_job_metadata(
        route.app_name,
        bundle,
        observability_id,
        submitted_at=submitted_at,
        job_progress=_route_job_progress_capability(route),
    )
    with scopes.span(
        runtime, ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_METADATA_WRITE)
    ):
        job_metadata_store()[call.object_id] = job_metadata

    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
        return JobSubmitResponse(
            job_id=call.object_id,
            status="submitted",
            poll_url=f"/jobs/{call.object_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
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
    response: Response,
) -> BudgetWindowBatchSubmitResponse | JSONResponse:
    """Submit a budget-window batch job."""
    runtime = observability_runtime(http_request)
    observability_id = _start_workflow_observability(http_request, runtime)
    response.headers[OBSERVABILITY_ID_HEADER] = observability_id
    runtime.set_context(
        country=request.country,
        observability_id=observability_id,
    )
    try:
        with scopes.span(runtime, BUDGET_WINDOW_STAGES.name(Stage.ROUTE_RESOLUTION)):
            route = resolve_route(
                request.country,
                request.version,
                request.policyengine_version,
            )
    except ValueError as e:
        runtime.record_exception(e, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(e)) from e

    runtime.set_context(
        resolved_app_name=route.app_name,
        resolved_version=route.response_version,
        policyengine_version=route.policyengine_version,
    )

    try:
        with scopes.span(runtime, BUDGET_WINDOW_STAGES.name(Stage.POLICYENGINE_BUNDLE)):
            bundle = build_policyengine_bundle(
                request.country,
                route,
            )
        _resolve_request_spm(request, bundle, route)
    except (ValueError, HuggingFaceDatasetReferenceError) as exc:
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message,
                errors=[detail.model_dump()],
                headers=observability_headers(observability_id),
            )
        runtime.record_exception(exc, handled=True, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with scopes.span(runtime, BUDGET_WINDOW_STAGES.name(Stage.REQUEST_PARSE)):
        payload = _build_budget_window_parent_payload(
            request,
            resolved_version=route.response_version,
            resolved_app_name=route.app_name,
            bundle=bundle,
        )

    # Lazy handle + spawn together: the RPC cost lands in ``spawn``.
    observability_context = runtime.capture_context()
    with scopes.span(runtime, BUDGET_WINDOW_STAGES.name(Stage.MODAL_FUNCTION_SPAWN)):
        batch_func = modal.Function.from_name(route.app_name, "run_budget_window_batch")
        call = batch_func.spawn(
            payload,
            observability_context=observability_context,
        )
    batch_job_id = call.object_id
    runtime.set_context(batch_job_id=batch_job_id)

    seed_state = create_initial_batch_state(
        batch_job_id=batch_job_id,
        request=request,
        route=BatchRoute(version=route.response_version, app_name=route.app_name),
        bundle=bundle,
        observability_id=observability_id,
    )
    with scopes.span(
        runtime, BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_METADATA_WRITE)
    ):
        put_batch_job_seed(seed_state)

    with scopes.span(runtime, BUDGET_WINDOW_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
        return BudgetWindowBatchSubmitResponse(
            batch_job_id=batch_job_id,
            # A new batch's seed state is always submitted.
            status="submitted",
            poll_url=f"/budget-window-jobs/{batch_job_id}",
            country=request.country,
            version=route.response_version,
            resolved_app_name=route.app_name,
            policyengine_bundle=bundle,
        )
