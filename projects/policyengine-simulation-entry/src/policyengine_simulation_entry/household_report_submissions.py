"""Create household reports, and start the household function call that calculates them.

A household report submission names a stored household, the baseline's
stored policy and, optionally, the reform's. The service:

1. finds the v2 version manifest's default worker's household function for
   the country, and its bundle;
2. reads, in one transaction, the country's catalog version for that
   worker's .py version, its model, the stored household and each side's
   policy values, and builds each side's household contract request for
   the household's ``default_year``, with the side's new simulation run ID
   as its calculation ID;
3. creates, in one transaction, each side's household simulation and its
   pending simulation run, the report, which has no options, and its first
   report run, pending with no Modal call ID;
4. starts one household function call with ``spawn``, passing the report
   run's ID and both sides' requests, then records the call's ID while the
   report run is still pending with none (the function may have claimed it
   already). When Modal refuses the call, or does not confirm it in time, it
   fails the report run and its simulation runs at once instead, under the
   same condition; a run the function claimed meanwhile, because Modal
   accepted the call after all, is left as it is and answered ``pending``.

Whatever is missing or cannot be calculated is refused in steps 1 and 2,
before anything is written. The household function then claims the report
run and each simulation run, writes each run's household document, and
finishes the report run. The restart check (``run_restarts``) starts
a report run whose call was never recorded once more, and the stuck-run
sweep fails it when that start is lost too. The errors are the economy routes'
(``economy_submission_errors``), so ``POST /reports`` answers both kinds
alike.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    HouseholdReportSimulation,
    HouseholdReportStore,
    MissingPolicyParametersError,
    NewHouseholdReport,
    ReportRunStatus,
    ReportSideError,
    RunVersions,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
    reference_refusal,
)
from policyengine_simulation_entry.household_contract_fields import (
    job_policy_fields,
    request_field_of,
)
from policyengine_simulation_entry.household_conversion import (
    UnreadableHouseholdError,
    household_situation,
    saved_spm_selection,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    ManifestHouseholdWorkers,
    ModalHouseholdFunctionCaller,
    VersionManifestUnavailableError,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from policyengine_simulation_persistence import (
        HouseholdReportInputs,
        PolicyReform,
        ReportSide,
        StoredHousehold,
    )

    from policyengine_simulation_entry.config import Settings
    from policyengine_simulation_entry.economy_submission_errors import RecordName
    from policyengine_simulation_entry.entry_runtime import (
        SimulationDatabaseConnection,
    )
    from policyengine_simulation_entry.household_report_api_models import (
        HouseholdReportRequest,
    )
    from policyengine_simulation_entry.household_workers import (
        HouseholdReportRunCaller,
        HouseholdWorker,
        HouseholdWorkerSource,
    )

logger = logging.getLogger(__name__)

# How long a submission waits for Modal to accept the household function's
# call before failing the report run.
CALL_START_TIMEOUT_SECONDS = 10.0
# How long one of the service's statements may run, as a household job's.
STATEMENT_TIMEOUT_MILLISECONDS = 5_000


class HouseholdReports(Protocol):
    """The household report reads and writes (``HouseholdReportStore``)."""

    def read_inputs(
        self,
        *,
        country: str,
        household_id: UUID,
        policy_ids: Mapping[ReportSide, UUID | None],
        policyengine_version: str,
    ) -> HouseholdReportInputs:
        """Read the catalog version, household and each side's policy together."""
        ...

    def create_report(self, report: NewHouseholdReport) -> None:
        """Create the report, its simulations and its runs."""
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
        """Fail the report run, and its simulation runs, while unstarted."""
        ...


class HouseholdReportBuildError(Exception):
    """The household function's contract refused a field the service built.

    The message names each refused field, never a value; the route answers
    it as an internal error.
    """


def _database_unavailable() -> EconomySubmissionUnavailableError:
    return EconomySubmissionUnavailableError(
        code="database_unavailable", detail="The simulation database could not be read."
    )


def _not_calculable(
    *reasons: UnprocessableReason,
) -> EconomySubmissionUnprocessableError:
    return EconomySubmissionUnprocessableError(
        code="household_not_calculable", reasons=reasons
    )


def _policy_ids(request: HouseholdReportRequest) -> dict[ReportSide, UUID | None]:
    """Return each side the report calculates and its stored policy, if any."""
    policy_ids: dict[ReportSide, UUID | None] = {"baseline": request.baseline.policy_id}
    if request.reform is not None:
        policy_ids["reform"] = request.reform.policy_id
    return policy_ids


def _read_refusal(
    error: Exception, request: HouseholdReportRequest, policyengine_version: str
) -> EconomySubmissionRefusedError | EconomySubmissionUnprocessableError | None:
    """Return the refusal a store read's error means, or None for another error.

    Every detail is written from the request, never from the error's text.
    """
    if isinstance(error, UnknownCatalogVersionError):
        return EconomySubmissionRefusedError(
            code="unknown_catalog_version",
            detail=(
                f"{request.country} has no catalog version for .py "
                f"{policyengine_version}, the default v2 worker's version."
            ),
        )
    if isinstance(error, UnknownHouseholdError):
        return EconomySubmissionRefusedError(
            code="unknown_household",
            detail=f"{request.country} has no stored household the request names.",
        )
    if not isinstance(error, ReportSideError):
        return None
    side, refused = error.side, error.error
    if isinstance(refused, UnknownPolicyError):
        return EconomySubmissionRefusedError(
            code="unknown_policy",
            detail=(
                f"{request.country} has no stored policy the request's {side} names."
            ),
        )
    if isinstance(refused, MissingPolicyParametersError):
        return EconomySubmissionUnprocessableError(
            code="missing_parameters",
            reasons=tuple(
                UnprocessableReason(
                    loc=("body", side, "policy_id", name),
                    msg=(
                        f"the catalog version for .py {policyengine_version} has "
                        "no such parameter"
                    ),
                    type="missing_parameter",
                )
                for name in refused.parameter_names
            ),
        )
    return None


def _spm_fields(document: Mapping[str, Any]) -> dict[str, object]:
    """Return the contract's SPM fields: the household's saved selection, if any."""
    saved = saved_spm_selection(document)
    if saved is None:
        return {}
    return {"spm": saved, "spm_selection_source": "saved_household"}


def _contract_reasons(
    error: ValidationError, side: ReportSide
) -> tuple[UnprocessableReason, ...]:
    """Name each field the contract refuses by the request field it traces to.

    Raise HouseholdReportBuildError when the contract refuses a field the
    service builds itself, which is no fault of the caller's.
    """
    reasons = []
    built_wrong = []
    for item in error.errors():
        location = tuple(item["loc"])
        request_field = request_field_of(location, spm_from_request=False)
        # A household report sets add_missing_variables itself.
        if request_field is None or request_field == "add_missing_variables":
            built_wrong.append(".".join(str(part) for part in location) or "request")
            continue
        field_location = (
            (side, request_field) if request_field == "policy_id" else (request_field,)
        )
        reasons.append(
            UnprocessableReason(
                loc=("body", *field_location, *location),
                msg=item["msg"],
                type=item["type"],
            )
        )
    if built_wrong:
        msg = (
            "the household function's request has fields the service built "
            f"wrong: {', '.join(built_wrong)}"
        )
        raise HouseholdReportBuildError(msg)
    return tuple(reasons)


@dataclass(frozen=True)
class HouseholdReportSideInputs:
    """One side of a household report run: the simulation run it records.

    ``policy_values`` holds the side's policy values by parameter name and
    period, empty for current law, as ``HouseholdReportStore.read_inputs``
    reads them into ``reforms``.
    """

    simulation_run_id: UUID
    policy_values: PolicyReform


def household_report_calculation(
    *,
    household: StoredHousehold,
    year: int,
    baseline: HouseholdReportSideInputs,
    reform: HouseholdReportSideInputs | None = None,
    worker: HouseholdWorker,
) -> HouseholdReportRunCalculation:
    """Build a household report run's household function payload from its inputs.

    Each side's request calculates the stored ``household``, in its
    country, for ``year`` with the household's saved SPM selection, on
    ``worker``'s bundle, and its calculation ID is the side's simulation
    run; ``reform`` is None for a report of the baseline alone. Every side
    adds the household's missing variables, so a household report holds
    every value. Creating a report passes the household's default year. A
    restart or re-run passes the stored report's year, and the household
    and policy values as they are now read.

    Raise EconomySubmissionUnprocessableError (``household_not_calculable``)
    when the stored household cannot be read, or the household contract
    refuses what would be calculated, the baseline's checked first. Its
    reasons are located at a report request's fields (``household_id``, a
    side's ``policy_id``); a caller rebuilding a stored run reads them as
    its own. Raise HouseholdReportBuildError when the contract refuses a
    field the service builds, two sides recording one simulation run
    included.
    """
    country = household.country
    try:
        situation = household_situation(country, household.document)
    except UnreadableHouseholdError:
        raise _not_calculable(
            UnprocessableReason(
                loc=("body", "household_id"),
                msg="the stored household's people or groups cannot be read",
                type="household_unreadable",
            )
        ) from None
    sides: dict[ReportSide, HouseholdReportSideInputs] = {"baseline": baseline}
    if reform is not None:
        sides["reform"] = reform
    requests = {
        side: _side_request(
            side,
            side_inputs,
            year=year,
            situation=situation,
            household=household,
            worker=worker,
        )
        for side, side_inputs in sides.items()
    }
    try:
        return HouseholdReportRunCalculation(
            baseline=requests["baseline"], reform=requests.get("reform")
        )
    except ValidationError as error:
        msg = "the household report run's calculation was built wrong: " + "; ".join(
            str(item["msg"]) for item in error.errors()
        )
        raise HouseholdReportBuildError(msg) from None


def _side_request(  # noqa: PLR0913  # the side and what it calculates
    side: ReportSide,
    side_inputs: HouseholdReportSideInputs,
    *,
    year: int,
    situation: dict[str, Any],
    household: StoredHousehold,
    worker: HouseholdWorker,
) -> HouseholdExecutionRequest:
    """Build one side's household contract request; raise its refusal."""
    try:
        return HouseholdExecutionRequest.model_validate(
            {
                "calculation_id": side_inputs.simulation_run_id,
                "country": household.country,
                # The report's year, as a situation's period key.
                "year": str(year),
                "household": situation,
                **job_policy_fields(reform=side_inputs.policy_values),
                # A household report holds every value, as the Public API's
                # stored-household route gives it.
                "add_missing_variables": True,
                "bundle": worker.bundle,
                **_spm_fields(household.document),
            }
        )
    except ValidationError as error:
        raise _not_calculable(*_contract_reasons(error, side)) from None


def _report_year(household: StoredHousehold) -> int:
    """Return the year a new report calculates: the household's default year."""
    if household.default_year is None:
        raise _not_calculable(
            UnprocessableReason(
                loc=("body", "household_id"),
                msg="the household has no default year to calculate",
                type="household_without_default_year",
            )
        )
    return household.default_year


class HouseholdReportSubmissionService:
    """Create household reports and start the household function call for each.

    The database and the manifest are read on threads, so the event loop
    never waits on them.
    """

    def __init__(
        self,
        *,
        store: HouseholdReports,
        workers: HouseholdWorkerSource,
        caller: HouseholdReportRunCaller,
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        """Use these seams; tests replace each of them."""
        self._store = store
        self._workers = workers
        self._caller = caller
        self._call_start_timeout_seconds = call_start_timeout_seconds
        self._new_id = new_id

    async def submit_report(
        self,
        request: HouseholdReportRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Create the report and its runs, and start its household function call.

        Raise EconomySubmissionRefusedError (400) or
        EconomySubmissionUnprocessableError (422) before writing anything;
        EconomySubmissionUnavailableError (503) when the manifest or the
        database cannot be read, or, naming the report, when Modal doesn't
        confirm the call and the report run was failed;
        EconomySubmissionOutcomeUnknownError (500) when the create's commit
        failed after its rows were inserted; and HouseholdReportBuildError
        (500) when the service built a request the contract refuses.
        """
        worker = await self._default_worker(request.country)
        inputs = await self._read_inputs(request, worker)
        report, calculation = self._prepare(request, worker, inputs)
        await self._create(report)
        status = await self._start_report_run(
            worker, report, calculation, observability_context=observability_context
        )
        return HouseholdReportCreated(
            report_id=report.id,
            report_run_id=report.run_id,
            status=status,
            year=report.year,
            baseline_simulation_id=report.baseline.simulation_id,
            baseline_simulation_run_id=report.baseline.simulation_run_id,
            reform_simulation_id=(
                None if report.reform is None else report.reform.simulation_id
            ),
            reform_simulation_run_id=(
                None if report.reform is None else report.reform.simulation_run_id
            ),
        )

    async def _default_worker(self, country: str) -> HouseholdWorker:
        try:
            return await asyncio.to_thread(self._workers.default_worker, country)
        except HouseholdFunctionUnavailableError:
            raise EconomySubmissionRefusedError(
                code="no_household_function",
                detail=(
                    f"The default v2 worker has no household function for {country}."
                ),
            ) from None
        except VersionManifestUnavailableError as error:
            logger.warning(
                "household_report_manifest_unavailable",
                extra={"error_type": type(error.__cause__).__name__},
            )
            raise EconomySubmissionUnavailableError(
                code="manifest_unavailable",
                detail="The v2 version manifest could not be read.",
            ) from None

    async def _read_inputs(
        self, request: HouseholdReportRequest, worker: HouseholdWorker
    ) -> HouseholdReportInputs:
        policyengine_version = worker.bundle.policyengine_version
        try:
            return await asyncio.to_thread(
                lambda: self._store.read_inputs(
                    country=request.country,
                    household_id=request.household_id,
                    policy_ids=_policy_ids(request),
                    policyengine_version=policyengine_version,
                )
            )
        except SQLAlchemyError as error:
            logger.warning(
                "household_report_inputs_unreadable",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None
        except Exception as error:
            refusal = _read_refusal(error, request, policyengine_version)
            if refusal is None:
                raise
            raise refusal from None

    def _prepare(
        self,
        request: HouseholdReportRequest,
        worker: HouseholdWorker,
        inputs: HouseholdReportInputs,
    ) -> tuple[NewHouseholdReport, HouseholdReportRunCalculation]:
        """Return the report to create and the calculation its call is given."""
        household = inputs.household
        year = _report_year(household)
        report_id, run_id = self._new_id(), self._new_id()
        sides: dict[ReportSide, HouseholdReportSimulation] = {
            side: HouseholdReportSimulation(
                simulation_id=self._new_id(),
                simulation_run_id=self._new_id(),
                policy_id=policy_id,
            )
            for side, policy_id in _policy_ids(request).items()
        }
        side_inputs = {
            side: HouseholdReportSideInputs(
                simulation_run_id=created.simulation_run_id,
                policy_values=inputs.reforms[side],
            )
            for side, created in sides.items()
        }
        calculation = household_report_calculation(
            household=household,
            year=year,
            baseline=side_inputs["baseline"],
            reform=side_inputs.get("reform"),
            worker=worker,
        )
        report = NewHouseholdReport(
            id=report_id,
            run_id=run_id,
            country=request.country,
            label=request.label,
            description=request.description,
            tax_benefit_model_id=inputs.tax_benefit_model_id,
            tax_benefit_model_version_id=inputs.catalog_version_id,
            household_id=request.household_id,
            year=year,
            versions=RunVersions(
                country_package_version=worker.bundle.country_package_version,
                policyengine_version=worker.bundle.policyengine_version,
            ),
            inputs=request.options(),
            baseline=sides["baseline"],
            reform=sides.get("reform"),
        )
        return report, calculation

    async def _create(self, report: NewHouseholdReport) -> None:
        """Create the rows; answer a refusal, an unknown outcome or an outage.

        A commit that failed after the rows were inserted may have written
        them, so it is never answered as unavailable, which would tell the
        caller nothing was created.
        """
        try:
            await asyncio.to_thread(self._store.create_report, report)
        except EconomyRecordReferenceError as error:
            raise reference_refusal(error) from None
        except EconomyRecordOutcomeUnknownError as error:
            # The route logs it, with the commit error's type.
            record: RecordName = "report"
            raise EconomySubmissionOutcomeUnknownError(
                record, error_type=type(error.__cause__).__name__
            ) from None
        except SQLAlchemyError as error:
            logger.warning(
                "household_report_not_created",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None

    async def _start_report_run(
        self,
        worker: HouseholdWorker,
        report: NewHouseholdReport,
        calculation: HouseholdReportRunCalculation,
        *,
        observability_context: dict[str, Any] | None,
    ) -> ReportRunStatus:
        try:
            call_id = await asyncio.wait_for(
                self._caller.start_report_run(
                    worker,
                    calculation.model_dump(mode="json"),
                    report_run_id=report.run_id,
                    observability_context=observability_context,
                ),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the run is failed unless it has started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            if await self._fail_unconfirmed(report, error):
                raise EconomySubmissionUnavailableError(
                    code="modal_call_refused",
                    detail=(
                        "Modal did not accept the report's call; the report was "
                        "created and its run failed."
                    ),
                    record_id=report.id,
                ) from None
            return ReportRunStatus.PENDING
        try:
            status = await asyncio.to_thread(
                self._store.record_report_call, report.run_id, call_id
            )
        except SQLAlchemyError as error:
            # The household function claims a pending run with no call ID
            # itself, so the calculation goes ahead either way.
            logger.warning(
                "household_report_call_not_recorded",
                extra={
                    "report_id": str(report.id),
                    "error_type": type(error).__name__,
                },
            )
            return ReportRunStatus.PENDING
        return ReportRunStatus.PENDING if status is None else status

    async def _fail_unconfirmed(
        self, report: NewHouseholdReport, error: BaseException
    ) -> bool:
        """Fail the report run of a call Modal didn't confirm, unless it started.

        Return whether it was failed. Modal may have accepted the call after
        the wait ended, and its function claimed the run first: the failure
        write then changes nothing. When the write itself fails, the run is
        left pending: the report exists, so it is answered as created, and
        the stuck-run sweep fails the run if no call ever claims it.
        """
        # The log names the error type only; its text could repeat request data.
        logger.error(
            "household_report_call_unconfirmed",
            extra={"report_id": str(report.id), "error_type": type(error).__name__},
        )
        message = (
            "modal_call_refused: Modal did not accept the report's call "
            f"({type(error).__name__})."
        )
        try:
            return await asyncio.to_thread(
                self._store.fail_unstarted_report,
                report.run_id,
                report.simulation_run_ids,
                message,
            )
        except SQLAlchemyError as write_error:
            logger.warning(
                "household_report_run_not_failed",
                extra={
                    "report_id": str(report.id),
                    "error_type": type(write_error).__name__,
                },
            )
            return False


def household_reports_for(
    settings: Settings, database: SimulationDatabaseConnection | None
) -> HouseholdReportSubmissionService | None:
    """Build the household report service as the household calculation one is.

    It needs the simulation database and the v2 Modal environment; without
    them household reports answer 503. Nothing is read from Modal until the
    first submission.
    """
    if not settings.household_calculations_configured or not isinstance(
        database, SimulationDatabase
    ):
        return None
    return HouseholdReportSubmissionService(
        store=HouseholdReportStore.from_engine(
            database.engine,
            statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
        ),
        workers=ManifestHouseholdWorkers.from_modal(
            manifest_name=settings.v2_manifest_name,
            environment=settings.v2_modal_environment,
        ),
        caller=ModalHouseholdFunctionCaller(settings.v2_modal_environment),
    )
