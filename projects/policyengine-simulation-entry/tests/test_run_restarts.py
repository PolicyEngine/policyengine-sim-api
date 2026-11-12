"""Starting a run or household job again when its Modal call never started.

The restart check rebuilds a run's call from its stored rows. Each test of a
restart first submits the run through the service that creates it, captures
the call that submission started, and requires the restart to start the same
call: the same payload, and the same context apart from the request ID. The
store's reads are tested against PostgreSQL in the persistence library; here
fakes answer as the stored rows would.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from economy_fixtures import economy_worker_version
from economy_service_fakes import (
    CATALOG_VERSION_ID,
    CREATED_AT,
    DYNAMIC_ID,
    POLICY_ID,
    REFORM,
    FakeCaller,
    FakeStore,
    report_request,
    service,
    simulation_request,
)
from household_fixtures import manifest_store
from policyengine_simulation_entry.economy_workers import ManifestEconomyWorkers
from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationRequest,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationService,
)
from policyengine_simulation_entry.household_workers import (
    ManifestHouseholdWorkers,
)
from policyengine_simulation_entry.run_restarts import (
    CHECK_TIME_LIMIT,
    RESTART_AFTER,
    RESTART_UNTIL,
    RESTARTS_PER_CHECK,
    RestartCallers,
    RestartOutcome,
    RestartSettings,
    RestartStores,
    RestartSummary,
    RestartWorkers,
    RunRestartService,
)
from policyengine_simulation_persistence import (
    EconomyScope,
    HouseholdCalculationInputs,
    HouseholdJobRecord,
    HouseholdJobStatus,
    RestartKind,
    RunCall,
    SimulationChoice,
    StoredEconomySide,
    StoredHousehold,
    StoredReportRun,
    StoredRunUnreadableError,
    StoredSimulationRun,
    UnstartedRun,
)
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from policyengine_simulation_entry.household_workers import HouseholdWorker
    from policyengine_simulation_persistence import NewHouseholdJob

NOW = CREATED_AT + timedelta(minutes=4)
DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))
HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-0000000000bb")
HOUSEHOLD_JOB_ID = UUID("00000000-0000-4000-8000-0000000000aa")
HOUSEHOLD_DOCUMENT = {
    "people": [
        {
            "id": "you",
            "values": {"age": 40, "employment_income": 30_000},
            "memberships": {
                "household": "your household",
                "family": "your family",
                "tax_unit": "your tax unit",
                "spm_unit": "your spm unit",
                "marital_unit": "your marital unit",
            },
        }
    ],
    "household": [{"id": "your household", "values": {"state_name": "TX"}}],
    "family": [{"id": "your family", "values": {}}],
    "tax_unit": [{"id": "your tax unit", "values": {}}],
    "spm_unit": [{"id": "your spm unit", "values": {}}],
    "marital_unit": [{"id": "your marital unit", "values": {}}],
}
SETTINGS = RestartSettings(environment="staging", modal_environment="staging")
# The manifest both a submission and its restart read: the call's context
# records the digest of the manifest its worker was read from.
MANIFEST_STORE = manifest_store(economy_worker_version())


@dataclass
class FakeRestartStore:
    """Lists the unstarted runs it is given and reads back their stored rows.

    ``marked_elsewhere`` makes every mark change nothing, as when another
    check marked the run first.
    """

    unstarted: tuple[UnstartedRun, ...] = ()
    report_runs: dict[UUID, StoredReportRun] = field(default_factory=dict)
    simulation_runs: dict[UUID, StoredSimulationRun] = field(default_factory=dict)
    report_simulation_run_ids: dict[UUID, tuple[UUID, ...]] = field(
        default_factory=dict
    )
    read_failure: Exception | None = None
    # A read of one run that raises, by run ID.
    read_failures: dict[UUID, Exception] = field(default_factory=dict)
    marked_elsewhere: bool = False
    # Each row's status and call as stored, by run ID; a row not named here is
    # as the rig's other fakes left it (``holder``).
    held: dict[UUID, RunCall | None] = field(default_factory=dict)
    holder: Callable[[UnstartedRun], RunCall | None] | None = None
    windows: list[tuple[datetime, datetime, int]] = field(default_factory=list)
    marked: list[UnstartedRun] = field(default_factory=list)

    def read_call(self, run: UnstartedRun) -> RunCall | None:
        if run.run_id in self.held:
            return self.held[run.run_id]
        return None if self.holder is None else self.holder(run)

    def read_report_run_simulation_run_ids(
        self, report_run_id: UUID
    ) -> tuple[UUID, ...]:
        return self.report_simulation_run_ids.get(report_run_id, ())

    def list_unstarted(
        self, *, created_after: datetime, created_before: datetime, limit: int
    ) -> tuple[UnstartedRun, ...]:
        self.windows.append((created_after, created_before, limit))
        return self.unstarted

    def mark_restart(self, run: UnstartedRun) -> bool:
        if self.marked_elsewhere:
            return False
        self.marked.append(run)
        return True

    def read_report_run(self, report_run_id: UUID) -> StoredReportRun | None:
        if self.read_failure is not None:
            raise self.read_failure
        if report_run_id in self.read_failures:
            raise self.read_failures[report_run_id]
        return self.report_runs.get(report_run_id)

    def read_simulation_run(
        self, simulation_run_id: UUID
    ) -> StoredSimulationRun | None:
        if self.read_failure is not None:
            raise self.read_failure
        return self.simulation_runs.get(simulation_run_id)


@dataclass
class FakeHouseholdJobs:
    """Holds the household jobs created, and answers the restart's reads of them."""

    default_year: int | None = 2026
    reform: dict[str, dict[str, object]] = field(default_factory=dict)
    claimed: bool = False
    created: list[NewHouseholdJob] = field(default_factory=list)
    recorded: list[tuple[UUID, str]] = field(default_factory=list)
    failed: list[tuple[UUID, str]] = field(default_factory=list)

    def read_inputs(self, **arguments: Any) -> HouseholdCalculationInputs:
        return HouseholdCalculationInputs(
            catalog_version_id=CATALOG_VERSION_ID,
            household=StoredHousehold(
                id=arguments["household_id"],
                country=arguments["country"],
                default_year=self.default_year,
                document=HOUSEHOLD_DOCUMENT,
            ),
            reform=self.reform,
        )

    def create_job(self, job: NewHouseholdJob) -> None:
        self.created.append(job)

    def record_call(self, job_id: UUID, modal_call_id: str) -> HouseholdJobStatus:
        self.recorded.append((job_id, modal_call_id))
        return (
            HouseholdJobStatus.RUNNING if self.claimed else HouseholdJobStatus.PENDING
        )

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        if self.claimed:
            return False
        self.failed.append((job_id, error_message))
        return True

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        job = next((job for job in self.created if job.id == job_id), None)
        if job is None:
            return None
        return HouseholdJobRecord(
            id=job.id,
            status=HouseholdJobStatus.PENDING,
            country=job.country,
            household_id=job.household_id,
            policy_id=job.policy_id,
            year=job.year,
            request_data=job.request_data,
            tax_benefit_model_version_id=job.tax_benefit_model_version_id,
            country_package_version=job.country_package_version,
            policyengine_version=job.policyengine_version,
            modal_call_id=None,
            result=None,
            error_message=None,
            created_at=CREATED_AT,
            started_at=None,
            completed_at=None,
        )


@dataclass
class FakeHouseholdCaller:
    """Records each household function call; ``failure`` is raised instead."""

    failure: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def spawn(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        household_job_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {
                "worker": worker,
                "payload": payload,
                "household_job_id": household_job_id,
                "observability_context": observability_context,
            }
        )
        return "fc-household-restarted"


@dataclass
class RecordingTelemetry:
    """Records each restart scope and outcome the check reports."""

    scopes: list[tuple[RestartKind, UUID, float]] = field(default_factory=list)
    outcomes: list[tuple[RestartKind, UUID, RestartOutcome, str | None]] = field(
        default_factory=list
    )

    @contextmanager
    def restart_scope(self, run: UnstartedRun, *, age_seconds: float) -> Iterator[None]:
        self.scopes.append((run.kind, run.run_id, age_seconds))
        yield

    def record_outcome(
        self,
        run: UnstartedRun,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        self.outcomes.append((run.kind, run.run_id, outcome, error_type))

    def capture_context(self) -> dict[str, str]:
        return {"traceparent": "00-restart-check"}


@dataclass
class RestartRig:
    """The restart service and every fake it was built with."""

    restarts: FakeRestartStore
    economy: FakeStore
    households: FakeHouseholdJobs
    economy_caller: FakeCaller
    household_caller: FakeHouseholdCaller
    telemetry: RecordingTelemetry
    service: RunRestartService

    def restart(self) -> RestartSummary:
        return asyncio.run(self.service.restart_unstarted_runs())


def _rig(  # noqa: PLR0913  # each fake a test may replace
    *,
    restarts: FakeRestartStore | None = None,
    economy: FakeStore | None = None,
    households: FakeHouseholdJobs | None = None,
    economy_caller: FakeCaller | None = None,
    household_caller: FakeHouseholdCaller | None = None,
    manifest: object | None = None,
    timer: Callable[[], float] | None = None,
) -> RestartRig:
    restarts = restarts or FakeRestartStore()
    economy = economy or FakeStore()
    households = households or FakeHouseholdJobs()
    economy_caller = economy_caller or FakeCaller()
    household_caller = household_caller or FakeHouseholdCaller()
    telemetry = RecordingTelemetry()
    store = MANIFEST_STORE if manifest is None else manifest

    def holder(run: UnstartedRun) -> RunCall:
        """Return the row as the fakes left it: claimed, given a call, or not."""
        if economy.claimed or households.claimed:
            return RunCall(status="running", modal_call_id="fc-original")
        recorded = dict([*economy.recorded_calls, *households.recorded])
        return RunCall(status="pending", modal_call_id=recorded.get(run.run_id))

    if restarts.holder is None:
        restarts.holder = holder
    restart_service = RunRestartService(
        stores=RestartStores(restarts=restarts, economy=economy, households=households),
        workers=RestartWorkers(
            economy=ManifestEconomyWorkers(store),
            households=ManifestHouseholdWorkers(store),
        ),
        callers=RestartCallers(economy=economy_caller, households=household_caller),
        settings=SETTINGS,
        telemetry=telemetry,
        clock=lambda: NOW,
        timer=timer or (lambda: 0.0),
        call_start_timeout_seconds=0.05,
        new_request_id=lambda: "restart-request",
    )
    return RestartRig(
        restarts=restarts,
        economy=economy,
        households=households,
        economy_caller=economy_caller,
        household_caller=household_caller,
        telemetry=telemetry,
        service=restart_service,
    )


def _submitted_report() -> tuple[StoredReportRun, tuple[dict, dict]]:
    """Submit a report as the service does; return its stored run and its call."""
    caller = FakeCaller()
    created = asyncio.run(
        service(
            FakeStore(), workers=ManifestEconomyWorkers(MANIFEST_STORE), caller=caller
        ).submit_report(
            report_request(), request_id="original-request", observability_context=None
        )
    )
    (worker, payload, context, _observability) = caller.report_runs[0]
    stored = StoredReportRun(
        report_run_id=created.report_run_id,
        country="us",
        versions=worker.versions,
        options=report_request().options(),
        scope=EconomyScope(
            catalog_version_id=CATALOG_VERSION_ID,
            region_code="us",
            dataset_name="populace_us_2024",
            year=2026,
        ),
        baseline=StoredEconomySide(
            simulation_run_id=created.baseline_simulation_run_id,
            choice=SimulationChoice(policy_id=None, dynamic_id=None),
            reform={},
        ),
        reform=StoredEconomySide(
            simulation_run_id=created.reform_simulation_run_id,
            choice=SimulationChoice(policy_id=POLICY_ID, dynamic_id=DYNAMIC_ID),
            reform=REFORM,
        ),
        created_at=CREATED_AT,
    )
    return stored, (payload, context)


def _submitted_simulation() -> tuple[StoredSimulationRun, tuple[dict, dict]]:
    """Submit a standalone simulation; return its stored run and its call."""
    caller = FakeCaller()
    created = asyncio.run(
        service(
            FakeStore(), workers=ManifestEconomyWorkers(MANIFEST_STORE), caller=caller
        ).submit_simulation(
            simulation_request(),
            request_id="original-request",
            observability_context=None,
        )
    )
    (worker, payload, context, _observability) = caller.simulation_runs[0]
    stored = StoredSimulationRun(
        country="us",
        versions=worker.versions,
        scope=EconomyScope(
            catalog_version_id=CATALOG_VERSION_ID,
            region_code="us",
            dataset_name="populace_us_2024",
            year=2026,
        ),
        run=StoredEconomySide(
            simulation_run_id=created.simulation_run_id,
            choice=SimulationChoice(policy_id=POLICY_ID, dynamic_id=None),
            reform=REFORM,
        ),
        created_at=CREATED_AT,
    )
    return stored, (payload, context)


def _submitted_household_job(
    households: FakeHouseholdJobs, **request_fields: Any
) -> dict[str, Any]:
    """Submit a household calculation into ``households``; return its call's payload."""
    caller = FakeHouseholdCaller()
    household_service = HouseholdCalculationService(
        store=households,
        workers=ManifestHouseholdWorkers(MANIFEST_STORE),
        caller=caller,
        new_job_id=lambda: HOUSEHOLD_JOB_ID,
    )
    asyncio.run(
        household_service.submit(
            HouseholdCalculationRequest.model_validate(
                {"country": "us", "household_id": str(HOUSEHOLD_ID), **request_fields}
            ),
            observability_context=None,
        )
    )
    households.recorded.clear()
    return caller.calls[0]["payload"]


def _unstarted(kind: RestartKind, run_id: UUID) -> UnstartedRun:
    return UnstartedRun(kind=kind, run_id=run_id, created_at=CREATED_AT)


def _without_request_id(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != "request_id"}


def test_the_check_lists_runs_created_three_to_ten_minutes_ago() -> None:
    rig = _rig()

    summary = rig.restart()

    assert rig.restarts.windows == [
        (NOW - RESTART_UNTIL, NOW - RESTART_AFTER, RESTARTS_PER_CHECK)
    ]
    assert (
        timedelta(minutes=3),
        timedelta(minutes=10),
    ) == (RESTART_AFTER, RESTART_UNTIL)
    assert summary == RestartSummary(listed=0)


def test_a_report_run_is_started_again_with_the_call_its_submission_started() -> None:
    stored, (payload, context) = _submitted_report()
    run = _unstarted(RestartKind.REPORT_RUN, stored.report_run_id)
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(run,), report_runs={stored.report_run_id: stored}
        )
    )

    summary = rig.restart()

    ((_worker, restarted_payload, restarted_context, observability),) = (
        rig.economy_caller.report_runs
    )
    assert restarted_payload == payload
    assert _without_request_id(restarted_context) == _without_request_id(context)
    assert restarted_context["request_id"] == "restart-request"
    assert observability == {"traceparent": "00-restart-check"}
    assert rig.restarts.marked == [run]
    assert rig.economy.recorded_calls == [(stored.report_run_id, "fc-coordinator")]
    assert summary == RestartSummary(listed=1, restarted=1)


def test_a_standalone_simulation_run_is_started_again_with_its_submissions_call() -> (
    None
):
    stored, (payload, context) = _submitted_simulation()
    run_id = stored.run.simulation_run_id
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.SIMULATION_RUN, run_id),),
            simulation_runs={run_id: stored},
        )
    )

    summary = rig.restart()

    ((_worker, restarted_payload, restarted_context, _observability),) = (
        rig.economy_caller.simulation_runs
    )
    assert restarted_payload == payload
    assert _without_request_id(restarted_context) == _without_request_id(context)
    assert rig.economy.recorded_calls == [(run_id, "fc-worker")]
    assert summary == RestartSummary(listed=1, restarted=1)


def test_a_household_job_is_started_again_with_its_submissions_call() -> None:
    households = FakeHouseholdJobs(reform=REFORM)
    payload = _submitted_household_job(
        households, policy_id=str(POLICY_ID), add_missing_variables=True
    )
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.HOUSEHOLD_JOB, HOUSEHOLD_JOB_ID),)
        ),
        households=households,
    )

    summary = rig.restart()

    (call,) = rig.household_caller.calls
    assert call["payload"] == payload
    assert call["household_job_id"] == HOUSEHOLD_JOB_ID
    assert call["observability_context"] == {"traceparent": "00-restart-check"}
    assert households.recorded == [(HOUSEHOLD_JOB_ID, "fc-household-restarted")]
    assert summary == RestartSummary(listed=1, restarted=1)


def test_a_run_another_check_marked_first_is_left_without_a_call() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
            marked_elsewhere=True,
        )
    )

    summary = rig.restart()

    assert rig.economy_caller.report_runs == []
    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, left=1)


def test_a_run_gone_before_it_is_read_is_left_alone() -> None:
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(
                _unstarted(RestartKind.REPORT_RUN, UUID(int=1)),
                _unstarted(RestartKind.SIMULATION_RUN, UUID(int=2)),
                _unstarted(RestartKind.HOUSEHOLD_JOB, UUID(int=3)),
            )
        )
    )

    summary = rig.restart()

    assert rig.restarts.marked == []
    assert summary == RestartSummary(listed=3, left=3)


def test_a_run_whose_rows_cannot_be_read_is_left_for_the_next_check() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            read_failure=DATABASE_ERROR,
        )
    )

    summary = rig.restart()

    assert rig.restarts.marked == []
    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)


def test_an_unreadable_manifest_leaves_the_run_for_the_next_check() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        manifest={},
    )

    summary = rig.restart()

    assert rig.restarts.marked == []
    assert summary == RestartSummary(listed=1, deferred=1)


def test_a_run_whose_worker_left_the_manifest_is_failed_as_not_started() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        manifest=manifest_store(economy_worker_version("6.18.0")),
    )

    summary = rig.restart()

    ((run_id, simulation_run_ids, message),) = rig.economy.failed_runs
    assert run_id == stored.report_run_id
    assert simulation_run_ids == (
        stored.baseline.simulation_run_id,
        stored.reform.simulation_run_id,
    )
    assert message.startswith("run_not_started: ")
    assert f".py {stored.versions.policyengine_version}" in message
    assert rig.economy_caller.report_runs == []
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_run_whose_stored_rows_no_longer_describe_it_is_failed() -> None:
    run_id = UUID(int=7)
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.SIMULATION_RUN, run_id),),
            read_failure=StoredRunUnreadableError("simulation run 7 names no region"),
        )
    )

    summary = rig.restart()

    ((failed_run_id, _sides, message),) = rig.economy.failed_runs
    assert failed_run_id == run_id
    assert message.startswith("run_not_started: ")
    assert "stored rows" in message
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_household_job_whose_household_changed_is_failed_as_not_started() -> None:
    households = FakeHouseholdJobs()
    _submitted_household_job(households)
    households.default_year = 2027
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.HOUSEHOLD_JOB, HOUSEHOLD_JOB_ID),)
        ),
        households=households,
    )

    summary = rig.restart()

    ((job_id, message),) = households.failed
    assert job_id == HOUSEHOLD_JOB_ID
    assert message.startswith("run_not_started: ")
    assert rig.household_caller.calls == []
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_restart_modal_refuses_fails_the_run_as_not_started() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        economy_caller=FakeCaller(failure=ConnectionError("Modal is down")),
    )

    summary = rig.restart()

    ((run_id, simulation_run_ids, message),) = rig.economy.failed_runs
    assert run_id == stored.report_run_id
    assert len(simulation_run_ids) == 2
    assert message == (
        "run_not_started: The run stayed pending with no Modal call recorded for "
        "it, and Modal did not accept the call that started it again "
        "(ConnectionError)."
    )
    assert summary == RestartSummary(listed=1, failed=1)
    assert rig.telemetry.outcomes == [
        (
            RestartKind.REPORT_RUN,
            stored.report_run_id,
            RestartOutcome.FAILED,
            "ConnectionError",
        )
    ]


def test_a_restart_modal_does_not_confirm_in_time_fails_the_run() -> None:
    stored, _call = _submitted_simulation()
    run_id = stored.run.simulation_run_id
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.SIMULATION_RUN, run_id),),
            simulation_runs={run_id: stored},
        ),
        economy_caller=FakeCaller(hang=True),
    )

    summary = rig.restart()

    ((failed_run_id, _sides, message),) = rig.economy.failed_runs
    assert failed_run_id == run_id
    assert "(TimeoutError)" in message
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_refused_restart_of_a_run_another_call_claimed_leaves_it() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        economy=FakeStore(claimed=True),
        economy_caller=FakeCaller(failure=ConnectionError("Modal is down")),
    )

    summary = rig.restart()

    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, started_meanwhile=1)


def test_a_restarted_call_another_call_claimed_first_counts_as_started_meanwhile() -> (
    None
):
    households = FakeHouseholdJobs()
    _submitted_household_job(households)
    households.claimed = True
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.HOUSEHOLD_JOB, HOUSEHOLD_JOB_ID),)
        ),
        households=households,
    )

    summary = rig.restart()

    assert len(rig.household_caller.calls) == 1
    assert summary == RestartSummary(listed=1, started_meanwhile=1)


def test_a_restart_whose_call_cannot_be_recorded_still_counts_its_call() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        economy=FakeStore(record_failure=DATABASE_ERROR),
    )

    summary = rig.restart()

    assert len(rig.economy_caller.report_runs) == 1
    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, call_not_recorded=1)


def test_each_listed_run_is_one_telemetry_scope_with_its_age_and_outcome() -> None:
    report, _report_call = _submitted_report()
    simulation, _simulation_call = _submitted_simulation()
    simulation_run_id = simulation.run.simulation_run_id
    report_run = replace(
        _unstarted(RestartKind.REPORT_RUN, report.report_run_id),
        created_at=CREATED_AT + timedelta(seconds=30),
    )
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(
                report_run,
                _unstarted(RestartKind.SIMULATION_RUN, simulation_run_id),
            ),
            report_runs={report.report_run_id: report},
        )
    )

    summary = rig.restart()

    assert rig.telemetry.scopes == [
        (RestartKind.REPORT_RUN, report.report_run_id, 210.0),
        (RestartKind.SIMULATION_RUN, simulation_run_id, 240.0),
    ]
    assert rig.telemetry.outcomes == [
        (RestartKind.REPORT_RUN, report.report_run_id, RestartOutcome.RESTARTED, None),
        (RestartKind.SIMULATION_RUN, simulation_run_id, RestartOutcome.LEFT, None),
    ]
    assert summary == RestartSummary(listed=2, restarted=1, left=1)


@pytest.mark.parametrize(
    ("modal_environment", "environment"),
    [("staging", "staging"), ("main", "production")],
)
def test_the_deployment_label_follows_the_modal_environment(
    modal_environment, environment
) -> None:
    assert RestartSettings.for_modal_environment(modal_environment) == (
        RestartSettings(environment=environment, modal_environment=modal_environment)
    )


def test_a_modal_environment_no_deployment_uses_is_refused() -> None:
    with pytest.raises(ValueError, match="dev"):
        RestartSettings.for_modal_environment("dev")


def test_an_unreadable_report_run_is_failed_with_its_simulation_runs() -> None:
    """As a submission fails a report run, with the simulation runs made for it."""
    run_id = UUID(int=8)
    sides = (UUID(int=81), UUID(int=82))
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, run_id),),
            read_failure=StoredRunUnreadableError("report run 8 names no region"),
            report_simulation_run_ids={run_id: sides},
        )
    )

    summary = rig.restart()

    ((failed_run_id, simulation_run_ids, message),) = rig.economy.failed_runs
    assert (failed_run_id, simulation_run_ids) == (run_id, sides)
    assert message.startswith("run_not_started: ")
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_restarted_call_claiming_its_run_before_the_record_is_restarted() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
            held={
                stored.report_run_id: RunCall(
                    status="running", modal_call_id="fc-coordinator"
                )
            },
        ),
        economy=FakeStore(claimed=True),
    )

    assert rig.restart() == RestartSummary(listed=1, restarted=1)


def test_a_run_failed_before_its_restart_was_recorded_is_left() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
            held={stored.report_run_id: RunCall(status="failed", modal_call_id=None)},
        ),
        economy=FakeStore(claimed=True),
    )

    assert rig.restart() == RestartSummary(listed=1, left=1)


def test_a_refused_restart_of_a_run_the_sweep_failed_meanwhile_is_left() -> None:
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
            held={stored.report_run_id: RunCall(status="failed", modal_call_id=None)},
        ),
        economy=FakeStore(claimed=True),
        economy_caller=FakeCaller(failure=ConnectionError("Modal is down")),
    )

    assert rig.restart() == RestartSummary(listed=1, left=1)


@dataclass
class AdvancingCaller(FakeCaller):
    """Starts calls as ``FakeCaller`` does, moving a timer on by each start."""

    elapsed: list[float] = field(default_factory=lambda: [0.0])
    step_seconds: float = 0.0

    async def start_report_run(self, *arguments: Any, **keywords: Any) -> str:
        self.elapsed[0] += self.step_seconds
        return await super().start_report_run(*arguments, **keywords)


def test_a_check_past_its_time_limit_leaves_the_other_runs_for_the_next() -> None:
    """No row is marked once the check's time is up, so none waits for the sweep."""
    first, _first_call = _submitted_report()
    second, _second_call = _submitted_report()
    caller = AdvancingCaller(step_seconds=CHECK_TIME_LIMIT.total_seconds())
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(
                _unstarted(RestartKind.REPORT_RUN, first.report_run_id),
                _unstarted(RestartKind.REPORT_RUN, second.report_run_id),
            ),
            report_runs={first.report_run_id: first, second.report_run_id: second},
        ),
        economy_caller=caller,
        timer=lambda: caller.elapsed[0],
    )

    summary = rig.restart()

    assert [run.run_id for run in rig.restarts.marked] == [first.report_run_id]
    assert [scope[1] for scope in rig.telemetry.scopes] == [first.report_run_id]
    assert summary == RestartSummary(listed=2, restarted=1, deferred=1)


class LoopWatchingManifestStore:
    """A manifest store that notes whether each read ran on an event loop."""

    def __init__(self, store: dict[str, object]) -> None:
        self.store = store
        self.read_on_event_loop: list[bool] = []

    def get(self, key: str, default: object = None) -> object:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.read_on_event_loop.append(False)
        else:
            self.read_on_event_loop.append(True)
        return self.store.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        raise AssertionError(key)


def test_a_runs_call_is_rebuilt_off_the_event_loop() -> None:
    """Modal's blocking reads, such as the manifest's, never hold up the loop."""
    stored, _call = _submitted_report()
    manifest = LoopWatchingManifestStore(MANIFEST_STORE)
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(_unstarted(RestartKind.REPORT_RUN, stored.report_run_id),),
            report_runs={stored.report_run_id: stored},
        ),
        manifest=manifest,
    )

    assert rig.restart() == RestartSummary(listed=1, restarted=1)
    assert manifest.read_on_event_loop
    assert not any(manifest.read_on_event_loop)


def test_an_unexpected_rebuild_error_defers_its_run_and_the_check_goes_on(
    caplog,
) -> None:
    """One row the check can't rebuild for an unforeseen reason holds up no other.

    The error is logged with its traceback, so it can be diagnosed.
    """
    broken_run_id = UUID(int=9)
    stored, _call = _submitted_report()
    rig = _rig(
        restarts=FakeRestartStore(
            unstarted=(
                _unstarted(RestartKind.REPORT_RUN, broken_run_id),
                _unstarted(RestartKind.REPORT_RUN, stored.report_run_id),
            ),
            report_runs={stored.report_run_id: stored},
            read_failures={broken_run_id: RuntimeError("unforeseen")},
        )
    )

    summary = rig.restart()

    assert rig.telemetry.outcomes[0] == (
        RestartKind.REPORT_RUN,
        broken_run_id,
        RestartOutcome.DEFERRED,
        "RuntimeError",
    )
    assert [run.run_id for run in rig.restarts.marked] == [stored.report_run_id]
    assert summary == RestartSummary(listed=2, restarted=1, deferred=1)
    (logged,) = [
        record
        for record in caplog.records
        if record.getMessage() == "v2_run_restart_unexpected_error"
    ]
    assert logged.exc_info is not None
    assert isinstance(logged.exc_info[1], RuntimeError)
    # The observability runtime's log handler writes only these attributes.
    assert logged.policyengine_attributes == {
        "run_kind": "report_run",
        "run_id": str(broken_run_id),
    }
