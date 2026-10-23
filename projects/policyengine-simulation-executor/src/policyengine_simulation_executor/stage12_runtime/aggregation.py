"""Aggregation of completed baseline and reform simulation outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd
from policyengine.core import Simulation
from policyengine_simulation_contract.spm import build_spm_comparison_provenance
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd
    from policyengine.tax_benefit_models.uk.datasets import PolicyEngineUKDataset
    from policyengine.tax_benefit_models.us.datasets import PolicyEngineUSDataset
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata
    from pydantic import JsonValue


def validate_uk_local_authority_metadata(
    country: CountryId,
    baseline: UKLocalAuthorityMetadata | None,
    reform: UKLocalAuthorityMetadata | None,
    *,
    region_code: str | None = None,
) -> UKLocalAuthorityMetadata | None:
    """Require matching UK authority metadata and reject it elsewhere.

    Retained artifacts are read without rerunning dataset detection, so a UK
    pair without metadata passes only with the same evidence detection needs:
    the report's region is national or nation-level and the bundle routes
    both constituency and local-authority regions to another dataset.
    """
    if country == "uk":
        if baseline is None and reform is None:
            from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # UK-only branch, reached only when a UK report has no metadata
                uk_area_codes_required,
            )

            if region_code is None or uk_area_codes_required(region_code):
                msg = "UK simulation artifact metadata is missing"
                raise ValueError(msg)
            return None
        if baseline is None or reform is None:
            msg = "UK simulation artifact metadata is missing on one side"
            raise ValueError(msg)
        if baseline != reform:
            msg = "UK simulation artifact boundary versions do not match"
            raise ValueError(msg)
        return baseline
    if baseline is not None or reform is not None:
        msg = "non-UK simulation artifacts contain UK authority metadata"
        raise ValueError(msg)
    return None


class PrecomputedSimulation(Simulation):
    """A Stage 12 aggregate input whose output dataset is already complete."""

    def ensure(self) -> None:
        """Do not recalculate frames loaded from Stage 12 artifacts."""
        return


def validate_aligned_outputs(
    report: ReportExecutionInput,
    output_plan: Stage12OutputPlan,
    baseline: SimulationArtifactDescriptor,
    reform: SimulationArtifactDescriptor,
) -> None:
    """Check that two simulation artifacts can be aggregated as this report's pair.

    Raises ``ValueError`` unless the roles, evaluation, bundle, output schema,
    output plan and row identities all line up.
    """
    if baseline.role.value != "baseline" or reform.role.value != "reform":
        msg = "simulation artifacts have incorrect report roles"
        raise ValueError(msg)
    if (
        baseline.evaluation_id != report.evaluation_id
        or reform.evaluation_id != report.evaluation_id
    ):
        msg = "simulation artifacts name another comparison run"
        raise ValueError(msg)
    if baseline.bundle != reform.bundle or baseline.bundle != report.baseline.bundle:
        msg = "simulation artifacts have incompatible bundle provenance"
        raise ValueError(msg)
    if baseline.output_schema_version != reform.output_schema_version:
        msg = "simulation artifacts use incompatible output schemas"
        raise ValueError(msg)
    expected_plan_sha256 = stage12_output_plan_sha256(output_plan)
    if (
        baseline.output_plan_sha256 != expected_plan_sha256
        or reform.output_plan_sha256 != expected_plan_sha256
    ):
        msg = "simulation artifacts do not satisfy the report output plan"
        raise ValueError(msg)
    if baseline.row_identity != reform.row_identity:
        msg = "simulation artifacts have incompatible stable row identities"
        raise ValueError(msg)


def _dataset_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    country: CountryId,
    year: int,
) -> PolicyEngineUSDataset | PolicyEngineUKDataset:
    from microdf import (  # noqa: PLC0415  # dataset rebuild: kept with the country dataset classes below, which load lazily
        MicroDataFrame,
    )

    fields = {}
    for entity, frame in frames.items():
        weight_column = f"{entity}_weight"
        fields[entity] = (
            MicroDataFrame(frame, weights=weight_column)
            if weight_column in frame.columns
            else MicroDataFrame(frame)
        )
    if country == "us":
        from policyengine.tax_benefit_models.us.datasets import (
            PolicyEngineUSDataset,
            USYearData,
        )

        return PolicyEngineUSDataset(
            data=USYearData(**fields),
            year=year,
            filepath=None,
            name="stage12-comparison",
            description="Retained Stage 12 output",
        )
    from policyengine.tax_benefit_models.uk.datasets import (
        PolicyEngineUKDataset,
        UKYearData,
    )

    return PolicyEngineUKDataset(
        data=UKYearData(**fields),
        year=year,
        filepath=None,
        name="stage12-comparison",
        description="Retained Stage 12 output",
    )


@dataclass(frozen=True)
class RetainedSimulationOutput:
    """One role's retained output frames and the artifact descriptor they came from."""

    frames: Mapping[str, pd.DataFrame]
    descriptor: SimulationArtifactDescriptor


def build_aggregate_report(
    *,
    report: ReportExecutionInput,
    baseline: RetainedSimulationOutput,
    reform: RetainedSimulationOutput,
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
) -> dict[str, Any]:
    """Build the aggregate report payload from retained baseline and reform frames.

    Runs the standard macro output builder over stand-in simulations that load
    the stored frames, so no model is recomputed. A UK national or nation-level
    report whose bundle routes area regions to another dataset has no area
    codes in its frames (see ``output_planning``), so its constituency and
    local-authority breakdowns are left out.
    """
    from policyengine_simulation_executor.simulation_output_builder import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_output_builder.SimulationOutputBuilder
        SimulationOutputBuilder,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime._country_module
        _country_module,
        _runtime_policy,
    )
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        uk_area_breakdowns_omitted,
    )

    country = report.baseline.geography.country
    country_module = _country_module(country)
    datasets = {
        "baseline": _dataset_from_frames(
            baseline.frames,
            country=country,
            year=report.baseline.year,
        ),
        "reform": _dataset_from_frames(
            reform.frames,
            country=country,
            year=report.reform.year,
        ),
    }

    def stand_in(
        dataset: PolicyEngineUSDataset | PolicyEngineUKDataset,
        policy: dict[str, JsonValue],
    ) -> PrecomputedSimulation:
        simulation = PrecomputedSimulation(
            dataset=dataset,
            tax_benefit_model_version=country_module.model,
            # Interval keys need compiling: the wrapper's dict compiler
            # rejects them.
            policy=_runtime_policy(
                policy, country_module=country_module, year=dataset.year
            ),
        )
        simulation.output_dataset = dataset
        return simulation

    params = {
        "country": country,
        "scope": "macro",
        "data": report.baseline.bundle.dataset.uri,
        "data_version": report.baseline.bundle.dataset.artifact_revision,
        "time_period": str(report.baseline.year),
        "region": report.baseline.geography.region,
        **report.baseline.options,
    }
    output = SimulationOutputBuilder(
        country=country,
        simulation_params=params,
        country_module=country_module,
        dataset=datasets["baseline"],
        baseline=stand_in(datasets["baseline"], report.baseline.policy),
        reform=stand_in(datasets["reform"], report.reform.policy),
        resolved_data_version=report.baseline.bundle.dataset.artifact_revision,
        resolved_region_code=report.baseline.geography.region,
        uk_local_authority_metadata=uk_local_authority_metadata,
        omit_uk_area_breakdowns=uk_area_breakdowns_omitted(
            country, report.baseline.geography.region
        ),
    ).serialize()
    output.update(
        build_spm_result(
            report=report,
            baseline_descriptor=baseline.descriptor,
            reform_descriptor=reform.descriptor,
        )
    )
    return {
        "contract_version": 1,
        "aggregate_schema_version": 1,
        "evaluation_id": str(report.evaluation_id),
        "requested_aggregates": [item.value for item in report.requested_aggregates],
        "bundle": report.baseline.bundle.model_dump(mode="json"),
        "result": output,
    }


def build_spm_result(
    *,
    report: ReportExecutionInput,
    baseline_descriptor: SimulationArtifactDescriptor,
    reform_descriptor: SimulationArtifactDescriptor,
) -> dict[str, Any]:
    """Return the ``spm_provenance`` fields for the report; empty without SPM.

    Raises ``ValueError`` when either artifact lacks an SPM receipt covering the
    report year.
    """
    requested_spm = report.baseline.options.get("spm")
    if requested_spm is None:
        return {}

    baseline_provenance = baseline_descriptor.calculation_provenance
    reform_provenance = reform_descriptor.calculation_provenance
    if baseline_provenance is None or reform_provenance is None:
        msg = "SPM calculation provenance is missing"
        raise ValueError(msg)
    for receipt in (baseline_provenance, reform_provenance):
        if str(report.baseline.year) not in receipt.years:
            msg = "SPM calculation provenance does not cover report year"
            raise ValueError(msg)
    comparison = build_spm_comparison_provenance(
        baseline_receipts=[baseline_provenance],
        reform_receipts=[reform_provenance],
    )
    return {
        "spm_provenance": comparison.model_dump(mode="json", by_alias=True),
    }
