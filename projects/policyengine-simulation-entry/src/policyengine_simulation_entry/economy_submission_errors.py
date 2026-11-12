"""Why an economy submission is refused, cannot be calculated, or cannot be served.

The submission service raises these, and the routes answer each with its
status: EconomySubmissionRefusedError 400, EconomySubmissionUnprocessableError
422, EconomySubmissionUnavailableError 503 and
EconomySubmissionOutcomeUnknownError 500; a re-run also raises
EconomyRecordNotFoundError 404 and EconomyRerunConflictError 409. ``read_refusal`` and
``reference_refusal`` turn the store's errors into them, writing each detail
from the request rather than from an error's text, since the Public API
passes a refusal's detail on to its callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    OverlappingReformError,
    ReportSideError,
    UnknownCatalogVersionError,
    UnknownDatasetError,
    UnknownDynamicError,
    UnknownPolicyError,
    UnknownRegionError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import (
        EconomyRecordReferenceError,
        EconomyRequest,
        ReportSide,
    )

    from policyengine_simulation_entry.economy_api_models import (
        EconomyConflictCode,
        EconomyRefusalCode,
        EconomyUnavailableCode,
        EconomyUnprocessableCode,
    )

# Each column whose foreign key can refuse a submission's rows: the code the
# refusal answers with and the row it names. A region, dataset or dynamic is
# the caller's own choice; any other row is one the Public API checked first,
# or one the service chose, so its refusal is the service's state.
REFERENCE_REFUSALS: dict[str, tuple[EconomyRefusalCode, str]] = {
    "region_id": ("unknown_region", "region"),
    "dataset_id": ("unknown_dataset", "dataset"),
    "dynamic_id": ("unknown_dynamic", "dynamic"),
    "policy_id": ("unknown_policy", "policy"),
    "tax_benefit_model_version_id": ("unknown_catalog_version", "catalog version"),
    "tax_benefit_model_id": ("unknown_catalog_version", "tax-benefit model"),
}
# What a submission creates, or a re-run calculates again.
RecordName = Literal["report", "simulation"]


@dataclass(frozen=True)
class RefusalSource:
    """Where refused values come from, as a refusal names and locates them.

    ``named_by`` says what names the values in a refusal's detail, and
    ``location`` starts each unprocessable reason's ``loc``.
    """

    named_by: str
    location: str


# A new record's request body, or a stored record a re-run reads.
REQUEST_BODY = RefusalSource(named_by="the request", location="body")
STORED_REPORT = RefusalSource(named_by="the report", location="report")
STORED_SIMULATION = RefusalSource(named_by="the simulation", location="simulation")


class EconomySubmissionRefusedError(Exception):
    """A row the submission names, or a function it needs, is missing (400)."""

    def __init__(self, *, code: EconomyRefusalCode, detail: str) -> None:
        """Keep the reason's code and its description."""
        super().__init__(detail)
        self.code: EconomyRefusalCode = code
        self.detail = detail


@dataclass(frozen=True)
class UnprocessableReason:
    """One reason stored values cannot be calculated, shaped as a FastAPI field error.

    ``loc`` starts with ``body`` and the request field the reason traces to.
    """

    loc: tuple[str | int, ...]
    msg: str
    type: str


class EconomySubmissionUnprocessableError(Exception):
    """The stored policies or dynamics the submission names cannot be calculated."""

    def __init__(
        self,
        *,
        code: EconomyUnprocessableCode,
        reasons: tuple[UnprocessableReason, ...],
    ) -> None:
        """Keep the code and every reason, in order."""
        super().__init__("; ".join(reason.msg for reason in reasons))
        self.code: EconomyUnprocessableCode = code
        self.reasons = reasons


class EconomySubmissionOutcomeUnknownError(Exception):
    """The submission's rows may have been written, but that can't be confirmed.

    The commit failed after the rows were inserted. The route answers 500,
    which the Public API reads as a record that may exist, never a 503,
    which it reads as nothing created. ``rerun`` says the rows are a stored
    record's new run, not a new record.
    """

    def __init__(
        self, record: RecordName, *, error_type: str, rerun: bool = False
    ) -> None:
        """Keep what the submission creates, and the commit error's type."""
        super().__init__(f"the {record} may have been created")
        self.record: RecordName = record
        self.error_type = error_type
        self.rerun = rerun


class EconomyRecordNotFoundError(LookupError):
    """A re-run names no stored report or simulation (404)."""

    def __init__(self, record: RecordName) -> None:
        """Keep which kind of record is missing."""
        super().__init__(f"no such {record}")
        self.record: RecordName = record


class EconomyRerunConflictError(Exception):
    """A stored record is not re-run now (409); nothing was written.

    ``run_id`` names the record's run pending or running, with
    ``run_in_progress``.
    """

    def __init__(
        self, *, code: EconomyConflictCode, detail: str, run_id: UUID | None = None
    ) -> None:
        """Keep the reason's code, its description and any run under way."""
        super().__init__(detail)
        self.code: EconomyConflictCode = code
        self.detail = detail
        self.run_id = run_id


class EconomySubmissionUnavailableError(Exception):
    """A dependency could not be read or refused the call; ask again later.

    ``record_id`` names the report or simulation that was created, or
    re-run, and whose new run was failed, when one was; ``run_id`` names a
    re-run's failed run.
    """

    def __init__(
        self,
        *,
        code: EconomyUnavailableCode,
        detail: str,
        record_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        """Keep the reason's code, its description and any created record."""
        super().__init__(detail)
        self.code: EconomyUnavailableCode = code
        self.detail = detail
        self.record_id = record_id
        self.run_id = run_id


def read_refusal(
    error: Exception,
    request: EconomyRequest,
    source: RefusalSource = REQUEST_BODY,
) -> EconomySubmissionRefusedError | EconomySubmissionUnprocessableError | None:
    """Return the refusal a store read's error means, or None for any other error.

    A missing catalog version, region, dataset, policy or dynamic is refused
    (400); a policy or dynamic setting parameters the catalog version lacks,
    or both setting one, cannot be calculated (422). A report side's refusal
    (ReportSideError) is located on that side's fields, under ``source``:
    the request's body, or the stored record a re-run reads. Every detail is
    written from ``request``, never from the error's text.
    """
    side: ReportSide | None = None
    if isinstance(error, ReportSideError):
        side, error = error.side, error.error
    return _missing_row_refusal(error, request, side, source) or _unprocessable(
        error, request, side, source
    )


def _missing_row_refusal(
    error: Exception,
    request: EconomyRequest,
    side: ReportSide | None,
    source: RefusalSource,
) -> EconomySubmissionRefusedError | None:
    """Return the 400 a missing row means, or None for another error."""
    catalog = (
        f"{request.country}'s catalog version for .py {request.policyengine_version}"
    )
    named_by = source.named_by if side is None else f"{source.named_by}'s {side}"
    # A stored record's region and dataset are named by the record, not asked
    # for; a request's are the caller's own.
    stored_by = "" if source is REQUEST_BODY else f", which {source.named_by} names"
    refusal: tuple[EconomyRefusalCode, str] | None = None
    if isinstance(error, UnknownCatalogVersionError):
        refusal = (
            "unknown_catalog_version",
            f"{request.country} has no catalog version for .py "
            f"{request.policyengine_version}, the default v2 worker's version.",
        )
    elif isinstance(error, UnknownRegionError):
        refusal = (
            "unknown_region",
            f"{catalog} has no region {request.region_code}{stored_by}.",
        )
    elif isinstance(error, UnknownDatasetError):
        dataset = (
            f"default dataset for region {request.region_code}"
            if request.dataset_name is None
            else f"dataset {request.dataset_name}"
        )
        refusal = ("unknown_dataset", f"{catalog} has no {dataset}{stored_by}.")
    elif isinstance(error, UnknownPolicyError):
        refusal = (
            "unknown_policy",
            f"{request.country} has no stored policy {named_by} names.",
        )
    elif isinstance(error, UnknownDynamicError):
        refusal = (
            "unknown_dynamic",
            f"No stored dynamic matches the one {named_by} names.",
        )
    if refusal is None:
        return None
    code, detail = refusal
    return EconomySubmissionRefusedError(code=code, detail=detail)


def _unprocessable(
    error: Exception,
    request: EconomyRequest,
    side: ReportSide | None,
    source: RefusalSource,
) -> EconomySubmissionUnprocessableError | None:
    """Return the 422 refused values mean, or None for another error.

    Each reason is located at the field that names the values: the request's
    own or the stored record's, or the report side's.
    """
    location: tuple[str, ...] = (
        (source.location,) if side is None else (source.location, side)
    )
    if isinstance(error, MissingPolicyParametersError):
        field_name = "policy_id" if error.source == "policy" else "dynamic_id"
        return EconomySubmissionUnprocessableError(
            code="missing_parameters",
            reasons=tuple(
                UnprocessableReason(
                    loc=(*location, field_name, name),
                    msg=(
                        f"the catalog version for .py {request.policyengine_version} "
                        "has no such parameter"
                    ),
                    type="missing_parameter",
                )
                for name in error.parameter_names
            ),
        )
    if isinstance(error, OverlappingReformError):
        return EconomySubmissionUnprocessableError(
            code="overlapping_parameters",
            reasons=tuple(
                UnprocessableReason(
                    loc=(*location, "dynamic_id", name),
                    msg="the policy and the dynamic both set this parameter",
                    type="overlapping_parameter",
                )
                for name in error.parameter_names
            ),
        )
    return None


def reference_refusal(
    error: EconomyRecordReferenceError,
) -> EconomySubmissionRefusedError:
    """Return the refusal of rows the database refused for naming a missing row.

    It names the kind of row, never its ID, which the caller sent.
    """
    code, row = REFERENCE_REFUSALS.get(
        error.column_name or "", ("missing_reference", "row")
    )
    return EconomySubmissionRefusedError(
        code=code, detail=f"a {row} the request names no longer exists"
    )
