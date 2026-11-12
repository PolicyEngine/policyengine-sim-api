"""Rebuild the Modal call of a run or household job whose call never started.

The restart check (``run_restarts``) starts such a row's call once more. The
call must be the one the row's submission started: ``RestartCallBuilder``
reads back what the submission built it from and builds it again with the
submission's own builders (``report_run_calculation``,
``simulation_run_calculation`` and ``prepare_household_job``), on the worker
of the .py version the row records. A household job whose rebuilt job would
record anything else (another year, catalog version or policy) can't be
rebuilt, so the restart never calculates something its row doesn't record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_persistence import (
        EconomyScope,
        HouseholdCalculationInputs,
        HouseholdJobRecord,
        HouseholdJobStatus,
        NewHouseholdJob,
        RunCall,
        RunVersions,
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

# The deployment each Modal environment holding the v2 workers belongs to, as
# the Simulation Entrypoint's deploy pairs them (APP_ENVIRONMENT). A call's
# context and artifact prefix name the deployment.
DEPLOYMENT_ENVIRONMENTS: Final = MappingProxyType(
    {"staging": "staging", "main": "production"}
)


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

    def read_report_run_simulation_run_ids(
        self, report_run_id: UUID
    ) -> tuple[UUID, ...]:
        """Return the simulation runs the report run names, baseline first."""
        ...

    def read_call(self, run: UnstartedRun) -> RunCall | None:
        """Return the row's status and recorded call, or None when it is gone."""
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


class DeferredRestartError(Exception):
    """The row's rows or the manifest couldn't be read now; the message is its type."""


class CallNotRebuiltError(Exception):
    """The row's call can't be built again; ``reason`` says why, as a clause.

    ``simulation_run_ids`` are a report run's simulation runs, failed with it.
    """

    def __init__(
        self,
        reason: str,
        *,
        error_type: str,
        simulation_run_ids: tuple[UUID, ...] = (),
    ) -> None:
        """Keep why, the error type that said so, and the report's simulation runs."""
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


@dataclass(frozen=True)
class RebuiltCall:
    """What starts a row's call again, and a report run's simulation runs.

    ``start`` takes the observability context the call continues from.
    """

    start: CallStart
    simulation_run_ids: tuple[UUID, ...] = ()


class RestartCallBuilder:
    """Build a row's call again from its stored rows."""

    def __init__(  # noqa: PLR0913  # each seam tests replace
        self,
        *,
        restarts: UnstartedRunSource,
        households: HouseholdJobRestarts,
        workers: RestartWorkers,
        callers: RestartCallers,
        settings: RestartSettings,
        new_request_id: Callable[[], str] = lambda: f"run-restart-{uuid.uuid4()}",
    ) -> None:
        """Read rows from the two stores; start calls with ``callers``."""
        self._restarts = restarts
        self._households = households
        self._workers = workers
        self._callers = callers
        self._settings = settings
        self._new_request_id = new_request_id

    def build(self, run: UnstartedRun) -> RebuiltCall | None:
        """Return what starts the row's call again, or None when the row is gone.

        Raise DeferredRestartError when its rows or the manifest can't be read
        now, and CallNotRebuiltError when its call can't be built again.
        """
        simulation_run_ids: tuple[UUID, ...] = ()
        try:
            if run.kind is RestartKind.REPORT_RUN:
                stored_report = self._restarts.read_report_run(run.run_id)
                if stored_report is None:
                    return None
                simulation_run_ids = (
                    stored_report.baseline.simulation_run_id,
                    stored_report.reform.simulation_run_id,
                )
                return RebuiltCall(self._report_call(stored_report), simulation_run_ids)
            if run.kind is RestartKind.SIMULATION_RUN:
                stored_simulation = self._restarts.read_simulation_run(run.run_id)
                if stored_simulation is None:
                    return None
                return RebuiltCall(self._simulation_call(stored_simulation))
            job = self._households.read(run.run_id)
            if job is None:
                return None
            return RebuiltCall(self._household_call(job))
        except (SQLAlchemyError, VersionManifestUnavailableError) as error:
            raise DeferredRestartError(type(error).__name__) from error
        except _REBUILD_ERRORS as error:
            if run.kind is RestartKind.REPORT_RUN and not simulation_run_ids:
                simulation_run_ids = self._report_simulation_run_ids(run.run_id)
            raise CallNotRebuiltError(
                _rebuild_reason(error),
                error_type=type(error).__name__,
                simulation_run_ids=simulation_run_ids,
            ) from error

    def _report_simulation_run_ids(self, report_run_id: UUID) -> tuple[UUID, ...]:
        """Return the simulation runs a report run its rows can't rebuild names.

        It is failed with them, as its submission fails it with the simulation
        runs created for it; only those still pending with no call are failed.
        When even they can't be read, it is failed alone, and the sweep fails
        them later.
        """
        try:
            return self._restarts.read_report_run_simulation_run_ids(report_run_id)
        except SQLAlchemyError:
            return ()

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
        inputs = self._households.read_inputs(
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
