"""What a report or simulation request becomes before and after its rows exist.

The submission services (``economy_submissions``, ``budget_window_submissions``)
turn a request into the persistence library's ``EconomyRequest``, choose
what a report stores of the request's options, the calculation's scope on
the default worker's bundle, and the answer for the report and run a
request ended on. The re-run service refuses cliffs for a report that
calculates none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

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
    from collections.abc import Mapping

    from policyengine_simulation_persistence import (
        EconomyCatalogRows,
        FoundEconomyReport,
        RecordKind,
        ReportRunStatus,
    )

    from policyengine_simulation_entry.budget_window_api_models import (
        BudgetWindowReportRequest,
    )
    from policyengine_simulation_entry.economy_api_models import (
        EconomyReportRequest,
        EconomySimulationRequest,
    )
    from policyengine_simulation_entry.economy_workers import EconomyWorker


# The reports whose re-run calculates no cliffs, as their answers name them.
_REPORTS_WITHOUT_CLIFFS: Final[dict[RecordKind, str]] = {
    "budget_window": "A budget-window report",
    "household": "A household report",
}


def refuse_cliffs_for(kind: RecordKind) -> None:
    """Refuse a re-run asking for cliffs of a report kind that calculates none.

    A budget window or a household report has no cliff calculation: only an
    economy report's simulation runs do (C1 · 24's answer 4b).
    """
    report = _REPORTS_WITHOUT_CLIFFS.get(kind)
    if report is None:
        return
    raise EconomySubmissionUnprocessableError(
        code="cliffs_not_available",
        reasons=(
            UnprocessableReason(
                loc=("body", "include_cliffs"),
                msg=(
                    f"{report} calculates no cliff impacts. Send include_cliffs: false."
                ),
                type="cliffs_not_available",
            ),
        ),
    )


def asks_for_cliffs(*, include_cliffs: bool, stored_inputs: Mapping[str, Any]) -> bool:
    """Return whether a report's re-run asks for its runs' cliffs.

    It does when the request sends ``include_cliffs: true``, or when the
    report was stored before C1 · 27 asking for cliffs, which its stored
    options still hold (the user, 2026-11-17). The stored value is only
    read; nothing writes or keys it.
    """
    return include_cliffs or stored_inputs.get("include_cliffs") is True


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
    """Return what the request names apart from policies, for the default worker.

    It names no dataset: the store reads the region's, or the local-area
    dataset a report on it asks for.
    """
    return EconomyRequest(
        country=request.country,
        policyengine_version=worker.policyengine_version,
        region_code=request.region_code,
        dataset_name=None,
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

    Raise EconomySubmissionRefusedError (``dataset_not_in_bundle``, the
    service's state) when the bundle lacks the catalog dataset: no request
    names one.
    """
    try:
        dataset = bundle_dataset(worker, catalog.dataset.name)
    except DatasetNotInBundleError:
        detail = (
            f"The default v2 worker, .py {worker.version}, has no dataset "
            f"{catalog.dataset.name}."
        )
        raise EconomySubmissionRefusedError(
            code="dataset_not_in_bundle", detail=detail
        ) from None
    return CalculationScope(
        country=request.country,
        region_code=catalog.region.code,
        dataset=dataset,
        year=request.year,
        options=simulation_options(spm=spm),
    )
