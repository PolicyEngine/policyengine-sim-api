"""Certified SPM runtime selection, independent of caller-supplied metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.spm import (
    SPMCapability,
    SPMInputError,
    SPMProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_comparison_provenance,
    build_spm_provenance,
    resolve_spm_selection,
    spm_error_detail,
    validate_spm_calculation_provenance,
)

if TYPE_CHECKING:
    from spm_calculator.rolling_forecast import SPMForecast


@lru_cache(maxsize=4)
def _forecast(expected_sha256: str | None) -> SPMForecast:
    from spm_calculator import (
        load_forecast,
    )

    return load_forecast(expected_sha256=expected_sha256)


def certified_spm_forecast(expected_sha256: str) -> SPMForecast:
    """Return the packaged SPM forecast whose content has ``expected_sha256``.

    Loaded once per process and digest; raises when the packaged forecast has
    another digest.
    """
    return _forecast(expected_sha256)


@lru_cache(maxsize=1)
def runtime_spm_capability() -> SPMCapability | None:
    """Return the installed image's certified SPM capability.

    None means the bundle configures no SPM measurement; a configured but
    unusable installation raises ``SPMInputError`` instead.

    Memoized for the life of the process because every input is a property
    of the installed image -- the bundle configuration, the wrapper's model
    fields, the country model's methods and the pinned forecast -- none of
    which a running container can change. One national request resolves the
    selection about six times (the worker entrypoint, ``_build_simulation``,
    and the dataset and baseline identity collectors), and each resolution
    called this.

    ``lru_cache`` does not memoize exceptions, so every fail-closed path
    still re-derives and re-raises: the cache can only ever skip work that
    already succeeded. Tests that stub the installed environment must call
    :func:`reset_spm_runtime_caches` (the executor suite does, automatically).
    """
    from policyengine.bundle import (
        get_current_bundle,
    )
    from policyengine.core import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine.core.Simulation
        Simulation,
    )

    try:
        bundle = get_current_bundle()
        configured = bundle.get("measurements", {}).get("spm")
    except (ImportError, ValueError, TypeError, AttributeError) as exc:
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "The installed SPM bundle configuration is unavailable",
        ) from exc
    if configured is None:
        return None
    if "spm" not in Simulation.model_fields or not hasattr(
        Simulation, "spm_provenance"
    ):
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "The installed wrapper does not support canonical SPM",
        )
    try:
        return _certified_us_spm_capability(configured)
    except (ImportError, ValueError, TypeError, OSError) as exc:
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(error_code, str(exc)) from exc


def _certified_us_spm_capability(configured: object) -> SPMCapability:
    """Certify the bundle's SPM defaults against the installed US model and forecast.

    Raises ``ImportError``, ``ValueError``, ``TypeError`` or ``OSError`` when the
    US model, the configuration or the pinned forecast cannot support them.
    """
    from policyengine_us import (
        Microsimulation,
    )

    if not (
        hasattr(Microsimulation, "spm_config")
        and callable(getattr(Microsimulation, "spm_provenance", None))
    ):
        msg = "The installed US model does not support canonical SPM"
        raise ValueError(msg)
    capability = SPMCapability(defaults=configured)
    forecast = _forecast(capability.defaults.forecast_content_sha256)
    forecast.entry(
        forecast.years[0],
        scenario=capability.defaults.scenario,
        as_of=capability.defaults.as_of,
    )
    return capability


@lru_cache(maxsize=64)
def _prevalidate_selection(
    selection_json: str, start_year: int, window_size: int
) -> None:
    """Prove a resolved selection can be measured, before anything expensive.

    Keyed on the resolved selection and the year range, which is everything
    it reads: the provider is constructed from the selection alone and asked
    only for per-year metadata. The same request resolves the same selection
    several times over (see :func:`runtime_spm_capability`), and the review
    of this change counted about six provider constructions per national
    request; memoizing collapses them to one per distinct selection and
    range.

    Only successful validations are memoized -- ``lru_cache`` re-runs after
    an exception -- so a rejected selection is rejected again, with the same
    typed error, every time it is asked for.
    """
    from spm_calculator.policyengine_adapter import (
        PolicyEngineSPMProvider,
    )

    selection = json.loads(selection_json)
    forecast = _forecast(selection["forecast_content_sha256"])
    if selection["county_vintage"] != "2020":
        msg = "Unsupported county vintage: use 2020"
        raise ValueError(msg)
    provider = PolicyEngineSPMProvider(
        forecast,
        **{
            key: value
            for key, value in selection.items()
            if key != "forecast_content_sha256"
        },
    )
    for year in range(start_year, start_year + window_size):
        # Use the country adapter's typed year contract. This temporary
        # provider validates metadata without measuring any SPM amount
        # or modifying the actual simulation's calculation receipts.
        provider.year_metadata(year)
        if selection["geography_kind"] == "metro":
            try:
                forecast.geography_factor(
                    year,
                    "renter",
                    kind="metro",
                    geoid=selection["geography_id"],
                    scenario=selection["scenario"],
                    as_of=selection["as_of"],
                )
            except ValueError as exc:
                error_code = "SPM_GEOGRAPHY_UNAVAILABLE"
                raise SPMInputError(error_code, str(exc)) from None


# Bound at import, so clearing still works while a test has replaced one of
# these module attributes with a stub.
_MEMO_CLEARERS = (
    runtime_spm_capability.cache_clear,
    _prevalidate_selection.cache_clear,
)


def reset_spm_runtime_caches() -> None:
    """Forget what was memoized from the installed environment.

    Production never needs this: a container's image is fixed for the life
    of the process. Tests that stub the installed bundle, wrapper or
    provider do, and the executor suite calls it around every test.

    ``_forecast`` is deliberately not cleared. It is keyed by the content
    hash of what it loads, so it cannot go stale, and a test that stubs it
    replaces the module attribute rather than filling the cache.
    """
    for clear in _MEMO_CLEARERS:
        clear()


def normalize_runtime_spm(params: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve before dataset loading, artifact lookup, or child submission."""
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )

    country = params.get("country", "us").lower()
    bundle = get_country_release_bundle(country)
    selection = resolve_spm_selection(
        country,
        params.get("spm"),
        capability=runtime_spm_capability() if country == "us" else None,
        policyengine_version=bundle.policyengine_version,
        model_version=bundle.model_version,
    )
    if selection is not None:
        canonical = json.dumps(selection, sort_keys=True, separators=(",", ":"))
        try:
            from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # simulation_runtime imports this module lazily; kept lazy on both sides
                _parse_year,
            )

            try:
                start = int(params.get("start_year") or _parse_year(params))
                window = int(params.get("window_size", 1))
            except ValueError:
                # Keep the order the range's old parse position gave it: the
                # selection was checked before the range, so an unusable year
                # never masked an unknown scenario. An empty range asks for
                # no year and reports the selection's own defect first.
                _prevalidate_selection(canonical, 0, 0)
                raise
            _prevalidate_selection(canonical, start, window)
        except ValueError as exc:
            detail = spm_error_detail(exc)
            if detail:
                raise SPMInputError(detail.code, detail.message) from exc
            # The installed provider constructor still exposes the forecast's
            # plain scenario error; use the same narrow translation as the
            # calculator's Frame and Axiom adapters, leaving other errors alone.
            if str(exc).startswith("Unknown forecast scenario:"):
                error_code = "SPM_SCENARIO_UNAVAILABLE"
                raise SPMInputError(error_code, str(exc)) from exc
            error_code = "SPM_SETTINGS_INVALID"
            raise SPMInputError(error_code, str(exc)) from exc
    return selection


def _required_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value:
        msg = f"SPM calculation receipt has no valid {field_name}"
        raise ValueError(msg)
    return value


def compact_spm_provenance(
    country_receipt: object,
    selection: object,
) -> SPMProvenance:
    """Extract the stable public receipt from country-owned diagnostics.

    The country package retains rich in-process diagnostic data. This adapter
    reads only the scalar identity fields, executed-year keys, and explicit
    package versions that belong on the simulation API wire contract.
    """
    if not isinstance(country_receipt, Mapping):
        msg = "SPM calculation receipt must be an object"
        raise TypeError(msg)
    resolved = SPMSelection.model_validate(selection)
    forecast_sha256 = _required_string(country_receipt, "forecast_sha256")
    for field_name, expected_value in (
        ("forecast_sha256", resolved.forecast_content_sha256),
        ("scenario", resolved.scenario),
        ("geography_kind", resolved.geography_kind),
    ):
        if country_receipt.get(field_name) != expected_value:
            msg = f"SPM calculation receipt {field_name} differs from the selection"
            raise ValueError(msg)
    raw_versions = country_receipt.get("runtime_versions")
    if not isinstance(raw_versions, Mapping):
        msg = "SPM calculation receipt has no runtime versions"
        raise TypeError(msg)
    runtime_versions = SPMRuntimeVersions.model_validate(raw_versions)
    raw_years = country_receipt.get("years")
    if not isinstance(raw_years, Mapping):
        msg = "SPM calculation receipt has no executed years"
        raise TypeError(msg)
    if any(not isinstance(year, str) for year in raw_years):
        msg = "SPM calculation receipt year keys must be strings"
        raise TypeError(msg)
    return build_spm_provenance(
        forecast_id=_required_string(country_receipt, "forecast_id"),
        forecast_sha256=forecast_sha256,
        selection=resolved,
        years=tuple(raw_years),
        runtime_versions=runtime_versions,
    )


def simulation_spm_calculation_provenance(
    simulation: object,
    selection: object,
) -> SPMProvenance:
    """Read and compact one simulation's resolved SPM receipt."""
    resolved = SPMSelection.model_validate(selection)
    if getattr(simulation, "spm_config", None) != resolved.model_dump(mode="json"):
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "Simulation ignored the requested SPM selection",
        )
    receipt = getattr(simulation, "spm_provenance", None)
    if not callable(receipt):
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "Simulation has no valid SPM calculation receipt",
        )
    try:
        return validate_spm_calculation_provenance(
            config=resolved,
            receipt=compact_spm_provenance(receipt(), resolved),
        )
    except (TypeError, ValueError) as exc:
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "Simulation has no valid SPM calculation receipt",
        ) from exc


def simulation_spm_result(
    baseline: object,
    reform: object,
    selection: object,
    *,
    expected_year: int | None = None,
) -> dict[str, object]:
    """Return the ``spm_provenance`` result fields for a baseline/reform pair.

    Empty when no SPM selection applies. Raises ``SPMInputError`` if either
    simulation ignored the selection, has no valid receipt, or (when
    ``expected_year`` is given) the receipts do not cover that year.
    """
    if selection is None:
        return {}
    baseline_provenance = simulation_spm_calculation_provenance(baseline, selection)
    reform_provenance = simulation_spm_calculation_provenance(reform, selection)
    comparison = build_spm_comparison_provenance(
        baseline_receipts=[baseline_provenance],
        reform_receipts=[reform_provenance],
    )
    if expected_year is not None and str(expected_year) not in (
        comparison.baseline.receipt.years
    ):
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(
            error_code,
            "Result SPM provenance does not cover the requested year",
        )
    return {
        "spm_provenance": comparison.model_dump(mode="json", by_alias=True),
    }
