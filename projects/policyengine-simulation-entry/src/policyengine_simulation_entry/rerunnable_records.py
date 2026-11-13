"""Which stored records the service re-runs, and what each calculates on.

A re-run (``economy_reruns``) first reads what the stored report or
simulation calculates, then checks it here: a record of a kind the service
doesn't re-run, of a country it doesn't calculate, or missing a row it
calculates on is refused (409 ``rerun_not_supported``) before anything is
read from the catalog or written. ``rerun_scope`` then says what the re-run
calculates on, with the default worker's bundle dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, get_args

from policyengine_simulation_contract.stage12_bundle import CountryId

from policyengine_simulation_entry.economy_calculation_inputs import (
    CalculationScope,
    DatasetNotInBundleError,
    bundle_dataset,
    simulation_options,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomyRerunConflictError,
    EconomySubmissionRefusedError,
)
from policyengine_simulation_entry.household_report_reruns import (
    StoredHouseholdReport,
)

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import (
        EconomyCatalogRows,
        EconomyRequest,
        RecordKind,
        ReportDefinition,
        SideDefinition,
        SimulationDefinition,
        WindowYearDefinition,
    )

    from policyengine_simulation_entry.economy_submission_errors import RecordName
    from policyengine_simulation_entry.economy_workers import EconomyWorker

# The kinds of stored economy report this service re-runs itself.
_RERUN_REPORT_KINDS: Final[frozenset[RecordKind]] = frozenset(
    {"economy", "budget_window"}
)
# The countries this service calculates, as the contract names them.
_COUNTRIES: Final[tuple[CountryId, ...]] = get_args(CountryId)
_REPORT: Final[RecordName] = "report"
_SIMULATION: Final[RecordName] = "simulation"


@dataclass(frozen=True)
class StoredReportRows:
    """The country and rows a stored report calculates on, each present."""

    report_id: UUID
    country: CountryId
    year: int
    region_code: str
    dataset_name: str
    baseline: SideDefinition
    reform: SideDefinition


@dataclass(frozen=True)
class StoredSimulationRows:
    """The country and rows a stored simulation calculates on, each present."""

    country: CountryId
    year: int
    region_code: str
    dataset_name: str


def calculated_country(country: str | None) -> CountryId | None:
    """Return the country if this service calculates it, or None."""
    return next((known for known in _COUNTRIES if known == country), None)


def rerun_refusal(
    record: RecordName, kind: RecordKind, missing: tuple[tuple[str, object], ...]
) -> EconomyRerunConflictError:
    """Return why a stored record is not re-run: its kind, or the first row it lacks.

    ``missing`` pairs each row's description with the value the record holds.
    """
    if kind == "unsupported":
        detail = f"The service does not re-run this type of {record}."
    elif record == _SIMULATION and kind == "household":
        detail = (
            "The service re-runs a household simulation only through its report "
            "(POST /reports/{report_id}/runs)."
        )
    else:
        lacking = next(name for name, value in missing if value is None)
        detail = f"The {record} names no {lacking}, so it cannot be re-run."
    return EconomyRerunConflictError(code="rerun_not_supported", detail=detail)


def rerunnable_report(definition: ReportDefinition) -> StoredReportRows:
    """Return the stored report's rows, or refuse one this service can't re-run."""
    baseline, reform = definition.baseline, definition.reform
    region_code, dataset_name = definition.region_code, definition.dataset_name
    country, year = calculated_country(definition.country), definition.year
    if (
        definition.kind not in _RERUN_REPORT_KINDS
        or country is None
        or baseline is None
        or reform is None
        or region_code is None
        or dataset_name is None
        or year is None
    ):
        raise rerun_refusal(
            _REPORT,
            definition.kind,
            (
                ("country this service calculates", country),
                ("baseline simulation", baseline),
                ("reform simulation", reform),
                ("region", region_code),
                ("dataset", dataset_name),
                ("year", year),
            ),
        )
    return StoredReportRows(
        report_id=definition.id,
        country=country,
        year=year,
        region_code=region_code,
        dataset_name=dataset_name,
        baseline=baseline,
        reform=reform,
    )


def rerunnable_household_report(
    definition: ReportDefinition,
) -> StoredHouseholdReport:
    """Return the stored household report's rows, or refuse one it can't re-run.

    A side naming a dynamic is refused: a household calculation applies none.
    """
    country, year = calculated_country(definition.country), definition.year
    household_id, baseline = definition.household_id, definition.baseline
    if country is None or household_id is None or baseline is None or year is None:
        raise rerun_refusal(
            _REPORT,
            definition.kind,
            (
                ("country this service calculates", country),
                ("household", household_id),
                ("baseline simulation", baseline),
                ("year", year),
            ),
        )
    if any(
        side is not None and side.choice.dynamic_id is not None
        for side in (baseline, definition.reform)
    ):
        raise EconomyRerunConflictError(
            code="rerun_not_supported",
            detail=(
                "The report's household simulation names a dynamic, which a "
                "household calculation does not apply, so it cannot be re-run."
            ),
        )
    return StoredHouseholdReport(
        report_id=definition.id,
        country=country,
        household_id=household_id,
        year=year,
        baseline=baseline,
        reform=definition.reform,
    )


def complete_window_years(
    definition: ReportDefinition,
) -> tuple[WindowYearDefinition, ...]:
    """Return the stored window's years, or refuse a window that lacks one.

    Its newest calculation must have a report run naming both sides in each
    year of its window, from the report's first year; the definition leaves
    out a year naming no reform.
    """
    expected = (
        ()
        if definition.year is None or definition.budget_window_size is None
        else tuple(
            range(definition.year, definition.year + definition.budget_window_size)
        )
    )
    years = definition.window_years
    if not expected or tuple(year.year for year in years) != expected:
        raise EconomyRerunConflictError(
            code="rerun_not_supported",
            detail=(
                "The report names no complete budget-window calculation, a "
                "report run naming both sides in each year of its window, so "
                "it cannot be re-run."
            ),
        )
    return years


def rerunnable_simulation(definition: SimulationDefinition) -> StoredSimulationRows:
    """Return the stored simulation's country and rows, or refuse it.

    Its country is None when its catalog version's model is of no country
    this service calculates.
    """
    country, year = calculated_country(definition.country), definition.year
    region_code, dataset_name = definition.region_code, definition.dataset_name
    if (
        definition.kind != "economy"
        or country is None
        or region_code is None
        or dataset_name is None
        or year is None
    ):
        raise rerun_refusal(
            _SIMULATION,
            definition.kind,
            (
                ("country this service calculates", country),
                ("region", region_code),
                ("dataset", dataset_name),
                ("year", year),
            ),
        )
    return StoredSimulationRows(
        country=country,
        year=year,
        region_code=region_code,
        dataset_name=dataset_name,
    )


def rerun_scope(
    country: CountryId,
    request: EconomyRequest,
    worker: EconomyWorker,
    catalog: EconomyCatalogRows,
    *,
    options: dict[str, Any],
) -> CalculationScope:
    """Return what a re-run calculates on, with the bundle's dataset.

    ``options`` are the stored report's (``reports.inputs``). Raise
    EconomySubmissionRefusedError (``dataset_not_in_bundle``) when the
    default worker's bundle lacks the stored dataset.
    """
    try:
        dataset = bundle_dataset(worker, catalog.dataset.name)
    except DatasetNotInBundleError:
        raise EconomySubmissionRefusedError(
            code="dataset_not_in_bundle",
            detail=(
                f"The default v2 worker, .py {worker.version}, has no dataset "
                f"{catalog.dataset.name}, which the stored record names."
            ),
        ) from None
    return CalculationScope(
        country=country,
        region_code=catalog.region.code,
        dataset=dataset,
        year=request.year,
        options=simulation_options(
            include_cliffs=bool(options.get("include_cliffs", False)),
            spm=options.get("spm"),
        ),
    )
