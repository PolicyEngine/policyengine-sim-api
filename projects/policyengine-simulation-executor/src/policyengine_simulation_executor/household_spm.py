"""SPM selection and receipts for v2 household calculations.

This reproduces the Public API's household SPM handling, so that a v2
household result and error carry the same SPM codes, messages and receipt:

- ``resolve_household_spm_selection`` checks a caller's settings, exactly as
  sent, and merges them over the installed bundle's certified defaults, with
  the Public API's error codes;
- ``household_spm_error_detail`` recognizes the SPM errors that end a
  calculation;
- ``household_spm_provenance`` reduces the country model's receipt to the
  compact public receipt and checks it matches the resolved selection.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any, NoReturn

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
    from collections.abc import Callable

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
_CONFIGURATION_UNAVAILABLE = "SPM_CONFIGURATION_UNAVAILABLE"
_SETTINGS_INVALID = "SPM_SETTINGS_INVALID"
_SETTINGS_UNSUPPORTED = "SPM_SETTINGS_UNSUPPORTED"


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


def _current_bundle() -> object:
    """Return the installed PolicyEngine.py bundle manifest."""
    from policyengine.bundle import get_current_bundle

    return get_current_bundle()


def _bundle_spm_configuration() -> object:
    """Return the installed bundle's SPM measurement configuration, if any.

    Raises SPMInputError when the bundle manifest cannot be read.
    """
    try:
        bundle = _current_bundle()
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        bundle = None
    if not isinstance(bundle, Mapping):
        raise SPMInputError(
            _CONFIGURATION_UNAVAILABLE, "The installed bundle manifest is invalid"
        )
    measurements = bundle.get("measurements")
    return measurements.get("spm") if isinstance(measurements, Mapping) else None


def _simulation_supports_spm(simulation_type: Callable[..., object]) -> bool:
    """Whether a country ``Simulation`` constructor implements canonical SPM."""
    try:
        parameters = inspect.signature(simulation_type).parameters
    except (TypeError, ValueError):
        return False
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


@lru_cache(maxsize=4)
def certified_forecast(expected_sha256: str) -> SPMForecast:
    """Return the packaged SPM forecast whose content has ``expected_sha256``.

    Loaded once per process and digest; raises when the packaged forecast has
    another digest. The household path keeps this loader apart from the main
    path's SPM module.
    """
    from spm_calculator import load_forecast

    return load_forecast(expected_sha256=expected_sha256)


def _certified_defaults(configured: object) -> tuple[SPMSelection, SPMForecast]:
    """Return the bundle's pinned SPM defaults and the forecast they name.

    Raises ValueError (or an import or loading error) when the configuration
    pins no forecast hash or scenario, or the US model lacks canonical SPM.
    """
    defaults = SPMSelection.model_validate(configured)
    if defaults.forecast_content_sha256 is None or defaults.scenario is None:
        msg = "The bundle must pin the SPM artifact hash and scenario"
        raise ValueError(msg)
    if not _us_model_supports_spm():
        msg = "The installed US model does not support canonical SPM"
        raise ValueError(msg)
    return defaults, certified_forecast(defaults.forecast_content_sha256)


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


def _merged_selection(chosen: SPMSelection, defaults: SPMSelection) -> dict[str, Any]:
    """Overlay the caller's choices on the defaults, as the Public API does.

    Changing the geography kind also replaces an inherited geography ID, and
    the forecast hash always comes from the bundle.
    """
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


def _certified_configuration(*, selection_sent: bool) -> Mapping[str, object] | None:
    """Return the bundle's certified SPM settings, or None for a legacy bundle.

    Raises SPM_CONFIGURATION_UNAVAILABLE for an unreadable bundle or for a US
    model that can run canonical SPM without a certified configuration, and
    SPM_SETTINGS_UNSUPPORTED when settings were sent to a legacy bundle.
    """
    configured = _bundle_spm_configuration()
    if isinstance(configured, Mapping):
        return configured
    if _us_model_supports_spm():
        # A model that can run canonical SPM must never run it uncertified.
        raise SPMInputError(
            _CONFIGURATION_UNAVAILABLE,
            "The installed bundle has no certified SPM measurement configuration",
        )
    if not selection_sent:
        return None
    raise SPMInputError(
        _SETTINGS_UNSUPPORTED,
        "This US model bundle does not support canonical SPM settings",
    )


def _raise_unavailable_forecast(*, saved_household: bool) -> NoReturn:
    """Reject a selection naming a forecast this bundle does not have."""
    if saved_household:
        # A saved selection is the household's identity, not a request: this
        # deployment lacks the forecast it was measured against.
        raise SPMInputError(
            _CONFIGURATION_UNAVAILABLE,
            "This deployment does not have the SPM artifact this household's "
            "saved selection was measured against",
        )
    raise SPMInputError(
        _SETTINGS_INVALID,
        "SPM selection does not match this bundle's artifact hash",
    )


def _resolve_certified_selection(
    chosen: SPMSelection,
    configured: Mapping[str, object],
    *,
    saved_household: bool,
) -> dict[str, Any]:
    """Merge the caller's settings over the certified defaults and validate them."""
    try:
        defaults, forecast = _certified_defaults(configured)
    except (AttributeError, ImportError, ValueError, TypeError, OSError) as error:
        raise SPMInputError(
            _CONFIGURATION_UNAVAILABLE, _readable_error(error)
        ) from error
    if chosen.forecast_content_sha256 not in (None, defaults.forecast_content_sha256):
        _raise_unavailable_forecast(saved_household=saved_household)
    try:
        resolved = _validated_selection(_merged_selection(chosen, defaults), forecast)
    except ValueError as error:
        raise SPMInputError(_SETTINGS_INVALID, _readable_error(error)) from error
    if resolved.geography_kind == "metro":
        _validate_metro_selection(forecast, resolved)
    return resolved.model_dump(mode="json")


def _requested_selection(selection: object) -> SPMSelection:
    """Read the caller's settings; raise SPM_SETTINGS_INVALID if they are malformed.

    As in the Public API, the area rule applies to the settings as sent, with
    an omitted geography kind read as county: a metro selection needs an area
    and any other kind accepts none. The contract's model checks that rule
    only once both fields are set, because Stage 12 requests inherit omitted
    fields first.
    """
    try:
        chosen = SPMSelection.model_validate({} if selection is None else selection)
    except ValidationError as error:
        raise SPMInputError(_SETTINGS_INVALID, _readable_error(error)) from error
    if chosen.geography_kind == "metro":
        if not chosen.geography_id or not chosen.geography_id.strip():
            raise SPMInputError(
                _SETTINGS_INVALID, "An SPM area selection requires geography_id"
            )
    elif chosen.geography_id is not None:
        raise SPMInputError(
            _SETTINGS_INVALID, "Only an SPM area selection accepts geography_id"
        )
    return chosen


def resolve_household_spm_selection(
    country: str,
    selection: object,
    *,
    sent: bool,
    saved_household: bool,
) -> dict[str, Any] | None:
    """Resolve a household's SPM settings, exactly as sent, against the bundle.

    ``sent`` says whether the caller sent settings at all; ``selection`` is
    what it sent. Returns the complete resolved selection, or None when no SPM
    applies (any country but the US, or a bundle whose US model predates
    canonical SPM). Raises SPMInputError with the Public API's code and
    message otherwise, checking in its order: settings for another country,
    an explicit null, malformed settings, then the bundle's certification.
    ``saved_household`` says the settings were read from a saved household,
    which changes only how a forecast hash the bundle no longer carries is
    reported.
    """
    if country != "us":
        if sent:
            raise SPMInputError(
                _SETTINGS_UNSUPPORTED, "SPM settings are only available for the US"
            )
        return None
    if sent and selection is None:
        raise SPMInputError(
            _SETTINGS_INVALID,
            "spm must be an object; omit it to inherit the certified defaults.",
        )
    chosen = _requested_selection(selection)
    configured = _certified_configuration(selection_sent=sent)
    if configured is None:
        return None
    return _resolve_certified_selection(
        chosen, configured, saved_household=saved_household
    )


def household_spm_error_detail(error: BaseException) -> SPMErrorDetail | None:
    """Return the SPM error that ends a calculation, following wrapped causes.

    Recognizes this module's own ``SPMInputError`` and a country model's error
    carrying one of ``HOUSEHOLD_SPM_INPUT_ERROR_CODES``, as the Public API does.
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


def _receipt_mapping(receipt: Mapping[str, object], field_name: str) -> Mapping:
    value = receipt.get(field_name)
    if not isinstance(value, Mapping):
        msg = f"Country SPM receipt requires {field_name}"
        raise ValueError(msg)  # noqa: TRY004  # only ValueError means an uncertifiable receipt, as in the Public API
    return value


def _receipt_years(receipt: Mapping[str, object]) -> tuple[str, ...]:
    """Return the receipt's years, sorted; raise ValueError if they are unusable."""
    source_years = receipt.get("years")
    if not isinstance(source_years, Mapping):
        msg = "Country SPM receipt requires a years mapping"
        raise ValueError(msg)  # noqa: TRY004  # only ValueError means an uncertifiable receipt, as in the Public API
    if any(not isinstance(year, str) for year in source_years):
        msg = "Country SPM receipt years must be strings"
        raise ValueError(msg)
    return tuple(sorted(source_years))


def _compact_receipt(
    selection: SPMSelection, receipt: Mapping[str, object]
) -> SPMProvenance:
    """Reduce the country model's rich receipt to the compact public receipt.

    Checks run in the Public API's order, so the first problem reported is the
    one it reports: the required texts, their match with the selection, the
    years, the runtime versions, then the forecast ID.
    """
    forecast_sha256 = _required_text(receipt, "forecast_sha256")
    scenario = _required_text(receipt, "scenario")
    geography_kind = _required_text(receipt, "geography_kind")
    if (
        forecast_sha256 != selection.forecast_content_sha256
        or scenario != selection.scenario
        or geography_kind != selection.geography_kind
    ):
        msg = "Country SPM receipt differs from the SPM configuration"
        raise ValueError(msg)
    years = _receipt_years(receipt)
    runtime_versions = SPMRuntimeVersions.model_validate(
        _receipt_mapping(receipt, "runtime_versions")
    )
    return build_spm_provenance(
        forecast_id=_required_text(receipt, "forecast_id"),
        forecast_sha256=forecast_sha256,
        selection=selection,
        years=years,
        runtime_versions=runtime_versions,
    )


def _country_receipt(simulation: object) -> SPMProvenance:
    """Reduce the simulation's receipt.

    Raises ValueError if the receipt cannot be certified. A simulation with
    ``spm_config`` but no ``spm_provenance`` method raises AttributeError, as
    in the Public API.
    """
    selection = _complete_settings(getattr(simulation, "spm_config", None))
    if selection is None:
        msg = "Country SPM configuration is incomplete"
        raise ValueError(msg)
    source = simulation.spm_provenance()  # pyright: ignore[reportAttributeAccessIssue]  # a country Simulation with spm_config has this method
    if not isinstance(source, Mapping):
        msg = "Country SPM receipt must be an object"
        raise ValueError(msg)  # noqa: TRY004  # only ValueError means an uncertifiable receipt, as in the Public API
    return _compact_receipt(selection, source)


def _calculation_receipt(simulation: object) -> SPMProvenance | None:
    """Return the simulation's compact SPM receipt; None without SPM support."""
    if not hasattr(simulation, "spm_config"):
        return None
    try:
        return _country_receipt(simulation)
    except ValueError as error:
        raise SPMInputError(
            _CONFIGURATION_UNAVAILABLE,
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
