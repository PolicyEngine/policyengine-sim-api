"""SPM selection and receipts for v2 household calculations.

This reproduces the Public API's household SPM handling, so that a v2
household result and error carry the same SPM codes, messages and receipt:

- ``resolve_household_spm_selection`` merges a caller's selection over the
  installed bundle's certified defaults, with the Public API's error codes;
- ``household_spm_error_detail`` recognizes the SPM errors that end a
  calculation;
- ``household_spm_provenance`` reduces the country model's receipt to the
  compact public receipt and checks it matches the resolved selection.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.spm import (
    SPMErrorDetail,
    SPMInputError,
    SPMProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from spm_calculator.rolling_forecast import SPMForecast

# Country SPM errors about the household's own inputs. When the caller did not
# choose SPM settings, a value that needs one of these is left uncalculated
# instead of failing the calculation.
HOUSEHOLD_SPM_INPUT_ERROR_CODES = frozenset(
    {
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
    }
)
_SUPPORTED_COUNTY_VINTAGE = "2020"


def _readable_error(error: BaseException) -> str:
    """Render a validation error as a client-facing message, without its URL."""
    if not isinstance(error, ValidationError):
        return str(error)
    return "; ".join(
        ": ".join(
            part
            for part in (
                ".".join(str(item) for item in detail["loc"]),
                detail["msg"].removeprefix("Value error, "),
            )
            if part
        )
        for detail in error.errors()
    )


def _bundle_spm_configuration() -> object:
    """Return the installed bundle's SPM measurement configuration, if any."""
    from policyengine.bundle import get_current_bundle

    measurements = get_current_bundle().get("measurements")
    return measurements.get("spm") if isinstance(measurements, Mapping) else None


def _simulation_supports_spm(simulation_type: Any) -> bool:  # noqa: ANN401  # a country model's Simulation class, read by attribute only
    """Whether a country ``Simulation`` constructor implements canonical SPM."""
    try:
        parameters = inspect.signature(simulation_type).parameters
    except (TypeError, ValueError):
        parameters = {}
    return "spm" in parameters or (
        hasattr(simulation_type, "spm_config")
        and callable(getattr(simulation_type, "spm_provenance", None))
    )


def _us_model_supports_spm() -> bool:
    """Whether the installed US model implements canonical SPM.

    A model this build cannot import has no canonical SPM.
    """
    try:
        from policyengine_us import Simulation
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False
    return _simulation_supports_spm(Simulation)


def _forecast(expected_sha256: str) -> SPMForecast:
    from policyengine_simulation_executor.spm import certified_spm_forecast

    return certified_spm_forecast(expected_sha256)


def _certified_defaults(configured: object) -> tuple[SPMSelection, SPMForecast]:
    """Return the bundle's pinned SPM defaults and the forecast they name.

    Raises ValueError (or an import or loading error) when the configuration
    pins no artifact hash or scenario, or the US model lacks canonical SPM.
    """
    defaults = SPMSelection.model_validate(configured)
    if defaults.forecast_content_sha256 is None or defaults.scenario is None:
        msg = "The bundle must pin the SPM artifact hash and scenario"
        raise ValueError(msg)
    if not _us_model_supports_spm():
        msg = "The installed US model does not support canonical SPM"
        raise ValueError(msg)
    return defaults, _forecast(defaults.forecast_content_sha256)


def _validated_selection(values: dict[str, Any], forecast: SPMForecast) -> SPMSelection:
    """Validate merged settings against the forecast; raise ValueError if invalid."""
    resolved = SPMSelection.model_validate(values)
    forecast.entry(forecast.years[0], scenario=resolved.scenario, as_of=resolved.as_of)
    if resolved.county_vintage != _SUPPORTED_COUNTY_VINTAGE:
        msg = "Unsupported county vintage: use 2020"
        raise ValueError(msg)
    return resolved


def _validate_metro_selection(forecast: SPMForecast, selection: SPMSelection) -> None:
    """Raise SPM_GEOGRAPHY_UNAVAILABLE unless the area is in some forecast year."""
    for year in forecast.years:
        try:
            forecast.geography_factor(
                year,
                "renter",
                scenario=selection.scenario,
                kind="metro",
                geoid=selection.geography_id,
                as_of=selection.as_of,
            )
        except ValueError:
            continue
        return
    error_code = "SPM_GEOGRAPHY_UNAVAILABLE"
    raise SPMInputError(
        error_code,
        f"SPM area is unavailable in the selected artifact: {selection.geography_id}",
    )


def _unsupported_country(selection: SPMSelection | None) -> None:
    if selection is not None:
        error_code = "SPM_SETTINGS_UNSUPPORTED"
        raise SPMInputError(error_code, "SPM settings are only available for the US")


def _merged_selection(chosen: SPMSelection, defaults: SPMSelection) -> dict[str, Any]:
    values = {name: getattr(defaults, name) for name in SPMSelection.model_fields}
    values.update(chosen.model_dump(exclude_unset=True))
    if (
        "geography_kind" in chosen.model_fields_set
        and chosen.geography_kind != defaults.geography_kind
    ):
        values["geography_id"] = chosen.geography_id
    values["forecast_content_sha256"] = defaults.forecast_content_sha256
    values["scenario"] = chosen.scenario or defaults.scenario
    return values


def resolve_household_spm_selection(
    country: str,
    selection: SPMSelection | None,
    *,
    saved_household: bool,
) -> dict[str, Any] | None:
    """Resolve a household's SPM selection against the installed bundle.

    ``selection`` is None when the caller made no choice. Returns the complete
    resolved selection, or None when no SPM applies (any country but the US,
    or a bundle whose US model predates canonical SPM). Raises SPMInputError
    with the Public API's code and message otherwise. ``saved_household`` says
    the selection was read from a saved household, which changes only how an
    artifact hash the bundle no longer carries is reported.
    """
    if country != "us":
        _unsupported_country(selection)
        return None
    chosen = selection if selection is not None else SPMSelection()
    configured = _bundle_spm_configuration()
    if not isinstance(configured, Mapping):
        if _us_model_supports_spm():
            # A model that can run canonical SPM must never run it uncertified.
            error_code = "SPM_CONFIGURATION_UNAVAILABLE"
            raise SPMInputError(
                error_code,
                "The installed bundle has no certified SPM measurement configuration",
            )
        if selection is None:
            return None
        error_code = "SPM_SETTINGS_UNSUPPORTED"
        raise SPMInputError(
            error_code,
            "This US model bundle does not support canonical SPM settings",
        )
    try:
        defaults, forecast = _certified_defaults(configured)
    except (AttributeError, ImportError, ValueError, TypeError, OSError) as error:
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(error_code, _readable_error(error)) from error
    if chosen.forecast_content_sha256 not in (None, defaults.forecast_content_sha256):
        if saved_household:
            error_code = "SPM_CONFIGURATION_UNAVAILABLE"
            raise SPMInputError(
                error_code,
                "This deployment does not have the SPM artifact this household's "
                "saved selection was measured against",
            )
        error_code = "SPM_SETTINGS_INVALID"
        raise SPMInputError(
            error_code,
            "SPM selection does not match this bundle's artifact hash",
        )
    try:
        resolved = _validated_selection(_merged_selection(chosen, defaults), forecast)
    except ValueError as error:
        error_code = "SPM_SETTINGS_INVALID"
        raise SPMInputError(error_code, _readable_error(error)) from error
    if resolved.geography_kind == "metro":
        _validate_metro_selection(forecast, resolved)
    return resolved.model_dump(mode="json")


def household_spm_error_detail(error: BaseException) -> SPMErrorDetail | None:
    """Return the SPM error that ends a calculation, following wrapped causes.

    Recognizes this module's own ``SPMInputError`` and a country model's error
    carrying one of ``HOUSEHOLD_SPM_INPUT_ERROR_CODES``.
    """
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(current, SPMInputError) or (
            isinstance(current, ValueError) and code in HOUSEHOLD_SPM_INPUT_ERROR_CODES
        ):
            return SPMErrorDetail(code=str(code), message=str(current))
        current = current.__cause__ or current.__context__
    return None


def _required_text(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value:
        msg = f"Country SPM receipt requires {field_name}"
        raise ValueError(msg)
    return value


def _complete_settings(settings: object) -> SPMSelection | None:
    """Return the selection only when every field is present."""
    try:
        selection = SPMSelection.model_validate(settings)
    except ValidationError:
        return None
    if set(SPMSelection.model_fields) - selection.model_fields_set:
        return None
    return selection


def _compact_receipt(
    selection: SPMSelection, receipt: Mapping[str, object]
) -> SPMProvenance:
    """Reduce the country model's rich receipt to the compact public receipt."""
    forecast_sha256 = _required_text(receipt, "forecast_sha256")
    if (
        forecast_sha256 != selection.forecast_content_sha256
        or _required_text(receipt, "scenario") != selection.scenario
        or _required_text(receipt, "geography_kind") != selection.geography_kind
    ):
        msg = "Country SPM receipt differs from the SPM configuration"
        raise ValueError(msg)
    source_years = receipt.get("years")
    if not isinstance(source_years, Mapping):
        msg = "Country SPM receipt requires a years mapping"
        raise ValueError(msg)  # noqa: TRY004  # the caller maps only ValueError to SPM_CONFIGURATION_UNAVAILABLE
    if any(not isinstance(year, str) for year in source_years):
        msg = "Country SPM receipt years must be strings"
        raise ValueError(msg)
    source_versions = receipt.get("runtime_versions")
    if not isinstance(source_versions, Mapping):
        msg = "Country SPM receipt requires runtime_versions"
        raise ValueError(msg)  # noqa: TRY004  # the caller maps only ValueError to SPM_CONFIGURATION_UNAVAILABLE
    return build_spm_provenance(
        forecast_id=_required_text(receipt, "forecast_id"),
        forecast_sha256=forecast_sha256,
        selection=selection,
        years=tuple(sorted(source_years)),
        runtime_versions=SPMRuntimeVersions.model_validate(source_versions),
    )


def _country_receipt(simulation: Any) -> SPMProvenance:  # noqa: ANN401  # a country model's Simulation, read by attribute only
    """Reduce the simulation's receipt; raise ValueError if it cannot be certified."""
    selection = _complete_settings(simulation.spm_config)
    if selection is None:
        msg = "Country SPM configuration is incomplete"
        raise ValueError(msg)
    source = simulation.spm_provenance()
    if not isinstance(source, Mapping):
        msg = "Country SPM receipt must be an object"
        raise ValueError(msg)  # noqa: TRY004  # the caller maps only ValueError to SPM_CONFIGURATION_UNAVAILABLE
    return _compact_receipt(selection, source)


def _calculation_receipt(simulation: object) -> SPMProvenance | None:
    """Return the simulation's compact SPM receipt; None without SPM support."""
    if not hasattr(simulation, "spm_config"):
        return None
    try:
        return _country_receipt(simulation)
    except ValueError as error:
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "The installed country model reported an SPM receipt this API cannot "
            f"certify: {_readable_error(error)}",
        ) from error


def household_spm_provenance(
    simulation: object,
    expected_selection: Mapping[str, object] | None,
) -> SPMProvenance | None:
    """Return the simulation's SPM receipt, checked against the resolved selection.

    Raises SPMInputError for a receipt that cannot be certified, and
    ValueError for a receipt where none was expected, a missing receipt, or a
    receipt for another selection.
    """
    receipt = _calculation_receipt(simulation)
    if expected_selection is None:
        if receipt is None:
            return None
        msg = "Unexpected SPM provenance for a calculation without SPM"
        raise ValueError(msg)
    if receipt is None:
        msg = "The calculation produced no SPM receipt"
        raise ValueError(msg)
    receipt_selection = {
        "forecast_content_sha256": receipt.forecast_sha256,
        "scenario": receipt.scenario,
        "geography_kind": receipt.geography_kind,
        "geography_id": receipt.geography_id,
        "county_vintage": receipt.county_vintage,
        "as_of": receipt.as_of.isoformat() if receipt.as_of is not None else None,
    }
    if receipt_selection != dict(expected_selection):
        msg = "SPM calculation configuration differs from the request"
        raise ValueError(msg)
    return receipt
