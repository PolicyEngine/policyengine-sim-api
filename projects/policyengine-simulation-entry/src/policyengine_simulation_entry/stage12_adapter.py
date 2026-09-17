"""Reviewed annual-comparison adapter for Stage 12 evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid5

from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    RequestedSimulationOutput,
    SimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_manifest import V2WorkerVersion


class EvaluationSkipReason(StrEnum):
    UNSUPPORTED_FLOW = "unsupported_flow"
    UNSUPPORTED_SCOPE = "unsupported_scope"
    UNSUPPORTED_BUDGET_WINDOW = "unsupported_budget_window"
    UNSUPPORTED_CLIFF_CALCULATION = "unsupported_cliff_calculation"
    UNSUPPORTED_COUNTRY = "unsupported_country"
    UNSUPPORTED_DATASET = "unsupported_dataset"
    MISSING_BUNDLE_PROVENANCE = "missing_bundle_provenance"
    UNSUPPORTED_OPTIONS = "unsupported_options"
    UNSUPPORTED_REQUEST_SHAPE = "unsupported_request_shape"


@dataclass(frozen=True)
class AdaptedEvaluation:
    report: ReportExecutionInput | None
    skip_reason: EvaluationSkipReason | None

    @property
    def eligible(self) -> bool:
        return self.report is not None


_ALLOWED_FIELDS = frozenset(
    {
        "country",
        "scope",
        "data",
        "time_period",
        "reform",
        "baseline",
        "region",
        "include_cliffs",
        "version",
        "policyengine_version",
        "spm",
        "segmented",
    }
)
_AGGREGATES = tuple(ReportAggregate)


def _skip(reason: EvaluationSkipReason) -> AdaptedEvaluation:
    return AdaptedEvaluation(report=None, skip_reason=reason)


def _country_worker(worker: V2WorkerVersion, country: str):
    return next(
        (item for item in worker.countries if item.country == country),
        None,
    )


def adapt_annual_comparison(
    payload: dict[str, Any],
    *,
    evaluation_id: UUID,
    worker: V2WorkerVersion,
) -> AdaptedEvaluation:
    """Convert only the reviewed production request shape without guessing."""

    if any(
        value is not None
        for key, value in payload.items()
        if key not in _ALLOWED_FIELDS
    ):
        return _skip(EvaluationSkipReason.UNSUPPORTED_OPTIONS)
    if payload.get("scope") != "macro":
        return _skip(EvaluationSkipReason.UNSUPPORTED_SCOPE)
    if payload.get("include_cliffs") is True:
        return _skip(EvaluationSkipReason.UNSUPPORTED_CLIFF_CALCULATION)
    if payload.get("segmented") not in {None, False}:
        return _skip(EvaluationSkipReason.UNSUPPORTED_OPTIONS)
    country = payload.get("country")
    if not isinstance(country, str) or _country_worker(worker, country) is None:
        return _skip(EvaluationSkipReason.UNSUPPORTED_COUNTRY)
    bundle_country = next(
        item for item in worker.bundle.countries if item.country == country
    )
    requested_policyengine_version = payload.get("policyengine_version")
    if (
        requested_policyengine_version is not None
        and requested_policyengine_version != worker.bundle.policyengine_version
    ):
        return _skip(EvaluationSkipReason.MISSING_BUNDLE_PROVENANCE)
    requested_country_package_version = payload.get("version")
    if (
        requested_country_package_version is not None
        and requested_country_package_version != bundle_country.country_package_version
    ):
        return _skip(EvaluationSkipReason.MISSING_BUNDLE_PROVENANCE)
    baseline = payload.get("baseline")
    reform = payload.get("reform")
    if not isinstance(baseline, dict) or not isinstance(reform, dict):
        return _skip(EvaluationSkipReason.UNSUPPORTED_REQUEST_SHAPE)
    year_text = payload.get("time_period")
    if (
        not isinstance(year_text, str)
        or len(year_text) != 4
        or not year_text.isdecimal()
    ):
        return _skip(EvaluationSkipReason.UNSUPPORTED_REQUEST_SHAPE)
    region = payload.get("region")
    if not isinstance(region, str) or not region:
        return _skip(EvaluationSkipReason.UNSUPPORTED_REQUEST_SHAPE)

    selected_dataset = next(
        item
        for item in bundle_country.datasets
        if item.identity == bundle_country.default_dataset
    )
    requested_dataset = payload.get("data")
    accepted_dataset_values = {
        None,
        "default",
        selected_dataset.identity,
        selected_dataset.uri,
        f"{selected_dataset.identity}@{selected_dataset.artifact_revision}",
    }
    if requested_dataset not in accepted_dataset_values:
        return _skip(EvaluationSkipReason.UNSUPPORTED_DATASET)

    dataset = DatasetProvenance(
        identity=selected_dataset.identity,
        uri=selected_dataset.uri,
        artifact_revision=selected_dataset.artifact_revision,
        data_package_name=bundle_country.data_package_name,
        data_package_version=bundle_country.data_package_version,
    )
    provenance = BundleProvenance(
        policyengine_version=worker.bundle.policyengine_version,
        country_package_name=bundle_country.country_package_name,
        country_package_version=bundle_country.country_package_version,
        dataset=dataset,
        bundle_manifest_sha256=worker.bundle_manifest_sha256,
    )
    population = DatasetPopulationInput(
        artifact=DatasetArtifactReference(
            uri=selected_dataset.uri,
            media_type=DatasetArtifactMediaType.HDF5,
            content_sha256=selected_dataset.sha256,
        )
    )
    geography = GeographySelection(
        country=cast(CountryId, country),
        region=region,
    )
    options = {key: payload[key] for key in ("spm",) if payload.get(key) is not None}
    output = RequestedSimulationOutput(variables=("*",))

    def simulation(role: SimulationRole, policy: dict) -> SimulationExecutionInput:
        return SimulationExecutionInput(
            evaluation_id=evaluation_id,
            simulation_execution_id=uuid5(evaluation_id, role.value),
            role=role,
            policy=policy,
            population=population,
            year=int(year_text),
            geography=geography,
            options=options,
            requested_output=output,
            bundle=provenance,
        )

    return AdaptedEvaluation(
        report=ReportExecutionInput(
            evaluation_id=evaluation_id,
            baseline=simulation(SimulationRole.BASELINE, baseline),
            reform=simulation(SimulationRole.REFORM, reform),
            requested_aggregates=_AGGREGATES,
        ),
        skip_reason=None,
    )
