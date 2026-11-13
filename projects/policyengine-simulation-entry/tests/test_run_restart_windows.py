"""Starting a stopped budget window again, from its earliest waiting year.

A budget window stops when none of its years is under way and years still
wait for a start: the service stopped before starting its first years, or a
year's coordinator before starting the next. The restart check rebuilds the
earliest waiting year's call and advances the window with the service's own
year starter, marking that year just before the window's first start. The
window advance is faked here as ``BudgetWindowStore`` advances a window; the
store is tested against PostgreSQL in its own library.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from economy_service_fakes import (
    CATALOG_VERSION_ID,
    CREATED_AT,
    DYNAMIC_ID,
    OUTPUT_BUCKET,
    POLICY_ID,
    REFORM,
    FakeCaller,
    numbered_ids,
)
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportRequest,
)
from policyengine_simulation_entry.budget_window_submissions import (
    ReportSubmissionService,
)
from policyengine_simulation_entry.economy_submissions import ServiceSettings
from policyengine_simulation_entry.economy_workers import ManifestEconomyWorkers
from policyengine_simulation_entry.run_restarts import (
    CHECK_TIME_LIMIT,
    RESTART_AFTER,
    RESTART_UNTIL,
    RESTARTS_PER_CHECK,
    RestartOutcome,
    RestartSummary,
)
from policyengine_simulation_persistence import (
    EconomyScope,
    IdleBudgetWindow,
    RestartKind,
    SimulationChoice,
    StoredEconomySide,
    StoredReportRun,
    StoredRunUnreadableError,
    UnstartedRun,
    WaitingYear,
)
from policyengine_stage12_persistence import (
    RunStatus,
    WindowAdvance,
    YearStartDeferred,
    YearStartRefused,
    YearToStart,
)
from test_budget_window_requests import WINDOW_BODY
from test_budget_window_submissions import FakeAdvancer, FakeWindowStore
from test_run_restarts import (
    DATABASE_ERROR,
    MANIFEST_STORE,
    NOW,
    FakeRestartStore,
    restart_rig,
    submitted_report,
)

if TYPE_CHECKING:
    from datetime import datetime

    from test_run_restarts import RestartRig

WINDOW_RUN_ID = UUID("00000000-0000-4000-8000-0000000000f1")
NOT_REBUILT = (
    "run_not_started: The run stayed pending with no Modal call recorded for it, "
    "and its call could not be built again: its stored rows no longer describe "
    "one calculation."
)


@dataclass
class FakeWindows:
    """Advances a window as ``BudgetWindowStore`` does: its waiting years in order.

    Each waiting year is offered to the starter until it defers one. A start
    that raises fails the window, and the year records the refusal's own
    message, or the error's type. ``ended`` is returned with no year offered,
    as for a window that finished, or that a failed year fails now.
    ``failure`` is raised before any year is offered, as when the window's
    lock or reads fail, and ``failure_after_starts`` after every start, as
    when the commit fails. A start that raises for a year in
    ``claimed_meanwhile`` counts that year started and goes on, as when the
    year's own call claimed it first; ``refusal_not_written`` is raised in
    place of failing the window, as when that failure can't be written.
    """

    waiting: tuple[YearToStart, ...] = ()
    status: RunStatus = RunStatus.PENDING
    ended: WindowAdvance | None = None
    failure: BaseException | None = None
    failure_after_starts: BaseException | None = None
    claimed_meanwhile: frozenset[UUID] = frozenset()
    refusal_not_written: BaseException | None = None
    advanced: list[UUID] = field(default_factory=list)
    started_calls: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def advance_window(
        self, window_run_id: UUID, *, now: datetime, start_year: Any = None
    ) -> WindowAdvance:
        self.advanced.append(window_run_id)
        if self.failure is not None:
            raise self.failure
        if self.ended is not None:
            return self.ended
        started = []
        for year in self.waiting:
            try:
                self.started_calls.append(start_year(year))
            except YearStartDeferred:
                break
            except Exception as error:  # as the store fails a refused year
                if year.run_ids.report_run_id in self.claimed_meanwhile:
                    started.append(year.year)
                    continue
                if self.refusal_not_written is not None:
                    raise self.refusal_not_written from error
                self.refusals.append(
                    error.error_message
                    if isinstance(error, YearStartRefused)
                    else type(error).__name__
                )
                return WindowAdvance(
                    status=RunStatus.FAILED, changed=True, started_years=tuple(started)
                )
            started.append(year.year)
        if self.failure_after_starts is not None:
            raise self.failure_after_starts
        return WindowAdvance(
            status=self.status, changed=False, started_years=tuple(started)
        )


def _idle_window(
    run_id: UUID = WINDOW_RUN_ID, last_changed_at: datetime = CREATED_AT
) -> IdleBudgetWindow:
    return IdleBudgetWindow(run_id=run_id, last_changed_at=last_changed_at)


def _year_to_start(stored: StoredReportRun, year: int) -> YearToStart:
    return YearToStart(
        year=year,
        run_ids=ReportRunIds(
            report_run_id=stored.report_run_id,
            baseline_simulation_run_id=stored.baseline.simulation_run_id,
            reform_simulation_run_id=stored.reform.simulation_run_id,
        ),
    )


def _idle_window_rig(**options: Any) -> tuple[RestartRig, StoredReportRun, dict]:
    """Return a rig with one idle window whose earliest waiting year was submitted.

    Its second waiting year is the following year, on runs of its own.
    """
    stored, (payload, _context) = submitted_report()
    second_year = YearToStart(
        year=2027,
        run_ids=ReportRunIds(
            report_run_id=UUID(int=2027),
            baseline_simulation_run_id=UUID(int=20271),
            reform_simulation_run_id=UUID(int=20272),
        ),
    )
    restarts = options.pop("restarts", None) or FakeRestartStore(
        idle_windows=(_idle_window(),),
        report_runs={stored.report_run_id: stored},
        waiting_years={
            WINDOW_RUN_ID: WaitingYear(report_run_id=stored.report_run_id, year=2026)
        },
    )
    windows = options.pop("windows", None) or FakeWindows(
        waiting=(_year_to_start(stored, 2026), second_year)
    )
    return restart_rig(restarts=restarts, windows=windows, **options), stored, payload


def _marked_run_ids(rig: RestartRig) -> list[UUID]:
    """Return the report runs of the window years the check marked."""
    return [year.report_run_id for year in rig.restarts.marked_years]


def test_an_idle_window_is_restarted_from_its_earliest_waiting_year() -> None:
    """Its first years start as the service starts them, from its first year's call."""
    rig, stored, payload = _idle_window_rig()

    summary = rig.restart()

    assert rig.windows.advanced == [WINDOW_RUN_ID]
    assert _marked_run_ids(rig) == [stored.report_run_id]
    first_call, second_call = rig.economy_caller.report_runs
    assert first_call[1] == payload
    assert second_call[1]["evaluation_id"] == str(UUID(int=2027))
    assert second_call[1]["baseline"]["year"] == 2027
    assert summary == RestartSummary(listed=1, restarted=1)
    assert rig.telemetry.outcomes == [
        (RestartKind.BUDGET_WINDOW, WINDOW_RUN_ID, RestartOutcome.RESTARTED, None)
    ]


def test_the_check_lists_idle_windows_in_the_same_window_of_time() -> None:
    rig, _stored, _payload = _idle_window_rig()

    rig.restart()

    assert (
        rig.restarts.listings
        == [(NOW - RESTART_UNTIL, NOW - RESTART_AFTER, RESTARTS_PER_CHECK)] * 2
    )


def test_rows_and_windows_are_taken_together_oldest_first_up_to_the_limit() -> None:
    """The check takes at most its limit, the longest waiting first, of either kind."""
    rows = tuple(
        UnstartedRun(
            kind=RestartKind.REPORT_RUN,
            run_id=UUID(int=100 + position),
            created_at=CREATED_AT + timedelta(seconds=2 * position),
        )
        for position in range(15)
    )
    windows = tuple(
        _idle_window(
            run_id=UUID(int=200 + position),
            last_changed_at=CREATED_AT + timedelta(seconds=2 * position + 1),
        )
        for position in range(15)
    )
    rig = restart_rig(
        restarts=FakeRestartStore(unstarted=rows, idle_windows=windows),
        windows=FakeWindows(),
    )

    summary = rig.restart()

    taken = [run_id for _kind, run_id, _age in rig.telemetry.scopes]
    interleaved = [
        run.run_id for pair in zip(rows, windows, strict=True) for run in pair
    ]
    assert taken == interleaved[:RESTARTS_PER_CHECK]
    assert summary.listed == RESTARTS_PER_CHECK


def test_a_window_whose_restarted_year_modal_refuses_counts_as_failed() -> None:
    rig, stored, _payload = _idle_window_rig(
        economy_caller=FakeCaller(failure=ConnectionError("Modal is down"))
    )

    summary = rig.restart()

    assert _marked_run_ids(rig) == [stored.report_run_id]
    assert rig.windows.refusals == ["ConnectionError"]
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_window_no_longer_waiting_is_left_unmarked() -> None:
    rig, _stored, _payload = _idle_window_rig(
        restarts=FakeRestartStore(idle_windows=(_idle_window(),))
    )

    summary = rig.restart()

    assert rig.restarts.marked_years == []
    assert rig.windows.advanced == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_window_another_check_marked_first_starts_no_call() -> None:
    rig, _stored, _payload = _idle_window_rig()
    rig.restarts.marked_elsewhere = True

    summary = rig.restart()

    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_window_whose_first_years_call_cannot_be_rebuilt_fails_with_the_reason() -> (
    None
):
    """The year records why, as a row the check can't rebuild is failed at once."""
    rig, stored, _payload = _idle_window_rig()
    rig.restarts.read_failures[stored.report_run_id] = StoredRunUnreadableError(
        "report run names no region"
    )

    summary = rig.restart()

    assert rig.windows.refusals == [NOT_REBUILT]
    assert rig.economy_caller.report_runs == []
    assert rig.restarts.marked_years == []
    assert summary == RestartSummary(listed=1, failed=1)
    assert rig.telemetry.outcomes == [
        (
            RestartKind.BUDGET_WINDOW,
            WINDOW_RUN_ID,
            RestartOutcome.FAILED,
            "StoredRunUnreadableError",
        )
    ]


def test_a_refusal_never_fails_a_year_other_than_the_one_not_rebuilt() -> None:
    """The year read was claimed before the advance, which offers the next first."""
    rig, stored, _payload = _idle_window_rig()
    rig.restarts.read_failures[stored.report_run_id] = StoredRunUnreadableError(
        "report run names no region"
    )
    rig.windows.waiting = rig.windows.waiting[1:]

    summary = rig.restart()

    assert rig.windows.refusals == []
    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_refused_year_its_own_call_claimed_first_is_left() -> None:
    """The year is under way after all; the later years wait for its coordinator."""
    rig, stored, _payload = _idle_window_rig()
    rig.restarts.read_failures[stored.report_run_id] = StoredRunUnreadableError(
        "report run names no region"
    )
    rig.windows.claimed_meanwhile = frozenset({stored.report_run_id})

    summary = rig.restart()

    assert rig.windows.refusals == []
    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_marked_window_whose_refusal_cannot_be_written_is_left_to_the_sweep() -> None:
    rig, stored, _payload = _idle_window_rig(
        economy_caller=FakeCaller(failure=ConnectionError("Modal is down"))
    )
    rig.windows.refusal_not_written = DATABASE_ERROR

    summary = rig.restart()

    assert _marked_run_ids(rig) == [stored.report_run_id]
    assert summary == RestartSummary(listed=1, left=1)
    assert rig.telemetry.outcomes[0][3] == "OperationalError"


def test_an_unexpected_error_from_the_mark_defers_the_window_and_is_logged(
    caplog,
) -> None:
    """It is no refusal by Modal, so no year is failed for it."""
    rig, _stored, _payload = _idle_window_rig()
    rig.restarts.mark_failure = RuntimeError("unforeseen")

    summary = rig.restart()

    assert rig.windows.refusals == []
    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)
    assert rig.telemetry.outcomes[0][3] == "RuntimeError"
    (logged,) = [
        record
        for record in caplog.records
        if record.getMessage() == "v2_run_restart_unexpected_error"
    ]
    assert isinstance(logged.exc_info[1], RuntimeError)
    assert logged.policyengine_attributes == {
        "run_kind": "budget_window",
        "run_id": str(WINDOW_RUN_ID),
    }


def test_a_database_error_before_any_start_leaves_the_window_for_the_next_check() -> (
    None
):
    """Nothing started and nothing is marked, so the next check restarts it."""
    rig, _stored, _payload = _idle_window_rig(
        windows=FakeWindows(failure=DATABASE_ERROR)
    )

    summary = rig.restart()

    assert rig.restarts.marked_years == []
    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)
    assert rig.telemetry.outcomes[0][2:] == (
        RestartOutcome.DEFERRED,
        "OperationalError",
    )


@pytest.mark.parametrize(
    "failure",
    [DATABASE_ERROR, RuntimeError("unforeseen")],
    ids=["database-error", "unexpected-error"],
)
def test_an_advance_that_fails_after_its_starts_counts_the_calls_not_recorded(
    failure,
) -> None:
    """The calls Modal accepted claim their years themselves."""
    rig, stored, _payload = _idle_window_rig()
    rig.windows.failure_after_starts = failure

    summary = rig.restart()

    assert _marked_run_ids(rig) == [stored.report_run_id]
    assert len(rig.economy_caller.report_runs) == 2
    assert summary == RestartSummary(listed=1, call_not_recorded=1)
    assert rig.telemetry.outcomes[0][3] == type(failure).__name__


def test_a_mark_that_cannot_be_written_starts_no_call() -> None:
    rig, _stored, _payload = _idle_window_rig()
    rig.restarts.mark_failure = DATABASE_ERROR

    summary = rig.restart()

    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)
    assert rig.telemetry.outcomes[0][3] == "OperationalError"


def test_an_unexpected_error_in_a_windows_restart_defers_it_and_the_check_goes_on(
    caplog,
) -> None:
    """One window the check can't restart for an unforeseen reason holds up no row.

    The error is logged with its traceback, so it can be diagnosed.
    """
    stored, _call = submitted_report()
    window_year = replace(stored, report_run_id=UUID(int=2026))
    later_row = UnstartedRun(
        kind=RestartKind.REPORT_RUN,
        run_id=stored.report_run_id,
        created_at=CREATED_AT + timedelta(seconds=1),
    )
    rig = restart_rig(
        restarts=FakeRestartStore(
            unstarted=(later_row,),
            idle_windows=(_idle_window(),),
            report_runs={
                stored.report_run_id: stored,
                window_year.report_run_id: window_year,
            },
            waiting_years={
                WINDOW_RUN_ID: WaitingYear(
                    report_run_id=window_year.report_run_id, year=2026
                )
            },
        ),
        windows=FakeWindows(failure=RuntimeError("unforeseen")),
    )

    summary = rig.restart()

    assert rig.telemetry.outcomes[0] == (
        RestartKind.BUDGET_WINDOW,
        WINDOW_RUN_ID,
        RestartOutcome.DEFERRED,
        "RuntimeError",
    )
    assert rig.restarts.marked_years == []
    assert [run.run_id for run in rig.restarts.marked] == [stored.report_run_id]
    assert summary == RestartSummary(listed=2, restarted=1, deferred=1)
    (logged,) = [
        record
        for record in caplog.records
        if record.getMessage() == "v2_run_restart_unexpected_error"
    ]
    assert isinstance(logged.exc_info[1], RuntimeError)
    assert logged.policyengine_attributes == {
        "run_kind": "budget_window",
        "run_id": str(WINDOW_RUN_ID),
    }


def test_a_window_gone_before_its_advance_is_left() -> None:
    rig, _stored, _payload = _idle_window_rig(
        windows=FakeWindows(failure=LookupError("budget-window run does not exist"))
    )

    summary = rig.restart()

    assert rig.restarts.marked_years == []
    assert summary == RestartSummary(listed=1, left=1)


@pytest.mark.parametrize(
    "ended",
    [
        # A year failed meanwhile, so this advance fails the window.
        WindowAdvance(status=RunStatus.FAILED, changed=True),
        # The window had already finished.
        WindowAdvance(status=RunStatus.SUCCEEDED, changed=False),
    ],
    ids=["failed-now", "finished"],
)
def test_a_window_that_ends_without_a_start_is_left_unmarked(ended) -> None:
    """The restart started nothing, so it failed nothing either."""
    rig, _stored, _payload = _idle_window_rig(windows=FakeWindows(ended=ended))

    summary = rig.restart()

    assert rig.restarts.marked_years == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_window_the_check_has_no_time_left_for_is_left_for_the_next_check() -> None:
    # The check starts, takes the window up, then runs out of time once its
    # first year's call is rebuilt.
    timer_readings = itertools.chain(
        [0.0, 0.0], itertools.repeat(CHECK_TIME_LIMIT.total_seconds())
    )
    rig, _stored, _payload = _idle_window_rig(timer=lambda: next(timer_readings))

    summary = rig.restart()

    assert rig.windows.advanced == []
    assert rig.restarts.marked_years == []
    assert summary == RestartSummary(listed=1, deferred=1)


def test_a_window_whose_manifest_cannot_be_read_is_left_for_the_next_check() -> None:
    rig, _stored, _payload = _idle_window_rig(manifest={})

    summary = rig.restart()

    assert rig.windows.advanced == []
    assert summary == RestartSummary(listed=1, deferred=1)


def test_a_windows_first_year_restarts_with_the_call_its_window_started() -> None:
    """The restarted first year's call is the one the window's submission started."""
    window_store = FakeWindowStore()
    caller = FakeCaller()
    submissions = ReportSubmissionService(
        windows=FakeAdvancer(window_store),
        store=window_store,
        workers=ManifestEconomyWorkers(MANIFEST_STORE),
        caller=caller,
        settings=ServiceSettings(
            environment="staging",
            modal_environment="staging",
            output_bucket=OUTPUT_BUCKET,
        ),
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
        clock=lambda: CREATED_AT,
    )
    request = BudgetWindowReportRequest.model_validate(WINDOW_BODY)
    asyncio.run(
        submissions.submit_budget_window(
            request, request_id="original-request", observability_context=None
        )
    )
    (report,) = window_store.windows
    first_year = report.first_year
    (worker, payload, context, _observability) = caller.report_runs[0]
    stored = StoredReportRun(
        report_run_id=first_year.report_run_id,
        country="us",
        versions=worker.versions,
        options=report.inputs,
        scope=EconomyScope(
            catalog_version_id=CATALOG_VERSION_ID,
            region_code="us",
            dataset_name="populace_us_2024",
            year=first_year.year,
        ),
        baseline=StoredEconomySide(
            simulation_run_id=first_year.baseline.simulation_run_id,
            choice=SimulationChoice(policy_id=None, dynamic_id=None),
            reform={},
        ),
        reform=StoredEconomySide(
            simulation_run_id=first_year.reform.simulation_run_id,
            choice=SimulationChoice(policy_id=POLICY_ID, dynamic_id=DYNAMIC_ID),
            reform=REFORM,
        ),
        created_at=CREATED_AT,
    )
    rig = restart_rig(
        restarts=FakeRestartStore(
            idle_windows=(_idle_window(run_id=report.window_run_id),),
            report_runs={stored.report_run_id: stored},
            waiting_years={
                report.window_run_id: WaitingYear(
                    report_run_id=stored.report_run_id, year=first_year.year
                )
            },
        ),
        windows=FakeWindows(waiting=(_year_to_start(stored, first_year.year),)),
    )

    summary = rig.restart()

    (restarted,) = rig.economy_caller.report_runs
    assert restarted[1] == payload
    assert _without_request_id(restarted[2]) == _without_request_id(context)
    assert summary == RestartSummary(listed=1, restarted=1)


def _without_request_id(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != "request_id"}
