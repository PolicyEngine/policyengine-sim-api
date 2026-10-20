"""Reviewed annual-comparison adapter for Stage 12 comparison runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
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
    SimulationExecutionInput,
    SimulationRole,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        Stage12CountryBundle,
        Stage12Dataset,
    )
    from policyengine_simulation_contract.stage12_manifest import (
        V2CountryWorker,
        V2WorkerVersion,
    )


class ComparisonSkipReason(StrEnum):
    """Why a request gets no Stage 12 comparison run.

    The value is reported as the skip event's ``reason`` attribute and in the
    422 body of the temporary direct-submission route.
    """

    UNSUPPORTED_FLOW = "unsupported_flow"
    UNSUPPORTED_SCOPE = "unsupported_scope"
    UNSUPPORTED_BUDGET_WINDOW = "unsupported_budget_window"
    UNSUPPORTED_COUNTRY = "unsupported_country"
    UNSUPPORTED_DATASET = "unsupported_dataset"
    MISSING_BUNDLE_PROVENANCE = "missing_bundle_provenance"
    UNSUPPORTED_OPTIONS = "unsupported_options"
    UNSUPPORTED_REQUEST_SHAPE = "unsupported_request_shape"


@dataclass(frozen=True)
class AdaptedComparisonRun:
    """Adapter outcome: exactly one of ``report`` and ``skip_reason`` is set."""

    report: ReportExecutionInput | None
    skip_reason: ComparisonSkipReason | None

    @property
    def eligible(self) -> bool:
        """Whether the request was adapted into a report rather than skipped."""
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
        # The public API submits ``_telemetry`` on every society-wide
        # calculation. ``SimulationRequest`` normalizes that internal alias to
        # this field before the Stage 12 adapter sees the payload. It is
        # correlation metadata, not a calculation option, so eligibility must
        # accept and then ignore it.
        "telemetry",
    }
)
_AGGREGATES = tuple(ReportAggregate)
_YEAR_DIGIT_COUNT = 4


def _skip(reason: ComparisonSkipReason) -> AdaptedComparisonRun:
    return AdaptedComparisonRun(report=None, skip_reason=reason)


def _country_worker(worker: V2WorkerVersion, country: str) -> V2CountryWorker | None:
    return next(
        (item for item in worker.countries if item.country == country),
        None,
    )


def _options_skip_reason(payload: dict[str, Any]) -> ComparisonSkipReason | None:
    """Return why the request's fields or options are unsupported, if they are."""
    if any(
        value is not None
        for key, value in payload.items()
        if key not in _ALLOWED_FIELDS
    ):
        return ComparisonSkipReason.UNSUPPORTED_OPTIONS
    if payload.get("scope") != "macro":
        return ComparisonSkipReason.UNSUPPORTED_SCOPE
    include_cliffs = payload.get("include_cliffs")
    if include_cliffs is not None and not isinstance(include_cliffs, bool):
        return ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE
    segmented = payload.get("segmented")
    if segmented is not None and not isinstance(segmented, bool):
        return ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE
    return None


def _bundle_version_skip_reason(
    payload: dict[str, Any],
    worker: V2WorkerVersion,
    bundle_country: Stage12CountryBundle,
) -> ComparisonSkipReason | None:
    """Return a skip reason if a requested package version is not the bundle's."""
    requested_policyengine_version = payload.get("policyengine_version")
    if (
        requested_policyengine_version is not None
        and requested_policyengine_version != worker.bundle.policyengine_version
    ):
        return ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE
    requested_country_package_version = payload.get("version")
    if (
        requested_country_package_version is not None
        and requested_country_package_version != bundle_country.country_package_version
    ):
        return ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE
    return None


def _request_shape_skip_reason(payload: dict[str, Any]) -> ComparisonSkipReason | None:
    """Return a skip reason if the policies, year or region are malformed."""
    if not isinstance(payload.get("baseline"), dict) or not isinstance(
        payload.get("reform"), dict
    ):
        return ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE
    year_text = payload.get("time_period")
    if (
        not isinstance(year_text, str)
        or len(year_text) != _YEAR_DIGIT_COUNT
        or not year_text.isdecimal()
    ):
        return ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE
    region = payload.get("region")
    if not isinstance(region, str) or not region:
        return ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE
    return None


def _accepted_dataset_values(selected_dataset: Stage12Dataset) -> set[str | None]:
    """Return the ``data`` values that name the bundle's default dataset."""
    return {
        None,
        "default",
        selected_dataset.identity,
        selected_dataset.uri,
        f"{selected_dataset.identity}@{selected_dataset.artifact_revision}",
    }


def adapt_annual_comparison(
    payload: dict[str, Any],
    *,
    evaluation_id: UUID,
    worker: V2WorkerVersion,
) -> AdaptedComparisonRun:
    """Convert only the reviewed production request shape without guessing."""
    skip_reason = _options_skip_reason(payload)
    if skip_reason is not None:
        return _skip(skip_reason)
    country = payload.get("country")
    if not isinstance(country, str) or _country_worker(worker, country) is None:
        return _skip(ComparisonSkipReason.UNSUPPORTED_COUNTRY)
    bundle_country = next(
        item for item in worker.bundle.countries if item.country == country
    )
    skip_reason = _bundle_version_skip_reason(payload, worker, bundle_country)
    if skip_reason is None:
        skip_reason = _request_shape_skip_reason(payload)
    if skip_reason is not None:
        return _skip(skip_reason)

    selected_dataset = next(
        item
        for item in bundle_country.datasets
        if item.identity == bundle_country.default_dataset
    )
    if payload.get("data") not in _accepted_dataset_values(selected_dataset):
        return _skip(ComparisonSkipReason.UNSUPPORTED_DATASET)

    return AdaptedComparisonRun(
        report=_report_input(
            payload,
            evaluation_id=evaluation_id,
            worker=worker,
            bundle_country=bundle_country,
            selected_dataset=selected_dataset,
        ),
        skip_reason=None,
    )


def _report_input(
    payload: dict[str, Any],
    *,
    evaluation_id: UUID,
    worker: V2WorkerVersion,
    bundle_country: Stage12CountryBundle,
    selected_dataset: Stage12Dataset,
) -> ReportExecutionInput:
    """Build the baseline and reform inputs for an eligible request."""
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
        country=cast("CountryId", payload["country"]),
        region=payload["region"],
    )
    options = {
        key: payload[key]
        for key in ("spm", "segmented")
        if payload.get(key) is not None
    }
    if payload.get("include_cliffs") is True:
        options["include_cliffs"] = True
    year = int(payload["time_period"])

    def simulation(role: SimulationRole, policy: dict) -> SimulationExecutionInput:
        return SimulationExecutionInput(
            evaluation_id=evaluation_id,
            simulation_execution_id=uuid5(evaluation_id, role.value),
            role=role,
            policy=policy,
            population=population,
            year=year,
            geography=geography,
            options=options,
            bundle=provenance,
        )

    return ReportExecutionInput(
        evaluation_id=evaluation_id,
        baseline=simulation(SimulationRole.BASELINE, payload["baseline"]),
        reform=simulation(SimulationRole.REFORM, payload["reform"]),
        requested_aggregates=_AGGREGATES,
    )
