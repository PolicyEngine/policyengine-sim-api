"""Create economy reports and simulations, and start the calls that calculate them.

A report submission names a region, a dataset, a year and each side's
stored policy and dynamic. The service:

1. finds the v2 version manifest's default worker's economy functions for
   the country, and its bundle;
2. reads, in one transaction, the country's catalog version for that
   worker's .py version, the region and dataset, each side's policy and
   dynamic values, and, when the output bucket is configured, each side's
   reusable run: a run of the same simulation that succeeded on the same
   versions, used by a succeeded report run with the same options, and
   never refused for reuse;
3. creates, in one transaction, each side's new simulation and pending run
   (none for a reused side), the report with its options, and its first
   report run, pending with no Modal call ID;
4. starts the coordinator's call with ``spawn``, passing the report and its
   run IDs, then records the call's ID while the report run is still
   pending with none (the coordinator may have claimed it already). When
   Modal refuses the call, or does not confirm it in time, it fails the
   report run and its new simulation runs at once instead, under the same
   condition; a run the coordinator claimed meanwhile, because Modal
   accepted the call after all, is left as it is and answered ``pending``.

A standalone simulation is created and started the same way, with one
simulation run calculated by the country's simulation function.

Whatever is missing or cannot be calculated is refused in steps 1 and 2,
before anything is written. The coordinator and the simulation functions
then claim their runs and record their results or failures; the stuck-run
sweep fails a pending run no call was recorded for.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    EconomyRequest,
    NewEconomyReport,
    NewEconomySimulation,
    ReportRunStatus,
    ReportSimulation,
    RunReuse,
    SimulationChoice,
    SimulationStatus,
)
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomySimulationCreated,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    CalculationScope,
    CallOrigin,
    DatasetNotInBundleError,
    bundle_dataset,
    report_run_calculation,
    simulation_options,
    simulation_run_calculation,
)
from policyengine_simulation_entry.economy_submission_errors import (
    REQUEST_BODY,
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    RecordName,
    RefusalSource,
    call_refused,
    read_refusal,
    reference_refusal,
)
from policyengine_simulation_entry.economy_workers import (
    EconomyWorkerUnavailableError,
)
from policyengine_simulation_entry.household_workers import (
    VersionManifestUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from policyengine_simulation_persistence import (
        EconomyCatalogRows,
        EconomyReportInputs,
        EconomyReportState,
        EconomySimulationInputs,
        EconomySimulationState,
        SimulationInputs,
    )

    from policyengine_simulation_entry.budget_window_api_models import (
        BudgetWindowReportRequest,
    )
    from policyengine_simulation_entry.economy_api_models import (
        EconomyRefusalCode,
        EconomyReportRequest,
        EconomySimulationRequest,
        PolicyChoice,
    )
    from policyengine_simulation_entry.economy_calculation_inputs import (
        RunCalculation,
    )
    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
        EconomyWorkerSource,
    )

logger = logging.getLogger(__name__)

# How long a submission waits for Modal to accept the run's call before
# failing the run.
CALL_START_TIMEOUT_SECONDS = 10.0


class EconomyRuns(Protocol):
    """The economy reads and writes (``EconomyRunStore``)."""

    def read_report_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        """Read a report's catalog rows, both sides' values and reusable runs."""
        ...

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        """Read a standalone simulation's catalog rows and values."""
        ...

    def create_report(self, report: NewEconomyReport) -> None:
        """Create the report, its new simulations and its runs."""
        ...

    def create_simulation(self, simulation: NewEconomySimulation) -> None:
        """Create the simulation and its first run."""
        ...

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
        """Fail the report run, and its new simulation runs, while unstarted."""
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

    def read_report(self, report_id: UUID) -> EconomyReportState | None:
        """Return the report with its newest run."""
        ...

    def read_simulation(self, simulation_id: UUID) -> EconomySimulationState | None:
        """Return the simulation with its newest run."""
        ...


@dataclass(frozen=True)
class ServiceSettings:
    """The deployment the service runs in.

    ``environment`` labels the calls' artifacts, ``modal_environment`` holds
    the v2 workers, and ``output_bucket`` is the bucket that keeps
    simulation output files for good. Without it, no report reuses a run.
    """

    environment: str
    modal_environment: str
    output_bucket: str | None = None

    @property
    def reuse_prefix(self) -> str | None:
        """Return ``gs://<bucket>/``, under which reusable output files lie."""
        return None if not self.output_bucket else f"gs://{self.output_bucket}/"


def _database_unavailable() -> EconomySubmissionUnavailableError:
    return EconomySubmissionUnavailableError(
        code="database_unavailable", detail="The simulation database could not be read."
    )


def simulation_choice(policy: PolicyChoice) -> SimulationChoice:
    """Return a request side's stored policy and dynamic."""
    return SimulationChoice(policy_id=policy.policy_id, dynamic_id=policy.dynamic_id)


class EconomySubmissionService[Store: EconomyRuns = EconomyRuns]:
    """Create economy reports and simulations, and start their calls.

    The database and the manifest are read on threads, so the event loop
    never waits on them. ``Store`` is the store's type: a subclass that
    reads more of the database names a protocol with more methods.
    """

    def __init__(  # noqa: PLR0913  # each seam tests replace
        self,
        *,
        store: Store,
        workers: EconomyWorkerSource,
        caller: EconomyRunCaller,
        settings: ServiceSettings,
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_id: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Use these seams; tests replace each of them."""
        self._store = store
        self._workers = workers
        self._caller = caller
        self._settings = settings
        self._call_start_timeout_seconds = call_start_timeout_seconds
        self._new_id = new_id
        self._clock = clock

    async def submit_report(
        self,
        request: EconomyReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated:
        """Create the report and its runs and start its coordinator.

        Raise EconomySubmissionRefusedError (400) or
        EconomySubmissionUnprocessableError (422) before writing anything;
        EconomySubmissionUnavailableError (503) when the manifest or
        database cannot be read, or, naming the report, when Modal doesn't
        confirm the call and the report run was failed; and
        EconomySubmissionOutcomeUnknownError (500) when the create's commit
        failed after its rows were inserted.
        """
        worker = await self._default_worker(request.country)
        options = request.options()
        reuse_prefix = self._settings.reuse_prefix
        reuse = (
            None
            if reuse_prefix is None
            else RunReuse(
                versions=worker.versions,
                inputs=options,
                output_uri_prefix=reuse_prefix,
            )
        )
        economy_request = economy_request_for(request, worker)
        inputs = await self._read_inputs(
            lambda: self._store.read_report_inputs(
                economy_request,
                baseline=simulation_choice(request.baseline),
                reform=simulation_choice(request.reform),
                reuse=reuse,
            ),
            request=economy_request,
        )
        scope = calculation_scope(
            request, worker, inputs.catalog, spm=options.get("spm")
        )
        report = NewEconomyReport(
            id=self._new_id(),
            run_id=self._new_id(),
            country=request.country,
            label=request.label,
            description=request.description,
            tax_benefit_model_id=inputs.catalog.model_id,
            population=inputs.catalog.population(request.year),
            versions=worker.versions,
            inputs=options,
            baseline=self._side(inputs.baseline),
            reform=self._side(inputs.reform),
        )
        calculation = report_run_calculation(
            worker,
            scope,
            self._origin(request_id),
            run_ids=ReportRunIds(
                report_run_id=report.run_id,
                baseline_simulation_run_id=report.baseline.simulation_run_id,
                reform_simulation_run_id=report.reform.simulation_run_id,
            ),
            baseline_policy=inputs.baseline.reform,
            reform_policy=inputs.reform.reform,
        )
        await self._create("report", lambda: self._store.create_report(report))
        status = await self._start_report_run(
            worker,
            calculation,
            report_id=report.id,
            run_id=report.run_id,
            new_simulation_run_ids=report.new_simulation_run_ids,
            observability_context=observability_context,
        )
        return EconomyReportCreated(
            report_id=report.id,
            report_run_id=report.run_id,
            status=status,
            baseline_simulation_id=report.baseline.simulation_id,
            baseline_simulation_run_id=report.baseline.simulation_run_id,
            baseline_reused=report.baseline.reused,
            reform_simulation_id=report.reform.simulation_id,
            reform_simulation_run_id=report.reform.simulation_run_id,
            reform_reused=report.reform.reused,
        )

    async def submit_simulation(
        self,
        request: EconomySimulationRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        """Create the simulation and its first run, and start the run's function.

        Raise the errors ``submit_report`` names, naming the simulation when
        its run was failed.
        """
        worker = await self._default_worker(request.country)
        economy_request = economy_request_for(request, worker)
        choice = SimulationChoice(
            policy_id=request.policy_id, dynamic_id=request.dynamic_id
        )
        inputs = await self._read_inputs(
            lambda: self._store.read_simulation_inputs(economy_request, choice),
            request=economy_request,
        )
        scope = calculation_scope(request, worker, inputs.catalog, spm=None)
        simulation = NewEconomySimulation(
            id=self._new_id(),
            run_id=self._new_id(),
            choice=inputs.simulation.choice,
            population=inputs.catalog.population(request.year),
            versions=worker.versions,
        )
        calculation = simulation_run_calculation(
            worker,
            scope,
            self._origin(request_id),
            simulation_run_id=simulation.run_id,
            policy=inputs.simulation.reform,
        )
        await self._create(
            "simulation", lambda: self._store.create_simulation(simulation)
        )
        status = await self._start_simulation_run(
            worker,
            calculation,
            simulation_id=simulation.id,
            run_id=simulation.run_id,
            observability_context=observability_context,
        )
        return EconomySimulationCreated(
            simulation_id=simulation.id,
            simulation_run_id=simulation.run_id,
            status=status,
        )

    async def read_report(self, report_id: UUID) -> EconomyReportState | None:
        """Return the report with its newest run, or None when there is none.

        Raise EconomySubmissionUnavailableError when it cannot be read.
        """
        return await self._read_state(lambda: self._store.read_report(report_id))

    async def read_simulation(
        self, simulation_id: UUID
    ) -> EconomySimulationState | None:
        """Return the simulation with its newest run, or None when there is none.

        Raise EconomySubmissionUnavailableError when it cannot be read.
        """
        return await self._read_state(
            lambda: self._store.read_simulation(simulation_id)
        )

    async def _read_state[State](self, read: Callable[[], State]) -> State:
        try:
            return await asyncio.to_thread(read)
        except SQLAlchemyError as error:
            logger.warning(
                "economy_state_unreadable",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None

    def _side(self, inputs: SimulationInputs) -> ReportSimulation:
        """Return a side naming its reusable run, or a new simulation and run."""
        if inputs.reusable_run is not None:
            return ReportSimulation(
                simulation_id=inputs.reusable_run.simulation_id,
                simulation_run_id=inputs.reusable_run.simulation_run_id,
                choice=inputs.choice,
                reused=True,
            )
        return ReportSimulation(
            simulation_id=self._new_id(),
            simulation_run_id=self._new_id(),
            choice=inputs.choice,
            reused=False,
        )

    def _origin(self, request_id: str) -> CallOrigin:
        return CallOrigin(
            environment=self._settings.environment,
            modal_environment=self._settings.modal_environment,
            request_id=request_id,
            created_at=self._clock(),
        )

    async def _default_worker(self, country: str) -> EconomyWorker:
        try:
            return await asyncio.to_thread(self._workers.default_worker, country)
        except EconomyWorkerUnavailableError:
            raise EconomySubmissionRefusedError(
                code="no_economy_worker",
                detail=(
                    "The default v2 worker cannot calculate economy runs for "
                    f"{country}."
                ),
            ) from None
        except VersionManifestUnavailableError as error:
            logger.warning(
                "economy_manifest_unavailable",
                extra={"error_type": type(error.__cause__).__name__},
            )
            raise EconomySubmissionUnavailableError(
                code="manifest_unavailable",
                detail="The v2 version manifest could not be read.",
            ) from None

    async def _read_inputs[Inputs](
        self,
        read: Callable[[], Inputs],
        *,
        request: EconomyRequest,
        source: RefusalSource = REQUEST_BODY,
    ) -> Inputs:
        """Run ``read`` on a thread; answer each refusal as ``source``'s error."""
        try:
            return await asyncio.to_thread(read)
        except SQLAlchemyError as error:
            logger.warning(
                "economy_inputs_unreadable",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None
        except Exception as error:
            refusal = read_refusal(error, request, source)
            if refusal is None:
                raise
            raise refusal from None

    async def _create(
        self,
        record: RecordName,
        create: Callable[[], None],
        *,
        rerun: bool = False,
        source: RefusalSource = REQUEST_BODY,
    ) -> None:
        """Create the rows; answer a refusal, an unknown outcome or an outage.

        A commit that failed after the rows were inserted may have written
        them, so it is never answered as unavailable, which would tell the
        caller nothing was created. ``rerun`` says the rows are a stored
        record's new run, and ``source`` what names a refused row.
        """
        try:
            await asyncio.to_thread(create)
        except EconomyRecordReferenceError as error:
            raise reference_refusal(error, source) from None
        except EconomyRecordOutcomeUnknownError as error:
            # The route logs it, with the commit error's type.
            raise EconomySubmissionOutcomeUnknownError(
                record, error_type=type(error.__cause__).__name__, rerun=rerun
            ) from None
        except SQLAlchemyError as error:
            logger.warning(
                "economy_records_not_created",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None

    async def _start_report_run(  # noqa: PLR0913  # the call, then the runs it calculates
        self,
        worker: EconomyWorker,
        calculation: RunCalculation,
        *,
        report_id: UUID,
        run_id: UUID,
        new_simulation_run_ids: Sequence[UUID],
        observability_context: dict[str, Any] | None,
        rerun: bool = False,
    ) -> ReportRunStatus:
        """Start the report run's coordinator and record its call, or fail the run.

        ``new_simulation_run_ids`` are the simulation runs created with the
        report run, which fail with it; ``rerun`` says the report existed
        before its new run.
        """
        try:
            call_id = await asyncio.wait_for(
                self._caller.start_report_run(
                    worker,
                    calculation.payload,
                    calculation.context,
                    observability_context=observability_context,
                ),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the run is failed unless it has started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            failed = await self._fail_unconfirmed(
                "report",
                report_id,
                error,
                lambda message: self._store.fail_unstarted_report(
                    run_id, new_simulation_run_ids, message
                ),
            )
            if failed:
                refused = call_refused(
                    "report", report_id, rerun_id=run_id if rerun else None
                )
                raise refused from None
            return ReportRunStatus.PENDING
        status = await self._record_call(
            "report",
            report_id,
            lambda: self._store.record_report_call(run_id, call_id),
        )
        return ReportRunStatus.PENDING if status is None else ReportRunStatus(status)

    async def _start_simulation_run(  # noqa: PLR0913  # the call, then the run it calculates
        self,
        worker: EconomyWorker,
        calculation: RunCalculation,
        *,
        simulation_id: UUID,
        run_id: UUID,
        observability_context: dict[str, Any] | None,
        rerun: bool = False,
    ) -> SimulationStatus:
        """Start the simulation run's function and record its call, or fail the run.

        ``rerun`` says the simulation existed before its new run.
        """
        try:
            call_id = await asyncio.wait_for(
                self._caller.start_simulation_run(
                    worker,
                    calculation.payload,
                    calculation.context,
                    observability_context=observability_context,
                ),
                timeout=self._call_start_timeout_seconds,
            )
        # As for a report run: any error or the timeout leaves it unconfirmed.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            failed = await self._fail_unconfirmed(
                "simulation",
                simulation_id,
                error,
                lambda message: self._store.fail_unstarted_simulation(run_id, message),
            )
            if failed:
                refused = call_refused(
                    "simulation", simulation_id, rerun_id=run_id if rerun else None
                )
                raise refused from None
            return SimulationStatus.PENDING
        status = await self._record_call(
            "simulation",
            simulation_id,
            lambda: self._store.record_simulation_call(run_id, call_id),
        )
        return SimulationStatus.PENDING if status is None else SimulationStatus(status)

    async def _fail_unconfirmed(
        self,
        record: RecordName,
        record_id: UUID,
        error: BaseException,
        fail: Callable[[str], bool],
    ) -> bool:
        """Fail the run of a call Modal didn't confirm, unless it has started.

        Return whether it was failed. Modal may have accepted the call after
        the wait ended, and its function claimed the run first: the failure
        write then changes nothing. When the write itself fails, the run is
        left pending: the record exists, so it is answered as created, and
        the stuck-run sweep fails the run if no call ever claims it.
        """
        # The log names the error type only; its text could repeat request data.
        logger.error(
            "economy_call_unconfirmed",
            extra={
                "record": record,
                "record_id": str(record_id),
                "error_type": type(error).__name__,
            },
        )
        message = (
            f"modal_call_refused: Modal did not accept the {record}'s call "
            f"({type(error).__name__})."
        )
        try:
            return await asyncio.to_thread(fail, message)
        except SQLAlchemyError as write_error:
            logger.warning(
                "economy_run_not_failed",
                extra={
                    "record": record,
                    "record_id": str(record_id),
                    "error_type": type(write_error).__name__,
                },
            )
            return False

    async def _record_call(
        self,
        record: RecordName,
        record_id: UUID,
        recording: Callable[[], ReportRunStatus | SimulationStatus | None],
    ) -> str | None:
        """Record the call's ID; when that fails, answer the run as created.

        The coordinator or function claims a pending run with no call ID
        itself, so the calculation goes ahead either way.
        """
        try:
            status = await asyncio.to_thread(recording)
        except SQLAlchemyError as error:
            logger.warning(
                "economy_call_not_recorded",
                extra={
                    "record": record,
                    "record_id": str(record_id),
                    "error_type": type(error).__name__,
                },
            )
            return None
        return None if status is None else str(status)


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
