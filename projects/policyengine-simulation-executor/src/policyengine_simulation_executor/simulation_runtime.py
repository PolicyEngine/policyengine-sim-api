"""Simulation implementation - pure logic with snapshotted imports.

This module avoids importing policyengine at module level so the worker can
load the requested country module without triggering cross-country imports.
No Modal dependencies here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_contract.spm import SPMInputError, spm_error_detail
from policyengine_simulation_observability import scopes
from policyengine_simulation_observability.stages import ANNUAL_IMPACT_STAGES, Stage
from policyengine_simulation_observability.telemetry import split_internal_payload

from policyengine_simulation_executor import state_datasets
from policyengine_simulation_executor.gcp_credentials import setup_gcp_credentials
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    JobProgressReporter,
)
from policyengine_simulation_executor.region_resolution import (
    RegionResolution,
    require_local_area_breakdown,
    resolve_dataset_reference,
    resolve_region,
)
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.request_policy import (
    DEFAULT_YEAR,
    compile_interval_policy,
    normalise_policy,
    parse_year,
    wrapper_policy,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_output_local_areas import (
    LocalAreaBreakdownBuilder,
)

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine.core import Dataset
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata
    from policyengine_simulation_observability.telemetry import TelemetryEnvelope

    from policyengine_simulation_executor.stage_reporting_simulation import (
        StageReportingSimulation,
    )

__all__ = [
    "DEFAULT_YEAR",
    "DatasetSelection",
    "RegionResolution",
    "UKAreaSimulations",
    "compile_interval_policy",
    "logger",
    "normalise_policy",
    "parse_year",
    "resolve_data_folder",
    "resolve_dataset_reference",
    "resolve_region",
    "run_simulation_impl",
    "setup_gcp_credentials",
    "wrapper_policy",
]

logger = logging.getLogger(__name__)

os.environ.setdefault("POLICYENGINE_SKIP_COUNTRY_IMPORTS", "1")


@dataclass(frozen=True)
class DatasetSelection:
    """One bundle-validated dataset choice shared by loading and artifact reuse."""

    name: str
    uri: str
    is_default: bool


@dataclass(frozen=True)
class UKAreaSimulations:
    """A baseline and reform on the dataset the bundle routes UK area regions to.

    A local-area breakdown run builds its constituency and local-authority
    impacts from this pair, with the request's policies and nation filter,
    and from the authority boundaries detected on that dataset.
    ``dataset_name`` is that dataset's bundle name, which the breakdown
    reports.
    """

    baseline: StageReportingSimulation
    reform: StageReportingSimulation
    local_authority_metadata: UKLocalAuthorityMetadata
    dataset_name: str


def run_simulation_impl(
    params: dict,
    *,
    runtime: ObservabilityRuntime,
    progress: JobProgressReporter = NULL_JOB_PROGRESS,
) -> dict:
    """Execute economic simulation.

    Pure implementation with no Modal dependencies.
    Accepts the gateway simulation payload and returns the legacy macro result dict.
    """
    # Set up GCP credentials if needed. The credentials temp file is
    # cleaned up on exit so we never leave signed JSON material on disk.
    with setup_gcp_credentials(runtime):
        try:
            return _run_simulation_impl_core(params, runtime=runtime, progress=progress)
        except ValueError as exc:
            # Gateway images intentionally do not install country packages.
            # Transport the public error through the shared contract instead.
            detail = spm_error_detail(exc)
            if detail is not None:
                raise SPMInputError(detail.code, detail.message) from None
            raise


def _country_module(country: str) -> ModuleType:
    country = country.lower()
    if country not in {"us", "uk"}:
        msg = f"Unsupported country: {country}"
        raise ValueError(msg)

    return import_module(f"policyengine.tax_benefit_models.{country}")


def resolve_data_folder() -> str:
    """Return the folder datasets and baseline artifacts are read from and saved to.

    One home for the default: the precompute downloads store artifacts into
    this folder for ``ensure_datasets``/``Simulation.ensure()`` to find, so
    any divergence from ``_load_dataset``'s resolution strands them.
    """
    return os.environ.get("POLICYENGINE_DATA_FOLDER", "/tmp/policyengine-data")  # noqa: S108  # fixed data directory inside the Modal container


def _nondefault_data_folder(country: str, name: str, uri: str) -> str:
    """Separate managed datasets even if their source filenames have the same stem."""
    identity = hashlib.sha256(f"{country}:{name}:{uri}".encode()).hexdigest()
    return f"/tmp/policyengine-alternate-data/{identity}"  # noqa: S108  # fixed data directory inside the Modal container


def _resolve_dataset_selection(
    params: dict[str, Any],
    *,
    region_resolution: RegionResolution | None = None,
) -> DatasetSelection:
    """Resolve the bundle's regional or default dataset to a managed name."""
    if "data" in params or "data_version" in params:
        msg = "Dataset overrides are not supported"
        raise ValueError(msg)
    country = params.get("country", "us").lower()
    bundle = get_country_release_bundle(country)
    if region_resolution is not None and region_resolution.dataset_reference:
        dataset_reference = region_resolution.dataset_reference
    else:
        dataset_reference = bundle.default_dataset

    # Region metadata can contain a URI rather than a managed dataset name.
    for name, uri in bundle.dataset_uris.items():
        if dataset_reference in (name, uri):
            return DatasetSelection(name, uri, name == bundle.default_dataset)
    for name, uri in bundle.dataset_uris.items():
        if dataset_reference == runtime_dataset_uri(
            uri,
            default_revision=bundle.data_package_version,
            artifact_revision=bundle.data_artifact_revision,
            validate_hf=False,
        ):
            return DatasetSelection(name, uri, name == bundle.default_dataset)
    msg = (
        f"Unsupported dataset {dataset_reference!r} for country {country!r}; "
        "choose a name in the certified release manifest"
    )
    raise ValueError(msg)


def _selected_dataset_revision(country: str, selection: DatasetSelection) -> str | None:
    """Return the release revision of the dataset a US state or district run loads.

    Such runs load a non-default dataset (the ACS-local dataset since .py 6.7),
    so their ``data_version`` names that dataset's revision rather than the
    default dataset's release. None leaves every other run reporting the
    release's data version, as before.
    """
    if country != "us" or selection.is_default:
        return None
    return _manifest_dataset_revision(country, selection.name)


def _manifest_dataset_revision(country: str, name: str) -> str | None:
    """Return the revision the release manifest records for a named dataset.

    None when the manifest has no such dataset or records no revision for it.
    """
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    reference = getattr(get_release_manifest(country), "datasets", {}).get(name)
    revision = getattr(reference, "revision", None)
    return revision if isinstance(revision, str) and revision else None


def _load_dataset(
    params: dict[str, Any],
    *,
    selection: DatasetSelection,
    country_module: ModuleType | None = None,
    data_folder: str | None = None,
) -> Dataset:
    """Load the selected dataset's year file, preparing it if the folder lacks it.

    ``data_folder`` replaces the folder the selection would otherwise use: the
    image's data folder for the default dataset, or a temporary folder for any
    other dataset.
    """
    country = params.get("country", "us").lower()
    year = parse_year(params)
    country_module = country_module or _country_module(country)
    if data_folder is None:
        data_folder = (
            resolve_data_folder()
            if selection.is_default
            else _nondefault_data_folder(country, selection.name, selection.uri)
        )

    start = time.monotonic()
    load_options = {"years": [year], "data_folder": data_folder}
    if not selection.is_default:
        load_options["datasets"] = [selection.name]
    datasets = country_module.ensure_datasets(**load_options)
    logger.info(
        "Loaded dataset %s year %s from %s in %.1fs",
        selection.name,
        year,
        data_folder,
        time.monotonic() - start,
    )
    return next(iter(datasets.values()))


def _build_simulation(
    params: dict[str, Any],
    *,
    dataset: Dataset,
    dataset_selection: DatasetSelection,
    policy: dict[str, Any] | None,
    region: RegionResolution,
) -> StageReportingSimulation:
    """Build a simulation that reports when its calculation starts.

    A run with a deterministic baseline id gets an ArtifactBaselineSimulation,
    which is a StageReportingSimulation that can load a precomputed baseline.
    """
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
        BaselineIdentityRequest,
        deterministic_baseline_id,
    )
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )
    from policyengine_simulation_executor.stage_reporting_simulation import (  # noqa: PLC0415  # resolved per call so tests can patch stage_reporting_simulation
        StageReportingSimulation,
    )

    selection = normalize_runtime_spm(params)
    params = {**params, **({"spm": selection} if selection is not None else {})}
    spm_kwargs: dict[str, Any] = {"spm": selection} if selection is not None else {}
    country = params.get("country", "us")
    country_module = _country_module(country)
    runtime_policy = compile_interval_policy(
        policy, country_module=country_module, year=parse_year(params)
    )
    scoping_strategy = region.scoping_strategy
    simulation_id = deterministic_baseline_id(
        BaselineIdentityRequest(
            params,
            country=country,
            dataset_is_default=dataset_selection.is_default,
            policy=runtime_policy,
            region_code=region.code,
            scoping_strategy=scoping_strategy,
            year=parse_year(params),
        )
    )
    if simulation_id is not None:
        # Deterministic id: ensure() loads a precomputed baseline artifact
        # when one is baked beside the dataset, and the subclass validates
        # the load (falling back to run()) — see baseline_artifacts.
        return ArtifactBaselineSimulation(
            **spm_kwargs,
            id=simulation_id,
            dataset=dataset,
            tax_benefit_model_version=country_module.model,
            policy=runtime_policy,
            scoping_strategy=scoping_strategy,
        )
    return StageReportingSimulation(
        **spm_kwargs,
        dataset=dataset,
        tax_benefit_model_version=country_module.model,
        policy=runtime_policy,
        scoping_strategy=scoping_strategy,
    )


def _report_calculation_start(
    simulation: StageReportingSimulation, progress: JobProgressReporter, stage: JobStage
) -> None:
    simulation.on_first_ensure(lambda: progress.enter(stage))


def _uk_area_simulations(
    params: dict[str, Any],
    *,
    runtime: ObservabilityRuntime,
    country_module: ModuleType,
    region: RegionResolution,
    policies: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> UKAreaSimulations:
    """Build a local-area breakdown's baseline and reform on the routed dataset.

    The bundle routes constituency and local-authority regions to a
    local-area dataset. The pair applies the request's baseline and reform
    policies and its UK-wide or nation filter to that dataset, whose area and
    disclosure columns are checked as it loads.
    """
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        detect_uk_area_dataset_metadata,
        routed_uk_area_dataset_name,
    )

    area_role = {"dataset_role": "uk_area"}
    with scopes.span(
        runtime,
        ANNUAL_IMPACT_STAGES.name(Stage.DATASET_RESOLUTION),
        attributes=area_role,
    ):
        selection = _resolve_dataset_selection(
            params,
            region_resolution=RegionResolution(
                code=region.code, dataset_reference=routed_uk_area_dataset_name()
            ),
        )
    with scopes.span(
        runtime, ANNUAL_IMPACT_STAGES.name(Stage.DATASET_LOAD), attributes=area_role
    ):
        dataset = _load_dataset(
            params, selection=selection, country_module=country_module
        )
    local_authority_metadata = detect_uk_area_dataset_metadata(
        dataset, dataset_name=selection.name
    )
    simulations: list[StageReportingSimulation] = []
    for kind, policy in zip(
        ("uk_area_baseline", "uk_area_reform"), policies, strict=True
    ):
        with scopes.span(
            runtime,
            ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_BUILD),
            attributes={"simulation_kind": kind},
        ):
            simulations.append(
                _build_simulation(
                    params,
                    dataset=dataset,
                    dataset_selection=selection,
                    policy=policy,
                    region=region,
                )
            )
    baseline, reform = simulations
    return UKAreaSimulations(
        baseline=baseline,
        reform=reform,
        local_authority_metadata=local_authority_metadata,
        dataset_name=selection.name,
    )


def _run_local_area_breakdown_core(
    simulation_params: dict[str, Any],
    *,
    runtime: ObservabilityRuntime,
    progress: JobProgressReporter,
    country: str,
) -> dict:
    """Run a UK local-area breakdown on the local-area dataset alone.

    The request's UK-wide or nation region filters one baseline and reform
    on the dataset the bundle routes area regions to; the national file is
    never loaded. The result holds the constituency and local-authority
    impacts only (``LocalAreaBreakdownOutput``).
    """
    require_local_area_breakdown(simulation_params, country=country)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.COUNTRY_MODULE_LOAD)):
        country_module = _country_module(country)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.REGION_RESOLUTION)):
        region_resolution = resolve_region(
            country_module=country_module,
            country=country,
            params=simulation_params,
        )
    runtime.set_context(region=region_resolution.code)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.POLICY_NORMALIZATION)):
        policies = (
            normalise_policy(simulation_params.get("baseline")),
            normalise_policy(simulation_params.get("reform")),
        )

    logger.info("Initialising the local-area baseline and reform simulations")
    area = _uk_area_simulations(
        simulation_params,
        runtime=runtime,
        country_module=country_module,
        region=region_resolution,
        policies=policies,
    )
    _report_calculation_start(area.baseline, progress, JobStage.BASELINE)
    _report_calculation_start(area.reform, progress, JobStage.REFORM)

    logger.info("Calculating the local-area breakdown")
    builder = LocalAreaBreakdownBuilder(
        baseline=area.baseline,
        reform=area.reform,
        local_authority_metadata=area.local_authority_metadata,
        dataset_name=area.dataset_name,
        data_version=_manifest_dataset_revision(country, area.dataset_name),
        year=parse_year(simulation_params),
        country_module=country_module,
        runtime=runtime,
        progress=progress,
    )
    output = builder.serialize()
    logger.info("Local-area breakdown complete")
    return output


def _run_simulation_impl_core(
    params: dict,
    *,
    runtime: ObservabilityRuntime,
    progress: JobProgressReporter = NULL_JOB_PROGRESS,
) -> dict:
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.REQUEST_PARSE)):
        simulation_params, telemetry, metadata = split_internal_payload(params)
    metadata = metadata or {}
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
        simulation_spm_result,
    )

    selection = normalize_runtime_spm(simulation_params)
    if selection is not None:
        simulation_params["spm"] = selection

    _log_simulation_start(simulation_params, telemetry, metadata)

    country = simulation_params.get("country", "us").lower()
    _set_runtime_attributes(
        runtime=runtime,
        simulation_params=simulation_params,
        telemetry=telemetry,
        metadata=metadata,
        country=country,
    )
    if simulation_params.get("breakdown") is not None:
        return _run_local_area_breakdown_core(
            simulation_params, runtime=runtime, progress=progress, country=country
        )

    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.COUNTRY_MODULE_LOAD)):
        country_module = _country_module(country)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.REGION_RESOLUTION)):
        region_resolution = resolve_region(
            country_module=country_module,
            country=country,
            params=simulation_params,
        )
    runtime.set_context(region=region_resolution.code)
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.DATASET_RESOLUTION)):
        dataset_selection = _resolve_dataset_selection(
            simulation_params, region_resolution=region_resolution
        )
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.DATASET_LOAD)):
        baked_folder = state_datasets.baked_state_data_folder(
            country=country,
            selection=dataset_selection,
            state_code=region_resolution.state_code,
            year=parse_year(simulation_params),
            data_folder=resolve_data_folder(),
        )
        if not dataset_selection.is_default:
            runtime.set_context(
                dataset_source=(
                    state_datasets.BAKED_STATE_DATASET_SOURCE
                    if baked_folder
                    else state_datasets.ON_DEMAND_DATASET_SOURCE
                )
            )
        dataset = _load_dataset(
            simulation_params,
            selection=dataset_selection,
            country_module=country_module,
            data_folder=baked_folder,
        )
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        detect_uk_run_dataset_metadata,
        uk_area_breakdowns_omitted,
    )

    uk_local_authority_metadata = detect_uk_run_dataset_metadata(
        country,
        dataset,
        region_code=region_resolution.code,
        dataset_name=dataset_selection.name,
    )
    with scopes.span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.POLICY_NORMALIZATION)):
        baseline_policy = normalise_policy(simulation_params.get("baseline"))
        reform_policy = normalise_policy(simulation_params.get("reform"))

    logger.info("Initialising baseline and reform simulations")
    with scopes.span(
        runtime,
        ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_BUILD),
        attributes={"simulation_kind": "baseline"},
    ):
        baseline = _build_simulation(
            simulation_params,
            dataset=dataset,
            dataset_selection=dataset_selection,
            policy=baseline_policy,
            region=region_resolution,
        )
    with scopes.span(
        runtime,
        ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_BUILD),
        attributes={"simulation_kind": "reform"},
    ):
        reform = _build_simulation(
            simulation_params,
            dataset=dataset,
            dataset_selection=dataset_selection,
            policy=reform_policy,
            region=region_resolution,
        )

    _report_calculation_start(baseline, progress, JobStage.BASELINE)
    _report_calculation_start(reform, progress, JobStage.REFORM)

    logger.info("Calculating economic impact")
    builder = SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=dataset,
        baseline=baseline,
        reform=reform,
        resolved_data_version=_selected_dataset_revision(country, dataset_selection),
        resolved_region_code=region_resolution.code,
        runtime=runtime,
        uk_local_authority_metadata=uk_local_authority_metadata,
        # A routed UK-wide or nation run computes no area results; a
        # breakdown=local_areas run on the local-area dataset does.
        omit_uk_area_breakdowns=uk_area_breakdowns_omitted(
            country, region_resolution.code
        ),
        progress=progress,
    )
    output = builder.serialize()
    output.update(
        simulation_spm_result(
            baseline, reform, selection, expected_year=parse_year(simulation_params)
        )
    )
    # ensure() has run inside the builder by now, so the artifact outcome
    # (hit / incomplete / miss) is known for deterministic-id baselines.
    artifact_outcome = getattr(baseline, "artifact_outcome", None)
    if artifact_outcome is not None:
        runtime.set_context(baseline_artifact=artifact_outcome)
        logger.info("Baseline artifact outcome: %s", artifact_outcome)
    logger.info("Comparison complete")
    if params.get("_emit_microdata"):
        from policyengine_simulation_executor.simulation_microdata import (  # noqa: PLC0415  # the microdata export loads only when a run asks for it
            extract_output_microdata,
        )

        output["_microdata"] = extract_output_microdata(baseline, reform)
    return output


def _log_simulation_start(
    simulation_params: dict[str, Any],
    telemetry: TelemetryEnvelope | None,
    metadata: dict[str, Any],
) -> None:
    logger.info(
        "Starting simulation for country=%s submission_claim_id=%s",
        simulation_params.get("country", "unknown"),
        getattr(telemetry, "submission_claim_id", None),
    )
    if metadata:
        logger.info("Received simulation metadata keys: %s", sorted(metadata))


def _set_runtime_attributes(
    *,
    runtime: ObservabilityRuntime,
    simulation_params: dict[str, Any],
    telemetry: TelemetryEnvelope | None,
    metadata: dict[str, Any],
    country: str,
) -> None:
    runtime.set_context(
        country=country,
        scope=simulation_params.get("scope"),
        simulation_year=parse_year(simulation_params),
        submission_claim_id=getattr(telemetry, "submission_claim_id", None),
        geography_code=getattr(telemetry, "geography_code", None),
        geography_type=getattr(telemetry, "geography_type", None),
        resolved_version=metadata.get("resolved_version"),
        resolved_app_name=metadata.get("resolved_app_name"),
    )
    bundle = metadata.get("policyengine_bundle")
    if isinstance(bundle, dict):
        runtime.set_context(
            policyengine_version=bundle.get("policyengine_version"),
            model_version=bundle.get("model_version"),
            data_version=bundle.get("data_version"),
        )
