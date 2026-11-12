"""Creating a budget-window report and starting its first years.

The service creates every year's runs in one transaction, then advances the
window, which starts the earliest years (at most five) by calling the
coordinator with each year's input. The first year's input is built as a
single-year report's is, and every later year's is derived from it. The
store and the window advance are faked here; both are tested against
PostgreSQL in the persistence libraries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from economy_service_fakes import (
    CATALOG,
    CREATED_AT,
    OUTPUT_BUCKET,
    REFORM,
    FakeCaller,
    FakeStore,
    FakeWorkers,
    numbered_ids,
)
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    ReportRunIds,
    Stage12InvocationContext,
    year_calculation,
)
from policyengine_simulation_entry import budget_window_submissions
from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportRequest,
)
from policyengine_simulation_entry.budget_window_submissions import (
    ReportSubmissionService,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionUnavailableError,
)
from policyengine_simulation_entry.economy_submissions import ServiceSettings
from policyengine_simulation_persistence import (
    BudgetWindowInputs,
    ReportRunStatus,
    ReusableRun,
    RunReuse,
    SimulationInputs,
)
from policyengine_stage12_persistence import (
    BUDGET_WINDOW_MAX_RUNNING_YEARS,
    RunStatus,
    WindowAdvance,
    YearStartDeferred,
    YearToStart,
)
from sqlalchemy.exc import OperationalError
from test_budget_window_requests import WINDOW_BODY

OBSERVED = {"observability_id": "observed"}
REUSED_IN_2027 = ReusableRun(
    simulation_id=UUID("00000000-0000-4000-8000-0000000000d1"),
    simulation_run_id=UUID("00000000-0000-4000-8000-0000000000d2"),
)


@dataclass
class FakeWindowStore(FakeStore):
    """Also reads and creates budget-window reports."""

    reusable_by_year: dict[int, tuple[ReusableRun | None, ReusableRun | None]] = field(
        default_factory=dict
    )
    window_reads: list[tuple[Any, ...]] = field(default_factory=list)
    windows: list[Any] = field(default_factory=list)

    def read_budget_window_inputs(self, request, *, baseline, reform, reuse, years):
        self.window_reads.append((request, baseline, reform, reuse, tuple(years)))
        if self.read_failure is not None:
            raise self.read_failure
        return BudgetWindowInputs(
            catalog=self.catalog,
            baseline=SimulationInputs(choice=baseline, reform={}),
            reform=SimulationInputs(choice=reform, reform=REFORM),
            reusable_runs={
                year: self.reusable_by_year.get(year, (None, None)) for year in years
            },
        )

    def create_budget_window_report(self, report) -> None:
        if self.create_failure is not None:
            raise self.create_failure
        self.windows.append(report)


@dataclass
class FakeAdvancer:
    """Starts the window's earliest years, as the budget-window store does.

    A start that raises fails the window, as Modal refusing a year does.
    """

    store: FakeWindowStore
    failure: Exception | None = None
    # How long the advance takes, as the database's lock and reads may, before
    # it starts its first year, and between one start and the next.
    delay_seconds: float = 0.0
    delay_between_starts_seconds: float = 0.0
    advances: list[UUID] = field(default_factory=list)
    started: list[tuple[YearToStart, str]] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)

    def advance_window(self, window_run_id, *, now, start_year):
        self.advances.append(window_run_id)
        time.sleep(self.delay_seconds)
        if self.failure is not None:
            raise self.failure
        (report,) = self.store.windows
        started_years = []
        for window_year in report.years[:BUDGET_WINDOW_MAX_RUNNING_YEARS]:
            year_to_start = YearToStart(
                year=window_year.year,
                run_ids=ReportRunIds(
                    report_run_id=window_year.report_run_id,
                    baseline_simulation_run_id=window_year.baseline.simulation_run_id,
                    reform_simulation_run_id=window_year.reform.simulation_run_id,
                ),
            )
            if started_years:
                time.sleep(self.delay_between_starts_seconds)
            try:
                call_id = start_year(year_to_start)
            except YearStartDeferred:
                # As the store, the year and the later ones wait.
                self.deferred.append(window_year.year)
                break
            except Exception:  # noqa: BLE001  # as the store, any failed start fails the window
                return WindowAdvance(
                    status=RunStatus.FAILED,
                    changed=True,
                    started_years=tuple(started_years),
                )
            self.started.append((year_to_start, call_id))
            started_years.append(window_year.year)
        return WindowAdvance(
            status=RunStatus.PENDING, changed=False, started_years=tuple(started_years)
        )


def _service(store, *, caller=None, advancer=None):
    return ReportSubmissionService(
        windows=advancer or FakeAdvancer(store),
        store=store,
        workers=FakeWorkers(),
        caller=caller or FakeCaller(),
        settings=ServiceSettings(
            environment="staging",
            modal_environment="staging",
            output_bucket=OUTPUT_BUCKET,
        ),
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
        clock=lambda: CREATED_AT,
    )


def _submit(submissions, **changes):
    request = BudgetWindowReportRequest.model_validate({**WINDOW_BODY, **changes})
    return asyncio.run(
        submissions.submit_budget_window(
            request, request_id="observed", observability_context=OBSERVED
        )
    )


def test_a_window_is_created_with_a_run_per_year_and_its_first_five_started() -> None:
    store = FakeWindowStore()
    caller = FakeCaller()
    advancer = FakeAdvancer(store)

    created = _submit(
        _service(store, caller=caller, advancer=advancer), budget_window_size=7
    )

    ((request, baseline, reform, reuse, years),) = store.window_reads
    assert (baseline.policy_id, str(reform.policy_id)) == (
        None,
        WINDOW_BODY["reform"]["policy_id"],
    )
    assert (request.year, years) == (2026, tuple(range(2026, 2033)))
    assert reuse == RunReuse(
        versions=reuse.versions,
        inputs={"include_cliffs": False, "spm": {"geography_kind": "national"}},
        output_uri_prefix=f"gs://{OUTPUT_BUCKET}/",
    )
    (report,) = store.windows
    assert [window_year.year for window_year in report.years] == list(range(2026, 2033))
    assert report.population == CATALOG.population(2026)
    assert report.inputs == reuse.inputs
    assert advancer.advances == [report.window_run_id]
    # The first five years were started, each with its own year and runs.
    assert len(caller.report_runs) == 5
    for (year_to_start, call_id), (_, payload, context, observed) in zip(
        advancer.started, caller.report_runs, strict=True
    ):
        report_input = ReportExecutionInput.model_validate(payload)
        year_context = Stage12InvocationContext.model_validate(context)
        assert (
            report_input.baseline.year == report_input.reform.year == year_to_start.year
        )
        assert year_context.report_run == year_to_start.run_ids
        assert report_input.reform.policy == REFORM
        assert (call_id, observed) == ("fc-coordinator", OBSERVED)
    first_year = report.years[0]
    assert created.model_dump() == {
        "report_id": report.id,
        "report_run_id": first_year.report_run_id,
        "budget_window_run_id": report.window_run_id,
        "status": ReportRunStatus.PENDING,
        "years": [
            {
                "year": window_year.year,
                "report_run_id": window_year.report_run_id,
                "baseline_simulation_id": window_year.baseline.simulation_id,
                "baseline_simulation_run_id": window_year.baseline.simulation_run_id,
                "baseline_reused": False,
                "reform_simulation_id": window_year.reform.simulation_id,
                "reform_simulation_run_id": window_year.reform.simulation_run_id,
                "reform_reused": False,
            }
            for window_year in report.years
        ],
    }


def test_each_later_year_is_derived_from_the_first() -> None:
    store = FakeWindowStore()
    caller = FakeCaller()
    advancer = FakeAdvancer(store)

    _submit(_service(store, caller=caller, advancer=advancer), budget_window_size=3)

    (_, first_payload, first_context, _) = caller.report_runs[0]
    for (year_to_start, _), (_, payload, context, _) in zip(
        advancer.started[1:], caller.report_runs[1:], strict=True
    ):
        derived_report, derived_context = year_calculation(
            ReportExecutionInput.model_validate(first_payload),
            Stage12InvocationContext.model_validate(first_context),
            year=year_to_start.year,
            run_ids=year_to_start.run_ids,
            created_at=CREATED_AT,
        )
        assert payload == derived_report.model_dump(mode="json")
        assert context == derived_context.model_dump(mode="json")


def test_a_year_reuses_its_own_year_s_run() -> None:
    store = FakeWindowStore(reusable_by_year={2027: (REUSED_IN_2027, None)})

    created = _submit(_service(store), budget_window_size=3)

    years = {window_year.year: window_year for window_year in store.windows[0].years}
    assert years[2027].baseline.simulation_run_id == REUSED_IN_2027.simulation_run_id
    assert years[2027].baseline.reused
    assert not years[2026].baseline.reused
    assert [year.baseline_reused for year in created.years] == [False, True, False]


def test_a_first_year_modal_refuses_answers_503_naming_the_report() -> None:
    store = FakeWindowStore()
    caller = FakeCaller(failure=RuntimeError("Modal is unavailable"))

    with pytest.raises(EconomySubmissionUnavailableError) as refused:
        _submit(_service(store, caller=caller), budget_window_size=3)

    assert refused.value.code == "modal_call_refused"
    assert refused.value.record_id == store.windows[0].id


def test_a_start_modal_does_not_confirm_in_time_is_refused() -> None:
    store = FakeWindowStore()

    with pytest.raises(EconomySubmissionUnavailableError) as refused:
        _submit(_service(store, caller=FakeCaller(hang=True)), budget_window_size=2)

    assert refused.value.record_id == store.windows[0].id


def test_a_window_whose_advance_cannot_be_written_is_answered_created() -> None:
    # The rows exist; a year Modal started claims itself, and the stuck-run
    # sweep fails a window no year advances.
    store = FakeWindowStore()
    advancer = FakeAdvancer(
        store, failure=OperationalError("SELECT 1", {}, Exception("closed"))
    )

    created = _submit(_service(store, advancer=advancer), budget_window_size=2)

    assert created.status is ReportRunStatus.PENDING
    assert created.report_id == store.windows[0].id


@dataclass
class SlowAfterFirstCaller(FakeCaller):
    """Starts the first call at once, then waits past any limit for the rest."""

    def __post_init__(self) -> None:
        self.calls = 0

    async def start_report_run(self, worker, report_payload, context_payload, **kwargs):
        self.calls += 1
        if self.calls > 1:
            await asyncio.sleep(60)
        return await super().start_report_run(
            worker, report_payload, context_payload, **kwargs
        )


def test_the_first_years_share_one_wait_for_modal() -> None:
    # A request starting five years one after another never waits more than
    # one call's limit: a later year not started in time is left for the
    # started year's coordinator to start.
    store = FakeWindowStore()
    caller = SlowAfterFirstCaller()
    advancer = FakeAdvancer(store)
    started_at = time.monotonic()

    created = _submit(
        _service(store, caller=caller, advancer=advancer), budget_window_size=5
    )

    assert time.monotonic() - started_at < 0.5
    assert created.status is ReportRunStatus.PENDING
    assert [year_to_start.year for year_to_start, _ in advancer.started] == [2026]
    assert advancer.deferred == [2027]
    # Once the wait has run out, no later year's call is even started.
    assert caller.calls == 2


def test_the_wait_for_modal_starts_when_the_first_year_starts() -> None:
    # The advance's lock and reads take longer than the wait itself; the
    # first year's call is still given the whole wait.
    store = FakeWindowStore()
    caller = FakeCaller()
    advancer = FakeAdvancer(store, delay_seconds=0.1)

    created = _submit(
        _service(store, caller=caller, advancer=advancer), budget_window_size=2
    )

    assert created.status is ReportRunStatus.PENDING
    assert [year_to_start.year for year_to_start, _ in advancer.started] == [
        2026,
        2027,
    ]


@dataclass
class RefusingAfterFirstCaller(FakeCaller):
    """Starts the first call, then is refused by Modal for the next."""

    def __post_init__(self) -> None:
        self.calls = 0

    async def start_report_run(self, worker, report_payload, context_payload, **kwargs):
        self.calls += 1
        if self.calls > 1:
            message = "Modal refused the call"
            raise RuntimeError(message)
        return await super().start_report_run(
            worker, report_payload, context_payload, **kwargs
        )


def test_a_later_year_modal_refuses_fails_the_window() -> None:
    store = FakeWindowStore()

    with pytest.raises(EconomySubmissionUnavailableError) as refused:
        _submit(
            _service(store, caller=RefusingAfterFirstCaller()), budget_window_size=3
        )

    assert refused.value.record_id == store.windows[0].id


def test_a_later_year_is_deferred_without_a_call_once_the_wait_has_run_out(
    caplog,
) -> None:
    store = FakeWindowStore()
    caller = FakeCaller()
    advancer = FakeAdvancer(store, delay_between_starts_seconds=0.1)

    with caplog.at_level(logging.INFO, logger=budget_window_submissions.__name__):
        created = _submit(
            _service(store, caller=caller, advancer=advancer), budget_window_size=3
        )

    assert created.status is ReportRunStatus.PENDING
    # The first year's call used the whole wait; the second is never called.
    assert len(caller.report_runs) == 1
    assert advancer.deferred == [2027]
    (deferral,) = [
        record
        for record in caplog.records
        if record.getMessage() == "budget_window_year_deferred"
    ]
    second_year = store.windows[0].years[1]
    assert (deferral.year, deferral.record_id) == (
        2027,
        str(second_year.report_run_id),
    )
