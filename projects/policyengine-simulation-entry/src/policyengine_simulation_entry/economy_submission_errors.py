"""Why an economy submission is refused, cannot be calculated, or cannot be served.

The submission service raises these, and the routes answer each with its
status: EconomySubmissionRefusedError 400, EconomySubmissionUnprocessableError
422 and EconomySubmissionUnavailableError 503. ``read_refusal`` and
``reference_refusal`` turn the store's errors into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    OverlappingReformError,
    UnknownCatalogVersionError,
    UnknownDatasetError,
    UnknownDynamicError,
    UnknownPolicyError,
    UnknownRegionError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import EconomyRecordReferenceError

    from policyengine_simulation_entry.economy_api_models import (
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
# What a submission creates.
RecordName = Literal["report", "simulation"]


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


class EconomySubmissionUnavailableError(Exception):
    """A dependency could not be read or refused the call; ask again later.

    ``record_id`` names the report or simulation that was created and whose
    run was failed, when one was.
    """

    def __init__(
        self,
        *,
        code: EconomyUnavailableCode,
        detail: str,
        record_id: UUID | None = None,
    ) -> None:
        """Keep the reason's code, its description and any created record."""
        super().__init__(detail)
        self.code: EconomyUnavailableCode = code
        self.detail = detail
        self.record_id = record_id


# Each store error naming a row the request names that doesn't exist, and
# the code it is refused with.
_MISSING_ROW_CODES: dict[type[LookupError], EconomyRefusalCode] = {
    UnknownRegionError: "unknown_region",
    UnknownDatasetError: "unknown_dataset",
    UnknownDynamicError: "unknown_dynamic",
    UnknownPolicyError: "unknown_policy",
}


def read_refusal(
    error: Exception, *, policyengine_version: str
) -> EconomySubmissionRefusedError | EconomySubmissionUnprocessableError | None:
    """Return the refusal a store read's error means, or None for any other error.

    A missing catalog version, region, dataset, policy or dynamic is refused
    (400); a policy or dynamic setting parameters the catalog version for
    ``policyengine_version`` lacks, or both setting one, cannot be
    calculated (422).
    """
    if isinstance(error, UnknownCatalogVersionError):
        return EconomySubmissionRefusedError(
            code="unknown_catalog_version",
            detail=f"{error}, the default v2 worker's version",
        )
    for error_type, code in _MISSING_ROW_CODES.items():
        if isinstance(error, error_type):
            return EconomySubmissionRefusedError(code=code, detail=str(error))
    if isinstance(error, MissingPolicyParametersError):
        field_name = "policy_id" if error.source == "policy" else "dynamic_id"
        return EconomySubmissionUnprocessableError(
            code="missing_parameters",
            reasons=tuple(
                UnprocessableReason(
                    loc=("body", field_name, name),
                    msg=(
                        f"the catalog version for .py {policyengine_version} has "
                        "no such parameter"
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
                    loc=("body", "dynamic_id", name),
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
