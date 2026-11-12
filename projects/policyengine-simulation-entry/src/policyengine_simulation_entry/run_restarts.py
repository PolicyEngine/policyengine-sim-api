"""Start a run or household job again when its Modal call never started.

The simulation service creates a household job, a report run or a standalone
simulation run ``pending`` with no Modal call ID, starts the call that
calculates it, then records the call's ID. When the service stops between
the insert and the start (a Cloud Run instance shut down, for example),
nothing records a call, and the stuck-run sweep would fail the run 15
minutes after its creation. The restart check, a scheduled Modal function
beside the sweep, starts such a row's call once more:

1. it lists the rows created 3 to 12 minutes ago that are still pending with
   no call and that nothing has changed since their insert;
2. for each, it reads back what the service built the call from and builds
   the same call, on the worker of the .py version the row records;
3. it marks the row, so it is never restarted twice, starts the call, and
   records the call's ID while the row is still pending with none.

A row whose call can't be built again (its worker left the version manifest,
or its stored rows changed), or whose new call Modal refuses, is failed at
once with ``run_not_started``, as the sweep would fail it later. A row whose
rows or manifest can't be read now is left unmarked, for the next check. A
call the service started after all claims the row only while the row records
no call or that call's own ID, so once the restart records its call, the
earlier call's claim is refused; if the earlier call claimed it first, the
restart's call is refused instead.

Each listed row is one telemetry scope, ``restart_scope``, with its age, and
its outcome is reported through ``record_outcome``, so how often runs need a
restart, and how each restart ended, can be counted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol, get_args

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    OverlappingReformError,
    RestartKind,
    StoredRunUnreadableError,
    UnknownCatalogVersionError,
    UnknownDynamicError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.economy_calculation_inputs import (
    CalculationScope,
    CallOrigin,
    DatasetNotInBundleError,
    bundle_dataset,
    report_run_calculation,
    simulation_options,
    simulation_run_calculation,
)
from policyengine_simulation_entry.economy_workers import (
    EconomyWorkerUnavailableError,
)
from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationRequest,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationInternalError,
    HouseholdCalculationUnprocessableError,
    prepare_household_job,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    VersionManifestUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from policyengine_simulation_persistence import (
        EconomyScope,
        HouseholdCalculationInputs,
        HouseholdJobRecord,
        HouseholdJobStatus,
        NewHouseholdJob,
        ReportRunStatus,
        RunVersions,
        SimulationStatus,
        StoredReportRun,
        StoredSimulationRun,
        UnstartedRun,
    )

    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
    )
    from policyengine_simulation_entry.household_workers import (
        HouseholdFunctionCaller,
        HouseholdWorker,
    )

# A row is restarted once it has stayed pending with no call this long, which
# is far longer than a submission takes to start and record its call.
RESTART_AFTER: Final = timedelta(minutes=3)
# A row older than this is left to the stuck-run sweep, which fails it at 15
# minutes, so a restart has time to record its call before the sweep judges it.
RESTART_UNTIL: Final = timedelta(minutes=12)
# At most this many rows are restarted by one check; the next takes the rest.
RESTARTS_PER_CHECK: Final = 50
# How long the check waits for Modal to accept a restarted call.
CALL_START_TIMEOUT_SECONDS: Final = 10.0
# The deployment each Modal environment holding the v2 workers belongs to, as
# the Simulation Entrypoint's deploy pairs them (APP_ENVIRONMENT). A call's
# context and artifact prefix name the deployment.
DEPLOYMENT_ENVIRONMENTS: Final = MappingProxyType(
    {"staging": "staging", "main": "production"}
)
_NOT_STARTED: Final = (
    "run_not_started: The run stayed pending with no Modal call recorded for it"
)
_PENDING: Final = "pending"


class RestartOutcome(StrEnum):
    """How the check ended for one listed row.

    ``restarted``: its new call started and the call's ID was recorded.
    ``call_not_recorded``: its new call started, but the ID couldn't be
    recorded; the call claims the row itself. ``started_meanwhile``: another
    call claimed the row after the check started its own, which that call
    then can't claim. ``left``: the row was gone, or changed before the check
    started a call; also a row whose failure couldn't be written, which the
    sweep fails later. ``deferred``: its rows or the manifest couldn't be
    read, so it is left unmarked for the next check. ``failed``: it couldn't
    be started again and was failed with ``run_not_started``.
    """

    RESTARTED = "restarted"
    CALL_NOT_RECORDED = "call_not_recorded"
    STARTED_MEANWHILE = "started_meanwhile"
    LEFT = "left"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True)
class RestartSettings:
    """The deployment the check restarts runs for.

    ``environment`` labels each call's context and artifacts, and
    ``modal_environment`` holds the v2 workers.
    """

    environment: str
    modal_environment: str

    @classmethod
    def for_modal_environment(cls, modal_environment: str) -> RestartSettings:
        """Return the settings of the deployment whose workers are in the environment.

        Raise ValueError for an environment no deployment uses.
        """
        environment = DEPLOYMENT_ENVIRONMENTS.get(modal_environment)
        if environment is None:
            msg = (
                "no deployment runs the v2 workers in Modal environment "
                f"{modal_environment!r}"
            )
            raise ValueError(msg)
        return cls(environment=environment, modal_environment=modal_environment)


@dataclass(frozen=True)
class RestartSummary:
    """How many rows one check listed, and how each ended (``RestartOutcome``)."""

    listed: int
    restarted: int = 0
    call_not_recorded: int = 0
    started_meanwhile: int = 0
    left: int = 0
    deferred: int = 0
    failed: int = 0


class UnstartedRunSource(Protocol):
    """The restart check's own reads and writes (``RunRestartStore``)."""

    def list_unstarted(
        self, *, created_after: datetime, created_before: datetime, limit: int
    ) -> tuple[UnstartedRun, ...]:
        """Return the oldest restartable rows created in the window."""
        ...

    def mark_restart(self, run: UnstartedRun) -> bool:
        """Mark the row restarted while it is still unstarted."""
        ...

    def read_report_run(self, report_run_id: UUID) -> StoredReportRun | None:
        """Return what the report run's call is built from."""
        ...

    def read_simulation_run(
        self, simulation_run_id: UUID
    ) -> StoredSimulationRun | None:
        """Return what the standalone simulation run's call is built from."""
        ...


class EconomyRunWrites(Protocol):
    """The submission's own call writes on economy runs (``EconomyRunStore``)."""

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        """Record the report run's call ID while it is pending with none."""
        ...

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        """Fail the report run, and its simulation runs, while unstarted."""
        ...

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the simulation run's call ID while it is pending with none."""
        ...

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        """Fail the simulation run while it is pending with no call ID."""
        ...


class HouseholdJobRestarts(Protocol):
    """The household job reads and writes a restart uses (``HouseholdJobStore``)."""

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the job as it stands."""
        ...

    def read_inputs(
        self,
        *,
        country: str,
        household_id: UUID,
        policy_id: UUID | None,
        policyengine_version: str,
    ) -> HouseholdCalculationInputs:
        """Read the catalog version, household and policy together."""
        ...

    def record_call(
        self, job_id: UUID, modal_call_id: str
    ) -> HouseholdJobStatus | None:
        """Record the call ID while the job is pending with none."""
        ...

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        """Fail the job while it is pending with no call ID."""
        ...


class VersionedEconomyWorkers(Protocol):
    """Find a .py version's economy functions (``ManifestEconomyWorkers``)."""

    def worker_for_version(
        self, country: str, policyengine_version: str
    ) -> EconomyWorker:
        """Return the economy functions of the worker for the .py version."""
        ...


class VersionedHouseholdWorkers(Protocol):
    """Find a .py version's household function (``ManifestHouseholdWorkers``)."""

    def worker_for_version(
        self, country: str, policyengine_version: str
    ) -> HouseholdWorker:
        """Return the household function of the worker for the .py version."""
        ...


class RestartTelemetry(Protocol):
    """Where the check reports each listed row (``ObservabilityRestartTelemetry``)."""

    def restart_scope(
        self, run: UnstartedRun, *, age_seconds: float
    ) -> AbstractContextManager[None]:
        """Return the scope one row's restart runs in."""
        ...

    def record_outcome(
        self,
        run: UnstartedRun,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        """Report how the row's restart ended, and the error type that ended it."""
        ...

    def capture_context(self) -> dict[str, str] | None:
        """Return the context a restarted call continues the check's trace from."""
        ...


class NoRestartTelemetry:
    """Report nothing."""

    def restart_scope(
        self,
        run: UnstartedRun,  # noqa: ARG002  # the protocol's argument
        *,
        age_seconds: float,  # noqa: ARG002  # the protocol's argument
    ) -> AbstractContextManager[None]:
        """Return a scope that does nothing."""
        return nullcontext()

    def record_outcome(
        self,
        run: UnstartedRun,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        """Report nothing."""

    def capture_context(self) -> dict[str, str] | None:
        """Return no context."""
        return None


@dataclass(frozen=True)
class RestartStores:
    """The stores a restart reads and writes."""

    restarts: UnstartedRunSource
    economy: EconomyRunWrites
    households: HouseholdJobRestarts


@dataclass(frozen=True)
class RestartWorkers:
    """Where a restart finds the worker of a row's .py version."""

    economy: VersionedEconomyWorkers
    households: VersionedHouseholdWorkers


@dataclass(frozen=True)
class RestartCallers:
    """What starts a restarted call on Modal."""

    economy: EconomyRunCaller
    households: HouseholdFunctionCaller


type CallStart = Callable[[dict[str, Any] | None], Awaitable[str]]


class _DeferredError(Exception):
    """The row's rows or the manifest couldn't be read now."""


class _CannotRebuildError(Exception):
    """The row's call can't be built again; ``reason`` says why, in a sentence part.

    ``simulation_run_ids`` are a report run's simulation runs, failed with it.
    """

    def __init__(
        self,
        reason: str,
        *,
        error_type: str,
        simulation_run_ids: tuple[UUID, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type
        self.simulation_run_ids = simulation_run_ids


class _ChangedRunError(LookupError):
    """What the call would be built from now differs from what the row records."""


# The errors that mean a row's call can't be built again, and why, in words a
# failed run's error message can hold. A worker's absence is described by the
# error itself, which names only the .py version and the country.
_REBUILD_REASONS: Final[dict[type[Exception], str | None]] = {
    EconomyWorkerUnavailableError: None,
    HouseholdFunctionUnavailableError: None,
    _ChangedRunError: None,
    StoredRunUnreadableError: "its stored rows no longer describe one calculation",
    UnknownCatalogVersionError: "its catalog version is gone",
    UnknownHouseholdError: "its household is gone",
    UnknownPolicyError: "its policy is gone",
    UnknownDynamicError: "its dynamic is gone",
    MissingPolicyParametersError: "its policy names parameters its catalog lacks",
    OverlappingReformError: "its policy and dynamic set the same parameters",
    DatasetNotInBundleError: "its worker's bundle has no dataset it names",
    HouseholdCalculationUnprocessableError: "its household can no longer be read",
    HouseholdCalculationInternalError: "its household request no longer validates",
    ValidationError: "its stored request no longer validates",
}
_REBUILD_ERRORS: Final = tuple(_REBUILD_REASONS)


def _rebuild_reason(error: Exception) -> str:
    reason = next(
        (
            reason
            for error_class, reason in _REBUILD_REASONS.items()
            if isinstance(error, error_class)
        ),
        None,
    )
    return str(error) if reason is None else reason


def _same_job(rebuilt: NewHouseholdJob, stored: HouseholdJobRecord) -> bool:
    """Return whether the rebuilt job records what the stored job records."""
    return (
        rebuilt.id,
        rebuilt.country,
        rebuilt.household_id,
        rebuilt.policy_id,
        rebuilt.year,
        rebuilt.request_data,
        rebuilt.tax_benefit_model_version_id,
        rebuilt.country_package_version,
        rebuilt.policyengine_version,
    ) == (
        stored.id,
        stored.country,
        stored.household_id,
        stored.policy_id,
        stored.year,
        stored.request_data,
        stored.tax_benefit_model_version_id,
        stored.country_package_version,
        stored.policyengine_version,
    )


def _country(country: str) -> CountryId:
    """Return the stored country as the contract names it."""
    for known in get_args(CountryId):
        if country == known:
            return known
    msg = f"no v2 worker calculates economy runs for country {country}"
    raise StoredRunUnreadableError(msg)


class RunRestartService:
    """Start the calls of the runs and household jobs whose call never started.

    The store reads and writes are synchronous: the check is a batch job of
    its own, so nothing else waits on its event loop.
    """

    def __init__(  # noqa: PLR0913  # each seam tests replace
        self,
        *,
        stores: RestartStores,
        workers: RestartWorkers,
        callers: RestartCallers,
        settings: RestartSettings,
        telemetry: RestartTelemetry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_request_id: Callable[[], str] = lambda: f"run-restart-{uuid.uuid4()}",
    ) -> None:
        """Use these seams; tests replace each of them."""
        self._stores = stores
        self._workers = workers
        self._callers = callers
        self._settings = settings
        self._telemetry = telemetry or NoRestartTelemetry()
        self._clock = clock
        self._call_start_timeout_seconds = call_start_timeout_seconds
        self._new_request_id = new_request_id

    async def restart_unstarted_runs(self) -> RestartSummary:
        """Start once more each row created 3 to 12 minutes ago with no call.

        Raise the store's error when the rows can't be listed.
        """
        now = self._clock()
        runs = self._stores.restarts.list_unstarted(
            created_after=now - RESTART_UNTIL,
            created_before=now - RESTART_AFTER,
            limit=RESTARTS_PER_CHECK,
        )
        outcomes: Counter[RestartOutcome] = Counter()
        for run in runs:
            age_seconds = (now - run.created_at).total_seconds()
            with self._telemetry.restart_scope(run, age_seconds=age_seconds):
                outcome, error_type = await self._restart(run)
                self._telemetry.record_outcome(run, outcome, error_type=error_type)
            outcomes[outcome] += 1
        return RestartSummary(
            listed=len(runs),
            **{outcome.value: count for outcome, count in outcomes.items()},
        )

    async def _restart(self, run: UnstartedRun) -> tuple[RestartOutcome, str | None]:
        """Start the row's call once more; return how it ended and any error type."""
        try:
            rebuilt = self._rebuilt_call(run)
        except _DeferredError as deferred:
            return RestartOutcome.DEFERRED, str(deferred)
        except _CannotRebuildError as cannot:
            message = (
                f"{_NOT_STARTED}, and its call could not be built again: "
                f"{cannot.reason}."
            )
            failed = self._fail(run, message, cannot.simulation_run_ids)
            # An unmarked row whose failure couldn't be written is listed again.
            outcome = {
                True: RestartOutcome.FAILED,
                False: RestartOutcome.LEFT,
                None: RestartOutcome.DEFERRED,
            }[failed]
            return outcome, cannot.error_type
        if rebuilt is None:
            return RestartOutcome.LEFT, None
        start_call, simulation_run_ids = rebuilt
        return await self._start_marked(run, start_call, simulation_run_ids)

    async def _start_marked(
        self,
        run: UnstartedRun,
        start_call: CallStart,
        simulation_run_ids: tuple[UUID, ...],
    ) -> tuple[RestartOutcome, str | None]:
        """Mark the row, then start its call and record the call's ID."""
        try:
            if not self._stores.restarts.mark_restart(run):
                return RestartOutcome.LEFT, None
        except SQLAlchemyError as error:
            return RestartOutcome.DEFERRED, type(error).__name__
        try:
            call_id = await asyncio.wait_for(
                start_call(self._telemetry.capture_context()),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the row is failed unless it has started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            message = (
                f"{_NOT_STARTED}, and Modal did not accept the call that started it "
                f"again ({type(error).__name__})."
            )
            failed = self._fail(run, message, simulation_run_ids)
            # A failure that changed nothing means another call claimed the
            # row; one that couldn't be written leaves the row to the sweep.
            outcome = {
                True: RestartOutcome.FAILED,
                False: RestartOutcome.STARTED_MEANWHILE,
                None: RestartOutcome.LEFT,
            }[failed]
            return outcome, type(error).__name__
        return self._recorded(run, call_id)

    def _recorded(
        self, run: UnstartedRun, call_id: str
    ) -> tuple[RestartOutcome, str | None]:
        """Record the started call's ID; return how the restart ended."""
        try:
            status = self._record(run, call_id)
        except SQLAlchemyError as error:
            return RestartOutcome.CALL_NOT_RECORDED, type(error).__name__
        if status is None:
            return RestartOutcome.LEFT, None
        if status == _PENDING:
            return RestartOutcome.RESTARTED, None
        return RestartOutcome.STARTED_MEANWHILE, None

    def _rebuilt_call(
        self, run: UnstartedRun
    ) -> tuple[CallStart, tuple[UUID, ...]] | None:
        """Return what starts the row's call again and its report's simulation runs.

        Return None when the row is gone. Raise _DeferredError when its rows or
        the manifest can't be read now, and _CannotRebuildError when its call
        can't be built again.
        """
        simulation_run_ids: tuple[UUID, ...] = ()
        try:
            if run.kind is RestartKind.REPORT_RUN:
                stored_report = self._stores.restarts.read_report_run(run.run_id)
                if stored_report is None:
                    return None
                simulation_run_ids = (
                    stored_report.baseline.simulation_run_id,
                    stored_report.reform.simulation_run_id,
                )
                return self._report_call(stored_report), simulation_run_ids
            if run.kind is RestartKind.SIMULATION_RUN:
                stored_simulation = self._stores.restarts.read_simulation_run(
                    run.run_id
                )
                if stored_simulation is None:
                    return None
                return self._simulation_call(stored_simulation), ()
            job = self._stores.households.read(run.run_id)
            if job is None:
                return None
            return self._household_call(job), ()
        except (SQLAlchemyError, VersionManifestUnavailableError) as error:
            raise _DeferredError(type(error).__name__) from error
        except _REBUILD_ERRORS as error:
            raise _CannotRebuildError(
                _rebuild_reason(error),
                error_type=type(error).__name__,
                simulation_run_ids=simulation_run_ids,
            ) from error

    def _economy_worker(self, country: str, versions: RunVersions) -> EconomyWorker:
        """Return the worker of the run's .py version, which must match its versions."""
        worker = self._workers.economy.worker_for_version(
            country, versions.policyengine_version
        )
        if worker.versions != versions:
            msg = (
                f"the v2 worker for .py {versions.policyengine_version} calculates "
                "on other versions than the run records"
            )
            raise _ChangedRunError(msg)
        return worker

    def _economy_scope(
        self,
        worker: EconomyWorker,
        country: str,
        scope: EconomyScope,
        options: dict[str, Any],
    ) -> CalculationScope:
        return CalculationScope(
            country=_country(country),
            region_code=scope.region_code,
            dataset=bundle_dataset(worker, scope.dataset_name),
            year=scope.year,
            options=simulation_options(
                include_cliffs=bool(options.get("include_cliffs", False)),
                spm=options.get("spm"),
            ),
        )

    def _origin(self, created_at: datetime) -> CallOrigin:
        """Return where the restarted call comes from, dated as its row was created.

        The row's creation time places the call's artifacts where its first
        call's would have been.
        """
        return CallOrigin(
            environment=self._settings.environment,
            modal_environment=self._settings.modal_environment,
            request_id=self._new_request_id(),
            created_at=created_at,
        )

    def _report_call(self, stored: StoredReportRun) -> CallStart:
        worker = self._economy_worker(stored.country, stored.versions)
        calculation = report_run_calculation(
            worker,
            self._economy_scope(worker, stored.country, stored.scope, stored.options),
            self._origin(stored.created_at),
            run_ids=ReportRunIds(
                report_run_id=stored.report_run_id,
                baseline_simulation_run_id=stored.baseline.simulation_run_id,
                reform_simulation_run_id=stored.reform.simulation_run_id,
            ),
            baseline_policy=stored.baseline.reform,
            reform_policy=stored.reform.reform,
        )
        caller = self._callers.economy

        def start(observability_context: dict[str, Any] | None) -> Awaitable[str]:
            return caller.start_report_run(
                worker,
                calculation.payload,
                calculation.context,
                observability_context=observability_context,
            )

        return start

    def _simulation_call(self, stored: StoredSimulationRun) -> CallStart:
        worker = self._economy_worker(stored.country, stored.versions)
        calculation = simulation_run_calculation(
            worker,
            # A standalone simulation takes no report options.
            self._economy_scope(worker, stored.country, stored.scope, {}),
            self._origin(stored.created_at),
            simulation_run_id=stored.run.simulation_run_id,
            policy=stored.run.reform,
        )
        caller = self._callers.economy

        def start(observability_context: dict[str, Any] | None) -> Awaitable[str]:
            return caller.start_simulation_run(
                worker,
                calculation.payload,
                calculation.context,
                observability_context=observability_context,
            )

        return start

    def _household_call(self, job: HouseholdJobRecord) -> CallStart:
        request = HouseholdCalculationRequest.model_validate(job.request_data)
        worker = self._workers.households.worker_for_version(
            job.country, job.policyengine_version
        )
        inputs = self._stores.households.read_inputs(
            country=request.country,
            household_id=request.household_id,
            policy_id=request.policy_id,
            policyengine_version=job.policyengine_version,
        )
        prepared = prepare_household_job(request, worker, inputs, job_id=job.id)
        if not _same_job(prepared.job, job):
            msg = (
                "its household, policy or catalog version changed since it was created"
            )
            raise _ChangedRunError(msg)
        caller = self._callers.households

        def start(observability_context: dict[str, Any] | None) -> Awaitable[str]:
            return caller.spawn(
                worker,
                prepared.payload,
                household_job_id=job.id,
                observability_context=observability_context,
            )

        return start

    def _fail(
        self, run: UnstartedRun, message: str, simulation_run_ids: tuple[UUID, ...]
    ) -> bool | None:
        """Fail the row while it is unstarted; return whether it was failed.

        Return None when the failure couldn't be written.
        """
        try:
            if run.kind is RestartKind.REPORT_RUN:
                failed = self._stores.economy.fail_unstarted_report(
                    run.run_id, simulation_run_ids, message
                )
            elif run.kind is RestartKind.SIMULATION_RUN:
                failed = self._stores.economy.fail_unstarted_simulation(
                    run.run_id, message
                )
            else:
                failed = self._stores.households.fail_unstarted(run.run_id, message)
        except SQLAlchemyError:
            return None
        return failed

    def _record(self, run: UnstartedRun, call_id: str) -> str | None:
        """Record the call's ID while the row is unstarted; return its status after."""
        if run.kind is RestartKind.REPORT_RUN:
            status = self._stores.economy.record_report_call(run.run_id, call_id)
        elif run.kind is RestartKind.SIMULATION_RUN:
            status = self._stores.economy.record_simulation_call(run.run_id, call_id)
        else:
            status = self._stores.households.record_call(run.run_id, call_id)
        return None if status is None else str(status)
