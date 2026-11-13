"""What a report or simulation request becomes before and after its rows exist.

The submission services (``economy_submissions``, ``budget_window_submissions``)
turn a request into the persistence library's ``EconomyRequest``, refuse what
can't be calculated yet, choose what a report stores of the request's
options, the calculation's scope on the default worker's bundle, and the
answer for the report and run a request ended on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from policyengine_simulation_persistence import EconomyRequest

from policyengine_simulation_entry.economy_api_models import EconomyReportCreated
from policyengine_simulation_entry.economy_calculation_inputs import (
    CalculationScope,
    DatasetNotInBundleError,
    bundle_dataset,
    simulation_options,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)

if TYPE_CHECKING:
    from policyengine_simulation_persistence import (
        EconomyCatalogRows,
        FoundEconomyReport,
        ReportRunStatus,
    )

    from policyengine_simulation_entry.budget_window_api_models import (
        BudgetWindowReportRequest,
    )
    from policyengine_simulation_entry.economy_api_models import (
        EconomyRefusalCode,
        EconomyReportRequest,
        EconomySimulationRequest,
    )
    from policyengine_simulation_entry.economy_workers import EconomyWorker


def refuse_cliffs(request: EconomyReportRequest) -> None:
    """Refuse ``include_cliffs: true`` until per-run cliff calculations exist.

    Identical requests share a report and its runs, so a cliff request can't
    change what they calculate; C1 · 39 calculates each simulation run's
    cliffs on its own instead (C1 · 24's interim answer A).
    """
    if request.include_cliffs:
        raise EconomySubmissionUnprocessableError(
            code="cliffs_not_available",
            reasons=(
                UnprocessableReason(
                    loc=("body", "include_cliffs"),
                    msg=(
                        "Cliff impacts are not calculated yet: they will be "
                        "calculated for each simulation run on its own. Send "
                        "include_cliffs: false."
                    ),
                    type="cliffs_not_available",
                ),
            ),
        )


def stored_options(options: dict[str, Any]) -> dict[str, Any]:
    """Return what a report stores of a request's options: the SPM selection.

    A report has no options of its own (C1 · 24's answer 4): ``include_cliffs``
    is a request option, never stored or keyed.
    """
    return {name: value for name, value in options.items() if name == "spm"}


def report_created(
    found: FoundEconomyReport, status: ReportRunStatus
) -> EconomyReportCreated:
    """Answer the report and run a request ended on, with ``status``.

    A side is reused when this request didn't insert its run.
    """
    return EconomyReportCreated(
        report_id=found.report_id,
        report_run_id=found.run.id,
        status=status,
        baseline_simulation_id=found.baseline.simulation_id,
        baseline_simulation_run_id=found.baseline.simulation_run_id,
        baseline_reused=not found.baseline.run_inserted,
        reform_simulation_id=found.reform.simulation_id,
        reform_simulation_run_id=found.reform.simulation_run_id,
        reform_reused=not found.reform.run_inserted,
    )


def economy_request_for(
    request: EconomyReportRequest
    | EconomySimulationRequest
    | BudgetWindowReportRequest,
    worker: EconomyWorker,
) -> EconomyRequest:
    """Return what the request names apart from policies, for the default worker."""
    return EconomyRequest(
        country=request.country,
        policyengine_version=worker.policyengine_version,
        region_code=request.region_code,
        dataset_name=request.dataset_name,
        year=request.year,
    )


def calculation_scope(
    request: EconomyReportRequest
    | EconomySimulationRequest
    | BudgetWindowReportRequest,
    worker: EconomyWorker,
    catalog: EconomyCatalogRows,
    *,
    spm: dict[str, Any] | None,
) -> CalculationScope:
    """Return what the calculation runs on, with the bundle's dataset.

    Raise EconomySubmissionRefusedError when the bundle lacks the catalog
    dataset: ``unknown_dataset`` when the request named it, and
    ``dataset_not_in_bundle`` for the region's default.
    """
    try:
        dataset = bundle_dataset(worker, catalog.dataset.name)
    except DatasetNotInBundleError:
        code: EconomyRefusalCode = (
            "dataset_not_in_bundle"
            if request.dataset_name is None
            else "unknown_dataset"
        )
        detail = (
            f"The default v2 worker, .py {worker.version}, has no dataset "
            f"{catalog.dataset.name}."
        )
        raise EconomySubmissionRefusedError(code=code, detail=detail) from None
    return CalculationScope(
        country=request.country,
        region_code=catalog.region.code,
        dataset=dataset,
        year=request.year,
        options=simulation_options(
            include_cliffs=getattr(request, "include_cliffs", False), spm=spm
        ),
    )
