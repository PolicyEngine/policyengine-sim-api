"""Read job metadata and routing from Modal, and resolve a request's route."""

import modal

from policyengine_simulation_gateway.route_resolution import (
    RouteResolution,
    policyengine_version_from_app_name,
    resolve_from_active_state,
)

JOB_METADATA_DICT_NAME = "simulation-api-job-metadata"
POLICYENGINE_VERSION_DICT_NAME = "simulation-api-policyengine-versions"
ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
ROUTING_STATE_ACTIVE_KEY = "active"


def job_metadata_store() -> "modal.Dict":
    """Return the Modal Dict of submitted jobs' metadata, creating it if missing."""
    return modal.Dict.from_name(JOB_METADATA_DICT_NAME, create_if_missing=True)


def _modal_exception_class(name: str) -> type[BaseException] | None:
    exception_module = getattr(modal, "exception", None)
    if exception_module is None:
        return None
    return getattr(exception_module, name, None)


def _is_modal_exception(exc: BaseException, name: str) -> bool:
    exception_class = _modal_exception_class(name)
    return exception_class is not None and isinstance(exc, exception_class)


def is_modal_job_not_found(exc: BaseException) -> bool:
    """Whether ``exc`` is Modal's error for an unknown job or an expired output."""
    return _is_modal_exception(exc, "NotFoundError") or _is_modal_exception(
        exc, "OutputExpiredError"
    )


def optional_modal_dict(name: str) -> "modal.Dict | None":
    """Return the named Modal Dict, or None when it doesn't exist."""
    try:
        return modal.Dict.from_name(name)
    except KeyError:
        return None
    except Exception as exc:
        if _is_modal_exception(exc, "NotFoundError"):
            return None
        raise


def active_routing_state() -> dict:
    """Return the active routing state from Modal, or an empty dict without one."""
    store = optional_modal_dict(ROUTING_STATE_DICT_NAME)
    if store is None:
        return {}
    state = store.get(ROUTING_STATE_ACTIVE_KEY)
    return state if isinstance(state, dict) else {}


def _resolve_from_legacy_dicts(
    *,
    country: str,
    version: str | None,
    policyengine_version: str | None,
) -> RouteResolution:
    if policyengine_version is not None:
        policyengine_versions = optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
        if policyengine_versions is None:
            msg = f"Unknown policyengine.py version {policyengine_version}"
            raise ValueError(msg)
        try:
            app_name = policyengine_versions[policyengine_version]
        except KeyError as error:
            msg = f"Unknown policyengine.py version {policyengine_version}"
            raise ValueError(msg) from error
        return RouteResolution(
            app_name=app_name,
            response_version=policyengine_version,
            policyengine_version=policyengine_version,
            bundle_manifest={},
        )

    country_versions = modal.Dict.from_name(f"simulation-api-{country}-versions")
    resolved_version = country_versions["latest"] if version is None else version

    try:
        app_name = country_versions[resolved_version]
    except KeyError as error:
        policyengine_versions = optional_modal_dict(POLICYENGINE_VERSION_DICT_NAME)
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
        msg = f"Unknown version {resolved_version} for country {country}"
        raise ValueError(msg) from error

    return RouteResolution(
        app_name=app_name,
        response_version=resolved_version,
        policyengine_version=policyengine_version_from_app_name(app_name),
        bundle_manifest={},
        country_model_version=resolved_version,
        route_provenance="legacy-country-dict",
    )


def resolve_route(
    country: str,
    version: str | None,
    policyengine_version: str | None = None,
) -> RouteResolution:
    """Resolve a country/package or policyengine.py version to a Modal app."""
    country_lower = country.lower()
    if country_lower not in ("us", "uk"):
        msg = f"Unknown country: {country}"
        raise ValueError(msg)

    state = active_routing_state()
    if state:
        return resolve_from_active_state(
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


def get_app_name(country: str, version: str | None) -> tuple[str, str]:
    """Backward-compatible helper for tests and API v1 health checks."""
    resolution = resolve_route(country, version)
    return resolution.app_name, resolution.response_version
