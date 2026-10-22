"""In-memory stand-ins for the run transitions and Modal, for the run tests.

``FakeRunTransitions`` keeps each simulation run's and comparison step's status
and applies the same conditional rules as ``RunTransitions``: a change applies
only from the expected status, and the runs of one call change together.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from policyengine_simulation_entry.modal_calls import (
    CallInspectionError,
    CallRunning,
    ModalRequest,
    ModalTarget,
)
from policyengine_simulation_persistence import (
    AbandonedDispatches,
    DueSimulationCall,
    RunError,
    RunStatus,
    SimulationKind,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from policyengine_simulation_entry.modal_calls import CallOutcome


@dataclass
class ManualClock:
    """Return a fixed time until a test advances it."""

    now: datetime = field(
        default_factory=lambda: datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    )

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.now

    def advance(self, interval: timedelta) -> None:
        """Move the fake time forward."""
        self.now += interval


@dataclass
class FakeExecution:
    """One simulation run, or one comparison step, as the fake keeps it."""

    id: UUID
    kind: str
    status: RunStatus = RunStatus.PENDING
    modal_call_id: str | None = None
    next_check_at: datetime | None = None
    check_count: int = 0
    error: RunError | None = None
    updated_at: datetime | None = None
    written_results: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class FakeRunRecord:
    """The run fields the recorder reads from a claimed run."""

    id: UUID
    simulation_kind: SimulationKind
    modal_call_id: str | None
    check_count: int


@dataclass(frozen=True)
class FakeReportRunRecord:
    """The report run fields the recorder reads from a claimed comparison."""

    id: UUID
    report_kind: str
    comparison_modal_call_id: str | None
    comparison_check_count: int


class FakeRunTransitions:
    """Conditional status changes over in-memory runs and comparison steps."""

    def __init__(self, clock: ManualClock) -> None:
        """Start with no runs."""
        self.clock = clock
        # Callers run these methods in worker threads, as with the database.
        self.lock = Lock()
        self.runs: dict[UUID, FakeExecution] = {}
        self.comparisons: dict[UUID, FakeExecution] = {}
        self.comparison_runs_succeeded: dict[UUID, bool] = {}

    # --- Building state --------------------------------------------------

    def add_run(self, kind: str = "economy", **values: Any) -> UUID:
        """Add a simulation run and return its ID."""
        run = FakeExecution(id=uuid4(), kind=kind, **values)
        self.runs[run.id] = run
        return run.id

    def add_comparison(self, *, runs_succeeded: bool = True, **values: Any) -> UUID:
        """Add an economy report run's comparison step and return its ID."""
        comparison = FakeExecution(id=uuid4(), kind="economy", **values)
        self.comparisons[comparison.id] = comparison
        self.comparison_runs_succeeded[comparison.id] = runs_succeeded
        return comparison.id

    def running_call(self, call_id: str, *kinds: str) -> list[UUID]:
        """Add runs already running under ``call_id``, due a check now."""
        return [
            self.add_run(
                kind,
                status=RunStatus.RUNNING,
                modal_call_id=call_id,
                next_check_at=self.clock(),
            )
            for kind in kinds or ("economy",)
        ]

    # --- Dispatch ----------------------------------------------------------

    def _change_runs(
        self, run_ids: Collection[UUID], expected: RunStatus, **values: Any
    ) -> bool:
        with self.lock:
            runs = [self.runs[run_id] for run_id in run_ids]
            if not runs or any(run.status is not expected for run in runs):
                return False
            for run in runs:
                for name, value in values.items():
                    setattr(run, name, value)
                run.updated_at = self.clock()
            return True

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        """Move pending runs to dispatching."""
        return self._change_runs(
            run_ids, RunStatus.PENDING, status=RunStatus.DISPATCHING
        )

    def record_simulation_dispatched(
        self,
        run_ids: Collection[UUID],
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the call and move the runs to running."""
        return self._change_runs(
            run_ids,
            RunStatus.DISPATCHING,
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
            next_check_at=first_check_at,
        )

    def record_simulation_dispatch_failure(
        self, run_ids: Collection[UUID], error: RunError
    ) -> bool:
        """Fail dispatching runs."""
        return self._change_runs(
            run_ids, RunStatus.DISPATCHING, status=RunStatus.FAILED, error=error
        )

    def _change_comparison(
        self, report_run_id: UUID, expected: RunStatus, **values: Any
    ) -> FakeExecution | None:
        with self.lock:
            comparison = self.comparisons[report_run_id]
            if comparison.status is not expected:
                return None
            for name, value in values.items():
                setattr(comparison, name, value)
            comparison.updated_at = self.clock()
            return comparison

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        """Move a pending comparison step whose runs succeeded to dispatching."""
        if not self.comparison_runs_succeeded[report_run_id]:
            return False
        return (
            self._change_comparison(
                report_run_id, RunStatus.PENDING, status=RunStatus.DISPATCHING
            )
            is not None
        )

    def record_comparison_dispatched(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the comparison's call."""
        return (
            self._change_comparison(
                report_run_id,
                RunStatus.DISPATCHING,
                status=RunStatus.RUNNING,
                modal_call_id=modal_call_id,
                next_check_at=first_check_at,
            )
            is not None
        )

    def record_comparison_dispatch_failure(
        self, report_run_id: UUID, error: RunError
    ) -> bool:
        """Fail a dispatching comparison step."""
        return (
            self._change_comparison(
                report_run_id,
                RunStatus.DISPATCHING,
                status=RunStatus.FAILED,
                error=error,
            )
            is not None
        )

    # --- Recording -------------------------------------------------------

    def claim_due_simulation_calls(
        self, *, limit: int, lease: timedelta
    ) -> list[DueSimulationCall]:
        """Claim running runs that are due, grouped by call."""
        now = self.clock()
        due = [
            run
            for run in self.runs.values()
            if run.status is RunStatus.RUNNING
            and run.next_check_at is not None
            and run.next_check_at <= now
        ][:limit]
        calls: dict[str, list[FakeRunRecord]] = {}
        for run in due:
            run.next_check_at = now + lease
            run.check_count += 1
            assert run.modal_call_id is not None
            calls.setdefault(run.modal_call_id, []).append(
                FakeRunRecord(
                    id=run.id,
                    simulation_kind=SimulationKind(run.kind),
                    modal_call_id=run.modal_call_id,
                    check_count=run.check_count,
                )
            )
        return [
            DueSimulationCall(modal_call_id=call_id, runs=tuple(runs))  # type: ignore[arg-type]
            for call_id, runs in sorted(calls.items())
        ]

    def _call_runs(self, modal_call_id: str) -> list[FakeExecution]:
        return [
            run
            for run in self.runs.values()
            if run.modal_call_id == modal_call_id and run.status is RunStatus.RUNNING
        ]

    def record_simulation_call_running(
        self, modal_call_id: str, *, next_check_at: datetime
    ) -> bool:
        """Set the next check of a call's running runs."""
        runs = self._call_runs(modal_call_id)
        for run in runs:
            run.next_check_at = next_check_at
        return bool(runs)

    def complete_simulation_call(self, modal_call_id: str, write_results) -> bool:
        """Mark a call's runs succeeded after the writer succeeds."""
        runs = self._call_runs(modal_call_id)
        if not runs:
            return False
        records = tuple(
            FakeRunRecord(
                id=run.id,
                simulation_kind=SimulationKind(run.kind),
                modal_call_id=run.modal_call_id,
                check_count=run.check_count,
            )
            for run in runs
        )
        connection = object()
        write_results(connection, records)
        for run in runs:
            run.status = RunStatus.SUCCEEDED
            run.next_check_at = None
            run.written_results.append(connection)
        return True

    def fail_simulation_call(self, modal_call_id: str, error: RunError) -> bool:
        """Fail a call's running runs."""
        runs = self._call_runs(modal_call_id)
        for run in runs:
            run.status = RunStatus.FAILED
            run.error = error
            run.next_check_at = None
        return bool(runs)

    def claim_due_comparisons(
        self, *, limit: int, lease: timedelta
    ) -> list[FakeReportRunRecord]:
        """Claim running comparison steps that are due."""
        now = self.clock()
        due = [
            comparison
            for comparison in self.comparisons.values()
            if comparison.status is RunStatus.RUNNING
            and comparison.next_check_at is not None
            and comparison.next_check_at <= now
        ][:limit]
        for comparison in due:
            comparison.next_check_at = now + lease
            comparison.check_count += 1
        return [
            FakeReportRunRecord(
                id=comparison.id,
                report_kind="economy",
                comparison_modal_call_id=comparison.modal_call_id,
                comparison_check_count=comparison.check_count,
            )
            for comparison in due
        ]

    def record_comparison_running(
        self, report_run_id: UUID, *, next_check_at: datetime
    ) -> bool:
        """Set a running comparison step's next check."""
        return (
            self._change_comparison(
                report_run_id, RunStatus.RUNNING, next_check_at=next_check_at
            )
            is not None
        )

    def complete_comparison(self, report_run_id: UUID, write_results) -> bool:
        """Mark a running comparison step succeeded after the writer succeeds."""
        comparison = self.comparisons[report_run_id]
        if comparison.status is not RunStatus.RUNNING:
            return False
        connection = object()
        write_results(
            connection,
            FakeReportRunRecord(
                id=comparison.id,
                report_kind="economy",
                comparison_modal_call_id=comparison.modal_call_id,
                comparison_check_count=comparison.check_count,
            ),
        )
        comparison.status = RunStatus.SUCCEEDED
        comparison.next_check_at = None
        comparison.written_results.append(connection)
        return True

    def fail_comparison(self, report_run_id: UUID, error: RunError) -> bool:
        """Fail a running comparison step."""
        return (
            self._change_comparison(
                report_run_id,
                RunStatus.RUNNING,
                status=RunStatus.FAILED,
                error=error,
                next_check_at=None,
            )
            is not None
        )

    def fail_abandoned_dispatches(
        self, *, claimed_before: datetime
    ) -> AbandonedDispatches:
        """Fail runs and comparison steps dispatching since before the cutoff."""
        failed_runs = []
        for run in self.runs.values():
            if (
                run.status is RunStatus.DISPATCHING
                and run.updated_at is not None
                and run.updated_at < claimed_before
            ):
                run.status = RunStatus.FAILED
                failed_runs.append(run.id)
        failed_comparisons = []
        for comparison in self.comparisons.values():
            if (
                comparison.status is RunStatus.DISPATCHING
                and comparison.updated_at is not None
                and comparison.updated_at < claimed_before
            ):
                comparison.status = RunStatus.FAILED
                failed_comparisons.append(comparison.id)
        return AbandonedDispatches(
            simulation_run_ids=tuple(failed_runs),
            report_run_ids=tuple(failed_comparisons),
        )


class FakeDispatcher:
    """Record spawned requests and return call IDs, or fail like Modal."""

    def __init__(
        self,
        failure: Exception | None = None,
        cancel_failure: Exception | None = None,
    ) -> None:
        """Fail spawns with ``failure`` and cancels with ``cancel_failure``."""
        self.failure = failure
        self.cancel_failure = cancel_failure
        self.requests: list[ModalRequest] = []
        self.cancelled: list[str] = []

    async def cancel(self, modal_call_id: str) -> None:
        """Record the cancelled call, or raise the configured failure."""
        self.cancelled.append(modal_call_id)
        if self.cancel_failure is not None:
            raise self.cancel_failure

    async def spawn(self, request: ModalRequest) -> str:
        """Return a new call ID, or raise the configured failure."""
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return f"fc-{len(self.requests)}"


class FakeInspector:
    """Return a scripted outcome per call ID, or running when none is set."""

    def __init__(self) -> None:
        """Start with every call running."""
        self.outcomes: dict[str, CallOutcome | CallInspectionError] = {}
        self.inspected: list[str] = []

    async def inspect(self, modal_call_id: str) -> CallOutcome:
        """Return the scripted outcome, or raise the scripted error."""
        self.inspected.append(modal_call_id)
        outcome = self.outcomes.get(modal_call_id, CallRunning())
        if isinstance(outcome, CallInspectionError):
            raise outcome
        return outcome


SAMPLE_REQUEST = ModalRequest(
    target=ModalTarget(application="policyengine-simulation-v2", function="run"),
    arguments=({"country": "us"},),
    observability_context={"traceparent": "00-test"},
)


def execution(transitions: FakeRunTransitions, run_id: UUID) -> FakeExecution:
    """Return a copy of a run as the fake keeps it."""
    return replace(transitions.runs[run_id])
