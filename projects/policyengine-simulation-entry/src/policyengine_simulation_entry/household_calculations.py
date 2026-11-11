"""Submit a household calculation as a household job, and read the job back.

A submission names a stored household, and optionally a stored policy. The
service:

1. finds the v2 version manifest's default worker's household function for
   the country, and its bundle;
2. reads, in one transaction, the country's catalog version for that
   worker's .py version, the stored household and the policy's reform
   values, and builds the household contract's request for the household's
   ``default_year``, with the new job's ID as its calculation ID;
3. creates the job ``pending`` with no Modal call ID;
4. starts the household function's call with ``spawn``, passing the job's
   ID, then records the call's ID while the job is still pending with none
   (the function may have claimed it already). When Modal refuses the call,
   or does not confirm it in time, it fails the job at once instead, under
   the same condition; a job the function claimed meanwhile, because Modal
   accepted the call after all, is answered as it stands.

Whatever is missing or cannot be calculated is refused in steps 1 and 2,
before anything is written. The household function then claims the job and
records its result or failure; the stuck-run sweep fails a pending job no
call was recorded for.
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
from policyengine_simulation_persistence import (
    HouseholdJobReferenceError,
    HouseholdJobStatus,
    MissingPolicyParametersError,
    NewHouseholdJob,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationAccepted,
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
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    VersionManifestUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_simulation_persistence import (
        HouseholdCalculationInputs,
        HouseholdJobRecord,
    )

    from policyengine_simulation_entry.household_api_models import (
        HouseholdCalculationRequest,
        HouseholdRefusalCode,
        HouseholdUnavailableCode,
    )
    from policyengine_simulation_entry.household_workers import (
        HouseholdFunctionCaller,
        HouseholdWorker,
        HouseholdWorkerSource,
    )

logger = logging.getLogger(__name__)

# How long a submission waits for Modal to accept the household function's
# call before failing the job.
CALL_START_TIMEOUT_SECONDS = 10.0
# The job's columns whose foreign keys can refuse it, and the row each names.
_REFERENCED_ROWS = {
    "household_id": "household",
    "policy_id": "policy",
    "tax_benefit_model_version_id": "catalog version",
}


class HouseholdCalculationRefusedError(Exception):
    """A row the submission names, or the household function it needs, is missing."""

    def __init__(self, *, code: HouseholdRefusalCode, detail: str) -> None:
        """Keep the reason's code and its description."""
        super().__init__(detail)
        self.code: HouseholdRefusalCode = code
        self.detail = detail


@dataclass(frozen=True)
class UnprocessableReason:
    """One reason stored data cannot be calculated, as FastAPI writes a field error."""

    loc: tuple[str | int, ...]
    msg: str
    type: str


class HouseholdCalculationUnprocessableError(Exception):
    """The stored household or policy the submission names cannot be calculated."""

    def __init__(self, reasons: tuple[UnprocessableReason, ...]) -> None:
        """Keep every reason, in order."""
        super().__init__("; ".join(reason.msg for reason in reasons))
        self.reasons = reasons


class HouseholdCalculationInternalError(Exception):
    """The service built a household function request its contract refuses.

    The message names each refused field the service built, never a value.
    """


class HouseholdCalculationUnavailableError(Exception):
    """A dependency could not be read or refused the call; ask again later.

    ``household_job_id`` names the job that was failed, when one was created.
    """

    def __init__(
        self,
        *,
        code: HouseholdUnavailableCode,
        detail: str,
        household_job_id: UUID | None = None,
    ) -> None:
        """Keep the reason's code, its description and any failed job."""
        super().__init__(detail)
        self.code: HouseholdUnavailableCode = code
        self.detail = detail
        self.household_job_id = household_job_id


class HouseholdJobs(Protocol):
    """The household job reads and writes (``HouseholdJobStore``)."""

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

    def create_job(self, job: NewHouseholdJob) -> None:
        """Create the job pending with no call."""
        ...

    def record_call(
        self, job_id: UUID, modal_call_id: str
    ) -> HouseholdJobStatus | None:
        """Record the call ID while the job is pending with none."""
        ...

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        """Fail the job while it is pending with no call ID."""
        ...

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the job as it stands."""
        ...


@dataclass(frozen=True)
class PreparedHouseholdJob:
    """A household job ready to create, and the call that calculates it."""

    job: NewHouseholdJob
    payload: dict[str, Any]


def _database_unavailable() -> HouseholdCalculationUnavailableError:
    return HouseholdCalculationUnavailableError(
        code="database_unavailable", detail="The simulation database could not be read."
    )


def _contract_reasons(
    error: ValidationError, request: HouseholdCalculationRequest
) -> tuple[UnprocessableReason, ...]:
    """Name each field the contract refuses by the request field it traces to.

    Raise HouseholdCalculationInternalError when the contract refuses a field
    the service builds itself, which is no fault of the caller's.
    """
    reasons = []
    built_wrong = []
    for item in error.errors():
        location = tuple(item["loc"])
        request_field = request_field_of(
            location, spm_from_request=request.spm_requested
        )
        if request_field is None:
            built_wrong.append(".".join(str(part) for part in location) or "request")
            continue
        reasons.append(
            UnprocessableReason(
                loc=("body", request_field, *location),
                msg=item["msg"],
                type=item["type"],
            )
        )
    if built_wrong:
        msg = (
            "the household function's request has fields the service built "
            f"wrong: {', '.join(built_wrong)}"
        )
        raise HouseholdCalculationInternalError(msg)
    return tuple(reasons)


def _spm_fields(
    request: HouseholdCalculationRequest, document: dict[str, Any]
) -> dict[str, object]:
    """Return the contract's SPM fields: the request's, else the saved selection."""
    if request.spm_requested:
        return {"spm": request.spm, "spm_selection_source": "request"}
    saved = saved_spm_selection(document)
    if saved is None:
        return {}
    return {"spm": saved, "spm_selection_source": "saved_household"}


def prepare_household_job(
    request: HouseholdCalculationRequest,
    worker: HouseholdWorker,
    inputs: HouseholdCalculationInputs,
    *,
    job_id: UUID,
) -> PreparedHouseholdJob:
    """Build the job and the household function's request from the stored rows.

    Raise HouseholdCalculationUnprocessableError for a household without a
    default year or a document that cannot be read, and for anything the
    household contract refuses.
    """
    household = inputs.household
    if household.default_year is None:
        raise HouseholdCalculationUnprocessableError(
            (
                UnprocessableReason(
                    loc=("body", "household_id"),
                    msg="the household has no default year to calculate",
                    type="household_without_default_year",
                ),
            )
        )
    try:
        situation = household_situation(request.country, household.document)
    except UnreadableHouseholdError as error:
        raise HouseholdCalculationUnprocessableError(
            (
                UnprocessableReason(
                    loc=("body", "household_id"),
                    msg=str(error),
                    type="household_unreadable",
                ),
            )
        ) from None
    try:
        contract_request = HouseholdExecutionRequest.model_validate(
            {
                "calculation_id": job_id,
                "country": request.country,
                # The household's default year, as a situation's period key.
                "year": str(household.default_year),
                "household": (
                    situation
                    if request.axes is None
                    else {**situation, "axes": request.axes}
                ),
                **job_policy_fields(reform=inputs.reform),
                "add_missing_variables": request.add_missing_variables,
                "bundle": worker.bundle,
                **_spm_fields(request, household.document),
            }
        )
    except ValidationError as error:
        raise HouseholdCalculationUnprocessableError(
            _contract_reasons(error, request)
        ) from None
    return PreparedHouseholdJob(
        job=NewHouseholdJob(
            id=job_id,
            country=request.country,
            household_id=request.household_id,
            policy_id=request.policy_id,
            year=household.default_year,
            request_data=request.model_dump(mode="json", exclude_unset=True),
            tax_benefit_model_version_id=inputs.catalog_version_id,
            country_package_version=worker.bundle.country_package_version,
            policyengine_version=worker.bundle.policyengine_version,
        ),
        payload=contract_request.model_dump(mode="json"),
    )


class HouseholdCalculationService:
    """Submit household calculations as household jobs, and read the jobs back.

    The database and the manifest are read on threads, so the event loop
    never waits on them.
    """

    def __init__(
        self,
        *,
        store: HouseholdJobs,
        workers: HouseholdWorkerSource,
        caller: HouseholdFunctionCaller,
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_job_id: Callable[[], UUID] = uuid4,
    ) -> None:
        """Use these seams; tests replace each of them."""
        self._store = store
        self._workers = workers
        self._caller = caller
        self._call_start_timeout_seconds = call_start_timeout_seconds
        self._new_job_id = new_job_id

    async def submit(
        self,
        request: HouseholdCalculationRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdCalculationAccepted:
        """Create the household job and start its call; return it as it stands.

        Raise HouseholdCalculationRefusedError (400) or
        HouseholdCalculationUnprocessableError (422) before writing anything,
        HouseholdCalculationInternalError (500) when the service builds a
        request the contract refuses, and HouseholdCalculationUnavailableError
        (503) when the manifest or database cannot be read, or Modal doesn't
        confirm the call and the job was failed.
        """
        worker = await self._default_worker(request.country)
        prepared = await self._prepare(request, worker)
        await self._create(prepared.job)
        job_id = prepared.job.id
        try:
            call_id = await asyncio.wait_for(
                self._caller.spawn(
                    worker,
                    prepared.payload,
                    household_job_id=job_id,
                    observability_context=observability_context,
                ),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the job is failed unless it has started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            return await self._answer_unconfirmed_call(job_id, error)
        status = await self._record_call(job_id, call_id)
        return HouseholdCalculationAccepted(job_id=job_id, status=status)

    async def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the household job as it stands, or None when there is none.

        Raise HouseholdCalculationUnavailableError when it cannot be read.
        """
        try:
            return await asyncio.to_thread(self._store.read, job_id)
        except SQLAlchemyError as error:
            logger.warning(
                "household_job_read_failed",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None

    async def _default_worker(self, country: str) -> HouseholdWorker:
        try:
            return await asyncio.to_thread(self._workers.default_worker, country)
        except HouseholdFunctionUnavailableError as error:
            raise HouseholdCalculationRefusedError(
                code="no_household_function", detail=str(error)
            ) from None
        except VersionManifestUnavailableError as error:
            logger.warning(
                "household_manifest_unavailable",
                extra={"error_type": type(error.__cause__).__name__},
            )
            raise HouseholdCalculationUnavailableError(
                code="manifest_unavailable",
                detail="The v2 version manifest could not be read.",
            ) from None

    async def _prepare(
        self, request: HouseholdCalculationRequest, worker: HouseholdWorker
    ) -> PreparedHouseholdJob:
        policyengine_version = worker.bundle.policyengine_version
        try:
            inputs = await asyncio.to_thread(
                self._store.read_inputs,
                country=request.country,
                household_id=request.household_id,
                policy_id=request.policy_id,
                policyengine_version=policyengine_version,
            )
        except UnknownCatalogVersionError as error:
            raise HouseholdCalculationRefusedError(
                code="unknown_catalog_version",
                detail=f"{error}, the default v2 worker's version",
            ) from None
        except UnknownHouseholdError as error:
            raise HouseholdCalculationRefusedError(
                code="unknown_household", detail=str(error)
            ) from None
        except UnknownPolicyError as error:
            raise HouseholdCalculationRefusedError(
                code="unknown_policy", detail=str(error)
            ) from None
        except MissingPolicyParametersError as error:
            raise HouseholdCalculationUnprocessableError(
                tuple(
                    UnprocessableReason(
                        loc=("body", "policy_id", name),
                        msg=(
                            f"the catalog version for .py {policyengine_version} "
                            "has no such parameter"
                        ),
                        type="missing_parameter",
                    )
                    for name in error.parameter_names
                )
            ) from None
        except SQLAlchemyError as error:
            logger.warning(
                "household_inputs_unreadable",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None
        return prepare_household_job(request, worker, inputs, job_id=self._new_job_id())

    async def _create(self, job: NewHouseholdJob) -> None:
        try:
            await asyncio.to_thread(self._store.create_job, job)
        except HouseholdJobReferenceError as error:
            referenced = {
                "household_id": job.household_id,
                "policy_id": job.policy_id,
                "tax_benefit_model_version_id": job.tax_benefit_model_version_id,
            }
            column = error.column_name
            detail = (
                f"the {_REFERENCED_ROWS[column]} {referenced[column]} no longer exists"
                if column in _REFERENCED_ROWS
                else "a row the household job names no longer exists"
            )
            raise HouseholdCalculationRefusedError(
                code="missing_reference", detail=detail
            ) from None
        except SQLAlchemyError as error:
            logger.warning(
                "household_job_not_created",
                extra={"error_type": type(error).__name__},
            )
            raise _database_unavailable() from None

    async def _answer_unconfirmed_call(
        self, job_id: UUID, error: BaseException
    ) -> HouseholdCalculationAccepted:
        """Fail the job of a call Modal didn't confirm, unless it has started.

        Modal may have accepted the call after the wait ended, and its
        household function claimed the job first: the failure write then
        changes nothing, and the job is answered as it stands. Raise
        HouseholdCalculationUnavailableError ``modal_call_refused`` when the
        job was failed. When the failure write, or reading the job back,
        itself fails, the job is answered pending: it exists, so the caller
        reads it rather than submitting again, and the stuck-run sweep fails
        it if no call ever starts.
        """
        # The log names the error type only; its text could repeat request data.
        logger.error(
            "household_call_unconfirmed",
            extra={"household_job_id": str(job_id), "error_type": type(error).__name__},
        )
        message = (
            "Modal did not accept the household function's call "
            f"({type(error).__name__})."
        )
        try:
            failed = await asyncio.to_thread(
                self._store.fail_unstarted, job_id, message
            )
        except SQLAlchemyError as write_error:
            # The stuck-run sweep fails the job later if it stays pending.
            logger.warning(
                "household_job_not_failed",
                extra={
                    "household_job_id": str(job_id),
                    "error_type": type(write_error).__name__,
                },
            )
            return HouseholdCalculationAccepted(
                job_id=job_id, status=HouseholdJobStatus.PENDING
            )
        if failed:
            raise HouseholdCalculationUnavailableError(
                code="modal_call_refused",
                detail=(
                    "Modal did not accept the household function's call; the job "
                    "was failed."
                ),
                household_job_id=job_id,
            )
        try:
            record = await asyncio.to_thread(self._store.read, job_id)
        except SQLAlchemyError as read_error:
            logger.warning(
                "household_job_not_read",
                extra={
                    "household_job_id": str(job_id),
                    "error_type": type(read_error).__name__,
                },
            )
        else:
            if record is not None:
                return HouseholdCalculationAccepted(job_id=job_id, status=record.status)
        return HouseholdCalculationAccepted(
            job_id=job_id, status=HouseholdJobStatus.PENDING
        )

    async def _record_call(self, job_id: UUID, call_id: str) -> HouseholdJobStatus:
        """Record the call's ID; when that fails, answer the job as created.

        The household function claims a pending job with no call ID itself,
        so the calculation goes ahead either way.
        """
        try:
            status = await asyncio.to_thread(self._store.record_call, job_id, call_id)
        except SQLAlchemyError as error:
            logger.warning(
                "household_call_not_recorded",
                extra={
                    "household_job_id": str(job_id),
                    "error_type": type(error).__name__,
                },
            )
            return HouseholdJobStatus.PENDING
        return HouseholdJobStatus.PENDING if status is None else status
