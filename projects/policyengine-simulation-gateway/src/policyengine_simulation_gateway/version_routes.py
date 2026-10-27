"""The version, health and ping routes."""

import modal
from fastapi import HTTPException, Request
from policyengine_simulation_contract.gateway_models import (
    HealthResponse,
    PingRequest,
    PingResponse,
    VersionMap,
    VersionsResponse,
)
from policyengine_simulation_contract.spm import SPMCapability
from policyengine_simulation_observability import scopes
from policyengine_simulation_observability.stages import ANNUAL_IMPACT_STAGES, Stage

from policyengine_simulation_gateway.bundle_payloads import bundle_identities
from policyengine_simulation_gateway.modal_routing import (
    POLICYENGINE_VERSION_DICT_NAME,
    active_routing_state,
    optional_modal_dict,
)
from policyengine_simulation_gateway.route_resolution import (
    SUPPORTED_ROUTE_KINDS,
    routing_state_bundles,
    routing_state_latest,
    routing_state_latest_since,
    routing_state_published_at,
    routing_state_routes,
)
from policyengine_simulation_gateway.routes_common import (
    logger,
    observability_runtime,
    record_not_found,
    router,
)


@router.get("/versions")
async def list_versions(request: Request) -> VersionsResponse:
    """List all available routing versions."""
    runtime = observability_runtime(request)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
        state = active_routing_state()
    if state:
        capabilities = {}
        for name, bundle in routing_state_bundles(state).items():
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
            bundles=bundle_identities(state),
            published_at=routing_state_published_at(state),
            latest_since=routing_state_latest_since(state),
        )

    # ``Dict.from_name`` is a lazy handle; the RPCs fire during the
    # ``dict(...)`` iterations, so those are what the segment must cover.
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
        policyengine_dict = optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        us_dict = modal.Dict.from_name("simulation-api-us-versions")
        uk_dict = modal.Dict.from_name("simulation-api-uk-versions")
        return VersionsResponse(
            policyengine=VersionMap(
                dict(policyengine_dict) if policyengine_dict is not None else {}
            ),
            us=VersionMap(dict(us_dict)),
            uk=VersionMap(dict(uk_dict)),
        )


def _version_map_from_state(state: dict, kind: str) -> VersionMap:
    """Return ``kind``'s routes and latest version from the active routing state.

    The state is read from a Modal Dict, so the helpers check each field's type.
    """
    versions: dict[str, str] = dict(routing_state_routes(state, kind))
    latest = routing_state_latest(state, kind)
    if latest is not None:
        versions["latest"] = latest
    return VersionMap(versions)


@router.get("/versions/{kind}")
async def get_country_versions(kind: str, request: Request) -> VersionMap:
    """Get available versions for policyengine, US, or UK routing."""
    runtime = observability_runtime(request)
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROUTE_KINDS:
        record_not_found(runtime, f"Unknown version kind: {kind}")
        raise HTTPException(status_code=404, detail=f"Unknown version kind: {kind}")

    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.ROUTE_RESOLUTION)):
        state = active_routing_state()
    if state:
        return _version_map_from_state(state, kind_lower)

    # The ``dict(...)`` iteration is where the Modal Dict RPCs happen; the
    # from_name handle itself is lazy and free.
    if kind_lower == "policyengine":
        with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
            version_dict = optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
            return VersionMap(dict(version_dict) if version_dict is not None else {})

    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.MODAL_DICT_READ)):
        version_dict = modal.Dict.from_name(f"simulation-api-{kind_lower}-versions")
        return VersionMap(dict(version_dict))


@router.get("/health")
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse()


@router.post("/ping")
async def ping(request: PingRequest) -> PingResponse:
    """Verify the API can receive and process requests.

    Responds with the request's ``value`` plus one as ``incremented``.
    """
    return PingResponse(incremented=request.value + 1)
