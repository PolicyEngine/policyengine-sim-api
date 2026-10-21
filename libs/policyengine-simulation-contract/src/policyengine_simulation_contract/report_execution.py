"""The typed outcome a report coordinator returns to its caller.

A report coordinator dispatched with ``result_delivery`` set to
``ReportResultDelivery.RETURN_TO_CALLER`` reads and writes no Stage 12 rows.
When the report succeeds it returns a ``ReportExecutionResult``, which carries
the report's aggregate inline, references to the baseline and reform simulation
artifacts, and the software, data and Modal deployment that produced them.
When the report fails it raises ``ReportExecutionError``, which carries the
redacted error code and summary that a Stage 12 report row would record for
the same failure.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    ContractText,
    ErrorCode,
    ErrorSummary,
    ReportAggregate,
    Sha256Digest,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12OutputPlan,
    StrictContractModel,
    stage12_output_plan_sha256,
)

REPORT_EXECUTION_ERROR_MESSAGE = "Report execution failed"


def report_aggregate_sha256(aggregate: SingleYearMacroOutput) -> str:
    """Return the SHA-256 of the aggregate's canonical JSON encoding.

    The encoding is compact, key-sorted UTF-8 JSON of the aggregate's JSON-mode
    dump, so an aggregate has the same digest before and after a JSON round
    trip. Raises ``ValueError`` when the aggregate contains NaN or infinity.
    """
    try:
        payload = json.dumps(
            aggregate.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except ValueError:
        msg = "aggregate contains a non-finite number"
        raise ValueError(msg) from None
    return sha256(payload).hexdigest()


class ReportExecutionProvenance(StrictContractModel):
    """The software, data and Modal deployment that produced one report.

    ``bundle`` names the PolicyEngine.py version, the country model package,
    the dataset and the bundle manifest they came from. The other fields name
    the Modal environment, application and simulation function that ran, the
    digest of the version manifest they were resolved from, and the Modal
    function call ID of the report coordinator.
    """

    bundle: BundleProvenance
    worker_version: ContractText
    modal_environment: ContractText
    modal_application: ContractText
    simulation_callable: ContractText
    version_manifest_sha256: Sha256Digest
    coordinator_invocation_id: ContractText


class ReportExecutionResult(StrictContractModel):
    """A completed report, as its coordinator returns it to the caller.

    ``aggregate`` is the report's single-year macro result: its totals, inline.
    ``aggregate_sha256`` is the digest ``report_aggregate_sha256`` gives for it.
    ``baseline`` and ``reform`` reference the two simulation artifacts the
    aggregate was computed from, and ``output_plan`` declares the columns they
    hold. Both artifacts must belong to this report, carry the baseline and
    reform roles, follow the output plan, cover the same rows and use the
    bundle named in ``provenance``. ``requested_aggregates`` must be the
    aggregates the output plan was built for, in the same order.
    """

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    requested_aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]
    aggregate_schema_version: Literal[1] = 1
    aggregate: SingleYearMacroOutput
    aggregate_sha256: Sha256Digest
    output_plan: Stage12OutputPlan
    baseline: SimulationArtifactDescriptor
    reform: SimulationArtifactDescriptor
    provenance: ReportExecutionProvenance
    completed_at: datetime

    @field_validator("requested_aggregates")
    @classmethod
    def require_unique_aggregates(
        cls,
        value: tuple[ReportAggregate, ...],
    ) -> tuple[ReportAggregate, ...]:
        """Reject repeated aggregate names."""
        if len(value) != len(set(value)):
            msg = "requested aggregates must be unique"
            raise ValueError(msg)
        return value

    @field_validator("completed_at")
    @classmethod
    def require_aware_completion_time(cls, value: datetime) -> datetime:
        """Reject a completion time without a timezone."""
        if value.tzinfo is None:
            msg = "completed_at must include a timezone"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_aggregates_of_the_output_plan(self) -> ReportExecutionResult:
        """Require the requested aggregates the output plan was built for.

        The coordinator builds the output plan from the report's requested
        aggregates, so a result naming others contradicts its own plan.
        """
        if self.requested_aggregates != self.output_plan.requirements.aggregates:
            msg = "requested aggregates must match the output plan's aggregates"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def require_artifacts_of_this_report(self) -> ReportExecutionResult:
        """Require a baseline and a reform artifact that form this report."""
        if self.baseline.role is not SimulationRole.BASELINE:
            msg = "the baseline artifact must use the baseline role"
            raise ValueError(msg)
        if self.reform.role is not SimulationRole.REFORM:
            msg = "the reform artifact must use the reform role"
            raise ValueError(msg)
        for descriptor in (self.baseline, self.reform):
            self._require_artifact_of_this_report(descriptor)
        if self.baseline.simulation_execution_id == self.reform.simulation_execution_id:
            msg = (
                "baseline and reform artifacts must have different simulation "
                "identifiers"
            )
            raise ValueError(msg)
        if self.baseline.row_identity != self.reform.row_identity:
            msg = "baseline and reform artifacts must cover the same rows"
            raise ValueError(msg)
        return self

    def _require_artifact_of_this_report(
        self,
        descriptor: SimulationArtifactDescriptor,
    ) -> None:
        """Require one artifact to belong to this report, bundle and output plan."""
        if descriptor.evaluation_id != self.evaluation_id:
            msg = "simulation artifacts must belong to this report"
            raise ValueError(msg)
        if descriptor.bundle != self.provenance.bundle:
            msg = "simulation artifacts must use the report's bundle"
            raise ValueError(msg)
        if descriptor.output_plan_sha256 != stage12_output_plan_sha256(
            self.output_plan
        ):
            msg = "simulation artifacts must follow the report's output plan"
            raise ValueError(msg)

    @model_validator(mode="after")
    def require_the_aggregate_digest(self) -> ReportExecutionResult:
        """Require ``aggregate_sha256`` to be the digest of ``aggregate``."""
        if report_aggregate_sha256(self.aggregate) != self.aggregate_sha256:
            msg = "aggregate_sha256 does not match the aggregate"
            raise ValueError(msg)
        return self


class ReportExecutionFailure(StrictContractModel):
    """A failed report or simulation: its redacted error code and summary.

    The values are the ones a Stage 12 row records for the same failure, which
    are safe to store and to show to the caller.
    """

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    error_code: ErrorCode
    error_summary: ErrorSummary


class ReportExecutionError(RuntimeError):
    """Raised by a coordinator or simulation whose result goes to its caller.

    The message is fixed; the failure is in ``failure``. The error pickles
    with its failure, so Modal's ``FunctionCall.get`` raises it in the caller
    as this type with the same failure, provided the caller can import this
    module.
    """

    def __init__(self, failure: ReportExecutionFailure) -> None:
        """Carry ``failure`` under the fixed message."""
        self.failure = failure
        super().__init__(REPORT_EXECUTION_ERROR_MESSAGE)

    def __reduce__(
        self,
    ) -> tuple[type[ReportExecutionError], tuple[ReportExecutionFailure]]:
        """Rebuild the error from its failure when it is unpickled."""
        return (type(self), (self.failure,))
