"""Compare two stored v2 economy simulation outputs: one report run's comparison.

The call (``EconomyComparisonCall``) names the report run, and each side's
simulation run, policy and stored output. The worker reads both outputs,
checks each against the digest sim recorded for it, requires both to hold
the same households and the columns this pair's comparison reads (the
labour supply response columns when either policy changes an elasticity),
and aggregates them with the Stage 12 aggregation. It returns the
``SingleYearMacroOutput``; sim validates it and stores it as rows. It writes
nothing, so a call Modal restarts after pre-empting it simply runs again.

A failure reaches the caller as ``ReportExecutionError``, with the redacted
code and summary a returned Stage 12 report carries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid5

from policyengine_simulation_contract.economy_runs import (
    CatalogPopulation,
    EconomyComparisonCall,
    policy_affects_labor_supply,
)
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ArtifactMediaType,
    ArtifactReference,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationRole,
    stage12_output_plan_sha256,
)

from policyengine_simulation_executor.economy_runs.provenance import (
    catalog_bundle_provenance,
)
from policyengine_simulation_executor.stage12_artifacts import (
    canonical_json_bytes,
    deserialize_simulation_frames,
    deserialize_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime.aggregation import (
    RetainedSimulationOutput,
    build_aggregate_report,
    validate_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_runtime.dependencies import (
    artifact_store,
)
from policyengine_simulation_executor.stage12_runtime.failure_recording import (
    FailureSite,
    report_failure,
)
from policyengine_simulation_executor.stage12_runtime.failures import (
    report_execution_error,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    resolve_simulation_output_plan,
    validate_output_frames,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pandas as pd
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.economy_runs import EconomyComparisonSide
    from policyengine_simulation_contract.stage12_execution import (
        BundleProvenance,
        DatasetPopulationInput,
        Stage12OutputPlan,
    )
    from policyengine_simulation_contract.uk_geography import (
        UKLocalAuthorityMetadata,
    )

    from policyengine_simulation_executor.stage12_artifacts import (
        Stage12ArtifactStore,
    )


@dataclass(frozen=True)
class EconomyComparisonDependencies:
    """The comparison's seams; unset seams use the deployed defaults.

    ``artifacts`` reads the stored outputs, ``aggregator`` builds the
    aggregate report, ``output_planner`` plans the columns each side must
    hold, and ``runtime`` receives the failure report.
    """

    artifacts: Stage12ArtifactStore | None = None
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report
    output_planner: Callable[..., Stage12OutputPlan] = resolve_simulation_output_plan
    runtime: ObservabilityRuntime | None = None


@dataclass(frozen=True)
class _StoredOutput:
    frames: Mapping[str, pd.DataFrame]
    size_bytes: int
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None


def _row_identity(frames: Mapping[str, pd.DataFrame]) -> RowIdentity:
    """Fingerprint the rows of a stored output by each entity's identifiers."""
    identifiers = {
        entity: sorted(str(value) for value in frames[entity][f"{entity}_id"].tolist())
        for entity in sorted(frames)
    }
    return RowIdentity(
        identifier_columns=tuple(f"{entity}.{entity}_id" for entity in identifiers),
        row_count=sum(len(values) for values in identifiers.values()),
        identity_sha256=sha256(canonical_json_bytes(identifiers)).hexdigest(),
    )


def _read_output(
    storage: Stage12ArtifactStore, side: EconomyComparisonSide
) -> _StoredOutput:
    """Read one side's stored output; raise ValueError if its bytes changed."""
    payload = storage.read(side.output_uri)
    if sha256(payload).hexdigest() != side.output_sha256:
        msg = "a stored simulation output differs from the digest recorded for it"
        raise ValueError(msg)
    return _StoredOutput(
        frames=deserialize_simulation_frames(payload),
        size_bytes=len(payload),
        uk_local_authority_metadata=deserialize_uk_local_authority_metadata(payload),
    )


def _simulation(
    call: EconomyComparisonCall,
    side: EconomyComparisonSide,
    role: SimulationRole,
    *,
    provenance: BundleProvenance,
    population: DatasetPopulationInput,
) -> SimulationExecutionInput:
    # A run can be compared with itself, and the report's two simulations need
    # identifiers of their own: each is named after the report run and its role.
    return SimulationExecutionInput(
        evaluation_id=call.report_run_id,
        simulation_execution_id=uuid5(call.report_run_id, role.value),
        role=role,
        policy=dict(side.policy),
        population=population,
        year=call.year,
        geography=GeographySelection(country=call.country, region=call.region_code),
        bundle=provenance,
    )


class _Comparison:
    """One comparison call, run step by step."""

    def __init__(
        self, call: EconomyComparisonCall, dependencies: EconomyComparisonDependencies
    ) -> None:
        self.call = call
        self.dependencies = dependencies
        provenance, population = catalog_bundle_provenance(
            country=call.country,
            policyengine_version=call.policyengine_version,
            population=CatalogPopulation(
                region_code=call.region_code, dataset_name=call.dataset_name
            ),
            resolved=load_stage12_bundle(),
        )
        self.report = ReportExecutionInput(
            evaluation_id=call.report_run_id,
            baseline=_simulation(
                call,
                call.baseline,
                SimulationRole.BASELINE,
                provenance=provenance,
                population=population,
            ),
            reform=_simulation(
                call,
                call.reform,
                SimulationRole.REFORM,
                provenance=provenance,
                population=population,
            ),
            requested_aggregates=tuple(ReportAggregate),
        )

    def _retained(
        self,
        simulation: SimulationExecutionInput,
        side: EconomyComparisonSide,
        stored: _StoredOutput,
        plan: Stage12OutputPlan,
    ) -> RetainedSimulationOutput:
        validate_output_frames(stored.frames, plan)
        return RetainedSimulationOutput(
            frames=stored.frames,
            descriptor=SimulationArtifactDescriptor(
                evaluation_id=self.call.report_run_id,
                simulation_execution_id=simulation.simulation_execution_id,
                role=simulation.role,
                artifact=ArtifactReference(
                    uri=side.output_uri,
                    media_type=ArtifactMediaType.PARQUET,
                    content_sha256=side.output_sha256,
                    size_bytes=stored.size_bytes,
                ),
                output_plan_sha256=stage12_output_plan_sha256(plan),
                row_identity=_row_identity(stored.frames),
                bundle=simulation.bundle,
            ),
        )

    def run(self) -> dict[str, Any]:
        call = self.call
        storage = self.dependencies.artifacts or artifact_store()
        baseline_output = _read_output(storage, call.baseline)
        reform_output = _read_output(storage, call.reform)
        # Both sides' outputs must hold the columns this pair's comparison
        # reads: the labour supply response columns when either policy
        # changes an elasticity.
        labor_supply_response = any(
            policy_affects_labor_supply(call.country, side.policy)
            for side in (call.baseline, call.reform)
        )
        plan = self.dependencies.output_planner(
            self.report.baseline, labor_supply_response=labor_supply_response
        )
        baseline = self._retained(
            self.report.baseline, call.baseline, baseline_output, plan
        )
        reform = self._retained(self.report.reform, call.reform, reform_output, plan)
        if baseline.descriptor.row_identity != reform.descriptor.row_identity:
            msg = "the simulation outputs hold different households"
            raise ValueError(msg)
        aggregate = AggregateReportArtifactPayload.model_validate(
            self.dependencies.aggregator(
                report=self.report,
                baseline=baseline,
                reform=reform,
                uk_local_authority_metadata=validate_uk_local_authority_metadata(
                    call.country,
                    baseline_output.uk_local_authority_metadata,
                    reform_output.uk_local_authority_metadata,
                    region_code=call.region_code,
                ),
            )
        )
        if (
            aggregate.evaluation_id != self.report.evaluation_id
            or aggregate.bundle != self.report.baseline.bundle
            or aggregate.requested_aggregates != self.report.requested_aggregates
        ):
            msg = "the aggregate does not describe this comparison"
            raise ValueError(msg)
        return json.loads(
            SingleYearMacroOutput.model_validate(aggregate.result).model_dump_json()
        )


def compare_economy_simulations(
    payload: object, *, dependencies: EconomyComparisonDependencies | None = None
) -> dict[str, Any]:
    """Compare a report run's two stored simulation outputs; return the result.

    Return a ``SingleYearMacroOutput`` in JSON mode. A call for another
    policyengine.py version, or a dataset the installed release lacks,
    raises ValueError before anything is read. Any later failure is reported
    once and raised as ``ReportExecutionError`` for the report run.
    """
    dependencies = dependencies or EconomyComparisonDependencies()
    call = EconomyComparisonCall.model_validate(payload)
    comparison = _Comparison(call, dependencies)
    try:
        return comparison.run()
    except Exception as error:  # noqa: BLE001  # every failure reaches the caller as a typed error with its redacted detail
        detail = report_failure(
            error,
            FailureSite(
                scope="economy_comparison",
                default_code="economy_comparison_failed",
                attributes={
                    "report_run_id": str(call.report_run_id),
                    "baseline_simulation_run_id": str(call.baseline.simulation_run_id),
                    "reform_simulation_run_id": str(call.reform.simulation_run_id),
                },
            ),
            runtime=dependencies.runtime,
        )
        raise report_execution_error(call.report_run_id, detail) from None
