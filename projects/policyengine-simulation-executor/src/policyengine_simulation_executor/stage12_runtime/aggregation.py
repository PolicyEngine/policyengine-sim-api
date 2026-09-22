"""Aggregation of completed baseline and reform simulation outputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    SimulationArtifactDescriptor,
)


def validate_aligned_outputs(
    report: ReportExecutionInput,
    baseline: SimulationArtifactDescriptor,
    reform: SimulationArtifactDescriptor,
) -> None:
    if baseline.role.value != "baseline" or reform.role.value != "reform":
        raise ValueError("simulation artifacts have incorrect report roles")
    if (
        baseline.evaluation_id != report.evaluation_id
        or reform.evaluation_id != report.evaluation_id
    ):
        raise ValueError("simulation artifacts name another comparison run")
    if baseline.bundle != reform.bundle or baseline.bundle != report.baseline.bundle:
        raise ValueError("simulation artifacts have incompatible bundle provenance")
    if baseline.output_schema_version != reform.output_schema_version:
        raise ValueError("simulation artifacts use incompatible output schemas")
    if baseline.row_identity != reform.row_identity:
        raise ValueError("simulation artifacts have incompatible stable row identities")


def _dataset_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    country: CountryId,
    year: int,
):
    from microdf import MicroDataFrame

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


def build_aggregate_report(
    *,
    report: ReportExecutionInput,
    baseline_frames: Mapping[str, pd.DataFrame],
    reform_frames: Mapping[str, pd.DataFrame],
    baseline_descriptor: SimulationArtifactDescriptor,
    reform_descriptor: SimulationArtifactDescriptor,
) -> dict[str, Any]:
    from policyengine_simulation_executor.segmented_national_reduce import (
        PrecomputedSimulation,
    )
    from policyengine_simulation_executor.simulation_output_builder import (
        SimulationOutputBuilder,
    )
    from policyengine_simulation_executor.simulation_runtime import _country_module

    country = report.baseline.geography.country
    country_module = _country_module(country)
    datasets = {
        "baseline": _dataset_from_frames(
            baseline_frames,
            country=country,
            year=report.baseline.year,
        ),
        "reform": _dataset_from_frames(
            reform_frames,
            country=country,
            year=report.reform.year,
        ),
    }

    def stand_in(dataset):
        simulation = PrecomputedSimulation(
            dataset=dataset,
            tax_benefit_model_version=country_module.model,
            policy=None,
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
        baseline=stand_in(datasets["baseline"]),
        reform=stand_in(datasets["reform"]),
        resolved_data_version=report.baseline.bundle.dataset.artifact_revision,
        resolved_region_code=report.baseline.geography.region,
    ).serialize()
    output.update(
        build_spm_result(
            report=report,
            baseline_descriptor=baseline_descriptor,
            reform_descriptor=reform_descriptor,
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
    requested_spm = report.baseline.options.get("spm")
    if requested_spm is None:
        return {}
    from policyengine_simulation_contract.spm import combine_spm_results

    baseline_provenance = baseline_descriptor.calculation_provenance
    reform_provenance = reform_descriptor.calculation_provenance
    if baseline_provenance is None or reform_provenance is None:
        raise ValueError("SPM calculation provenance is missing")
    selection = baseline_provenance.get("spm_config")
    if not isinstance(selection, dict):
        raise TypeError("SPM calculation selection is invalid")
    if selection != reform_provenance.get("spm_config"):
        raise ValueError("SPM calculation selections do not match")
    return combine_spm_results(
        [
            {
                "spm_config": selection,
                "spm_provenance": {
                    "baseline": [baseline_provenance.get("spm_provenance")],
                    "reform": [reform_provenance.get("spm_provenance")],
                },
            }
        ],
        selection,
        expected_year=report.baseline.year,
    )
