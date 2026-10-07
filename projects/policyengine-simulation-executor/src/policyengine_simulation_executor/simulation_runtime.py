"""
Simulation implementation - pure logic with snapshotted imports.

This module avoids importing policyengine at module level so the worker can
load the requested country module without triggering cross-country imports.
No Modal dependencies here.
"""

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Iterator

from policyengine_observability import ObservabilityRuntime

from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    Stage,
)
from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
    resolve_bundle_dataset_name,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_observability.telemetry import split_internal_payload

logger = logging.getLogger(__name__)

os.environ.setdefault("POLICYENGINE_SKIP_COUNTRY_IMPORTS", "1")

DEFAULT_YEAR = 2026


@dataclass(frozen=True)
class RegionResolution:
    code: str
    dataset_reference: str | None = None
    scoping_strategy: Any | None = None


@dataclass(frozen=True)
class DatasetSelection:
    """One bundle-validated dataset choice shared by loading and artifact reuse."""

    name: str
    uri: str
    is_default: bool


def _normalize_credentials_blob(creds_json: str) -> str:
    """Return the raw JSON blob, decoding the outer escape if present.

    The upstream Modal secret sometimes stores the credentials payload
    double-encoded (the entire JSON object is wrapped in quotes with
    backslash-escaped interior quotes). Historically we always attempted
    the unescape as a fallback which could accidentally parse an already
    clean blob. Only unwrap when the payload looks wrapped."""

    try:
        json.loads(creds_json)
    except json.JSONDecodeError:
        looks_escaped = creds_json.lstrip().startswith('"') or '\\"' in creds_json
        if looks_escaped:
            return json.loads(f'"{creds_json}"')
        raise
    return creds_json


@contextlib.contextmanager
def setup_gcp_credentials(
    runtime: ObservabilityRuntime | None = None,
) -> Iterator[None]:
    """
    Set up GCP credentials from environment variable.

    Modal secrets are injected as environment variables. The GCP library
    expects GOOGLE_APPLICATION_CREDENTIALS to point to a file path. If
    credentials JSON is provided, write it to a temp file that's deleted
    on exit. This runs as a context manager to guarantee cleanup even if
    the caller raises mid-simulation; the previous fire-and-forget
    ``tempfile.mkstemp`` path leaked credential material on disk every
    time a container served a request.
    """
    previous = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    creds_file = None
    try:
        credential_span = (
            runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.CREDENTIAL_SETUP))
            if runtime is not None
            else contextlib.nullcontext()
        )
        with credential_span:
            # Log available GCP-related env vars for debugging.
            gcp_vars = {
                k: v[:50] + "..." if len(v) > 50 else v
                for k, v in os.environ.items()
                if "GOOGLE" in k or "GCP" in k or "CREDENTIAL" in k
            }
            logger.info(f"GCP-related env vars: {list(gcp_vars.keys())}")

            if previous:
                logger.info("GOOGLE_APPLICATION_CREDENTIALS already set")
            else:
                creds_json = (
                    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
                    or os.environ.get("GCP_CREDENTIALS_JSON")
                    or os.environ.get("GOOGLE_CREDENTIALS")
                    or os.environ.get("SERVICE_ACCOUNT_JSON")
                )

                if not creds_json:
                    logger.warning("No GCP credentials found in environment variables")
                else:
                    normalized = _normalize_credentials_blob(creds_json)
                    # ``NamedTemporaryFile(delete=True)`` removes the file when
                    # the context exits. We restore any prior value of
                    # ``GOOGLE_APPLICATION_CREDENTIALS`` so a retry in the same
                    # container doesn't silently pick up a stale path.
                    creds_file = tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".json",
                        delete=True,
                    )
                    creds_file.write(normalized)
                    creds_file.flush()
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file.name
                    logger.info(f"GCP credentials written to {creds_file.name}")

        yield
    finally:
        if creds_file is not None:
            if previous is None:
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            else:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = previous
            creds_file.close()


def run_simulation_impl(
    params: dict,
    *,
    runtime: ObservabilityRuntime,
) -> dict:
    """
    Execute economic simulation.

    Pure implementation with no Modal dependencies.
    Accepts the gateway simulation payload and returns the legacy macro result dict.
    """
    # Set up GCP credentials if needed. The credentials temp file is
    # cleaned up on exit so we never leave signed JSON material on disk.
    with setup_gcp_credentials(runtime):
        try:
            return _run_simulation_impl_core(params, runtime=runtime)
        except ValueError as exc:
            # Gateway images intentionally do not install country packages.
            # Transport the public error through the shared contract instead.
            from policyengine_simulation_contract.spm import (
                SPMInputError,
                spm_error_detail,
            )

            detail = spm_error_detail(exc)
            if detail is not None:
                raise SPMInputError(detail.code, detail.message) from None
            raise


def _parse_year(params: dict[str, Any]) -> int:
    value = params.get("time_period") or params.get("year") or DEFAULT_YEAR
    return int(value)


def _normalise_period_key(period_key: Any) -> str:
    """Convert legacy ``start.stop`` period keys to v4 effective dates."""
    text = str(period_key)
    parts = text.split(".")
    if len(parts) > 1 and len(parts[0]) == 10:
        return parts[0]
    return text


def _normalise_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not policy:
        return None

    normalised: dict[str, Any] = {}
    for parameter, value in policy.items():
        if isinstance(value, dict):
            normalised[parameter] = {
                _normalise_period_key(period): period_value
                for period, period_value in value.items()
            }
        else:
            normalised[parameter] = value
    return normalised


def _resolve_dataset_reference(country: str) -> str:
    return resolve_bundle_dataset_name(country, None)


def _normalise_region_code(country: str, region: Any) -> str:
    if region is None or str(region).strip() == "":
        return country

    raw = str(region).strip()
    if raw.lower() in {"us", "uk"}:
        return raw.lower()

    if "/" not in raw:
        if country == "us" and len(raw) == 2:
            return f"state/{raw.lower()}"
        if country == "uk":
            return f"country/{raw.lower().replace(' ', '_')}"
        return raw

    prefix, value = raw.split("/", maxsplit=1)
    prefix = prefix.lower()
    value = value.strip()
    if prefix == "state":
        value = value.lower()
    elif prefix == "country":
        value = value.lower().replace(" ", "_")
    elif prefix in {"congressional_district", "place"}:
        value = value.upper()
    elif prefix == "local_authority":
        value = value.upper()
    return f"{prefix}/{value}"


def _build_uk_weight_replacement_region(region_code: str):
    if "/" not in region_code:
        return None

    prefix, value = region_code.split("/", maxsplit=1)
    if prefix not in {"constituency", "local_authority"}:
        return None

    from policyengine.core.region import Region
    from policyengine.core.scoping_strategy import WeightReplacementStrategy
    from policyengine.data.uk_geography_assets import (
        CONSTITUENCY_ASSET_SPEC,
        LOCAL_AUTHORITY_ASSET_SPEC,
    )

    asset_spec = (
        CONSTITUENCY_ASSET_SPEC
        if prefix == "constituency"
        else LOCAL_AUTHORITY_ASSET_SPEC
    )
    return Region(
        code=region_code,
        label=value,
        region_type=prefix,
        parent_code="uk",
        scoping_strategy=WeightReplacementStrategy(
            weight_matrix_bucket=asset_spec.bucket,
            weight_matrix_key=asset_spec.weight_matrix_filename,
            lookup_csv_bucket=asset_spec.bucket,
            lookup_csv_key=asset_spec.lookup_csv_filename,
            region_code=value,
            # The bucket copy is replaced by every data release; the matrix
            # comes from the certified bundle instead, placed locally by
            # ``_require_certified_uk_weight_matrix`` before the run.
            download_missing_assets=False,
        ),
    )


def _require_certified_uk_weight_matrix(
    scoping_strategy, dataset, dataset_selection
) -> None:
    """Bind a weight-matrix region to the release of the dataset it reweights.

    ``WeightReplacementStrategy`` gives each household the weight in its
    column of the matrix, by position. Only the matrix certified with the
    selected dataset (same repository and revision in the policyengine.py
    bundle) lines up with its households: another release can have the same
    shape and a different household order. This places that certified,
    digest-checked matrix where the strategy looks first, confirms the
    strategy will read exactly that file, and checks the run year and the
    household dimension. The unversioned bucket copy, which every data
    release replaces, is never downloaded (see
    ``_build_uk_weight_replacement_region``).
    """

    from policyengine.core.scoping_strategy import WeightReplacementStrategy

    if not isinstance(scoping_strategy, WeightReplacementStrategy):
        return

    from pathlib import Path

    import h5py
    import pandas as pd
    from policyengine.data.uk_geography_assets import (
        UKGeographyAssetSpec,
        default_download_dir,
        resolve_uk_geography_asset_paths,
    )
    from policyengine.provenance.dataset_materialization import materialize_dataset
    from policyengine.provenance.manifest import get_release_manifest

    from policyengine_simulation_executor import simulation_output_geographic

    region = scoping_strategy.region_code
    matrix_name = scoping_strategy.weight_matrix_key
    manifest = get_release_manifest("uk")
    package = manifest.data_package

    def release(reference) -> tuple[str, str]:
        return (
            reference.repo_id or package.repo_id,
            reference.revision or package.release_manifest_revision or package.version,
        )

    selected = manifest.datasets.get(dataset_selection.name)
    if selected is None:
        raise ValueError(
            f"UK dataset {dataset_selection.name!r} is not in the certified bundle"
        )
    certified_name = next(
        (
            name
            for name, reference in manifest.datasets.items()
            if reference.path == matrix_name and release(reference) == release(selected)
        ),
        None,
    )
    if certified_name is None:
        raise ValueError(
            f"UK region {region!r} reweights households with {matrix_name}, but the "
            f"certified bundle has no {matrix_name} from the release of "
            f"{dataset_selection.name!r}; a matrix from another release does not "
            "line up with its households."
        )
    certified = materialize_dataset(
        "uk", certified_name, data_dir=default_download_dir()
    )
    spec = UKGeographyAssetSpec(
        geography_type="weight replacement",
        weight_matrix_filename=matrix_name,
        lookup_csv_filename=scoping_strategy.lookup_csv_key,
        bucket=scoping_strategy.weight_matrix_bucket,
        weight_matrix_bucket=scoping_strategy.weight_matrix_bucket,
        lookup_csv_bucket=scoping_strategy.lookup_csv_bucket,
    )
    simulation_output_geographic._required_uk_geography_lookup_csv_path(spec)
    paths = resolve_uk_geography_asset_paths(spec, download_missing_assets=False)
    if Path(paths.weight_matrix_path).resolve() != Path(certified.path).resolve():
        raise ValueError(
            f"UK region {region!r} would read {paths.weight_matrix_path}, not the "
            f"{matrix_name} certified with {dataset_selection.name!r} "
            f"({certified.path})."
        )
    year = str(dataset.year)
    with h5py.File(paths.weight_matrix_path, "r") as matrix:
        if year not in matrix:
            covered = ", ".join(sorted(matrix))
            raise ValueError(
                f"UK region {region!r} reweights households with {matrix_name}, "
                f"which has no weights for {year} (it covers {covered})."
            )
        weights = matrix[year]
        if not isinstance(weights, h5py.Dataset):
            raise TypeError(f"{matrix_name} entry {year} is not a weight matrix")
        matrix_households = weights.shape[-1]
    households = len(pd.DataFrame(dataset.data.entity_data["household"]))
    if households != matrix_households:
        raise ValueError(
            f"UK region {region!r} reweights households with {matrix_name}, "
            f"which was built for {matrix_households} households; the selected "
            f"UK dataset has {households}. Constituency and local-authority "
            "runs on this dataset need a local-area dataset that carries "
            "constituency and local-authority codes."
        )


def _region_parent_dataset_reference(
    country_module,
    country: str,
    region,
) -> str:
    parent_code = getattr(region, "parent_code", None)
    visited: set[str] = set()

    while isinstance(parent_code, str) and parent_code:
        if parent_code in visited:
            raise ValueError(f"Region hierarchy cycle at {parent_code}")
        visited.add(parent_code)

        parent_region = country_module.model.get_region(parent_code)
        parent_dataset_path = getattr(parent_region, "dataset_path", None)
        if isinstance(parent_dataset_path, str):
            bundle = get_country_release_bundle(country)
            return runtime_dataset_uri(
                parent_dataset_path,
                default_revision=bundle.data_package_version,
                artifact_revision=bundle.data_artifact_revision,
                validate_hf=False,
            )
        parent_code = getattr(parent_region, "parent_code", None)

    return _resolve_dataset_reference(country)


def _reject_unscoped_us_place_region(region_code: str, region) -> None:
    if not region_code.startswith("place/"):
        return
    if getattr(region, "dataset_path", None) is not None:
        return
    if getattr(region, "scoping_strategy", None) is not None:
        return
    raise ValueError(
        "US place regions are not yet supported for runtime simulation because "
        "policyengine.py does not expose place-level dataset scoping."
    )


def _resolve_region(
    *,
    country_module,
    country: str,
    params: dict[str, Any],
) -> RegionResolution:
    if params.get("region_group"):
        return _resolve_region_group(
            country_module=country_module, country=country, params=params
        )
    region_code = _normalise_region_code(country, params.get("region"))
    if region_code == country:
        return RegionResolution(
            code=region_code,
            dataset_reference=_resolve_dataset_reference(country),
        )

    region = country_module.model.get_region(region_code)
    if region is None and country == "uk":
        region = _build_uk_weight_replacement_region(region_code)
    if region is None:
        raise ValueError(f"Unsupported {country.upper()} region: {region_code}")
    if country == "us":
        _reject_unscoped_us_place_region(region_code, region)

    dataset_path = getattr(region, "dataset_path", None)
    if isinstance(dataset_path, str):
        bundle = get_country_release_bundle(country)
        dataset_reference = runtime_dataset_uri(
            dataset_path,
            default_revision=bundle.data_package_version,
            artifact_revision=bundle.data_artifact_revision,
            validate_hf=False,
        )
    else:
        dataset_reference = _region_parent_dataset_reference(
            country_module,
            country,
            region,
        )

    return RegionResolution(
        code=region_code,
        dataset_reference=dataset_reference,
        scoping_strategy=getattr(region, "scoping_strategy", None),
    )


def _resolve_region_group(
    *,
    country_module,
    country: str,
    params: dict[str, Any],
) -> RegionResolution:
    """Resolve a ``region_group`` (list of member region codes) to one
    ``RegionGroupStrategy`` that scopes a single simulation to the union of the
    members.

    Members must be row-filter regions (states/CDs) so national weights are
    preserved (INV-5); the group filters the national dataset.
    """
    from policyengine.core.scoping_strategy import (
        RegionGroupStrategy,
        RowFilterStrategy,
    )

    codes = [
        _normalise_region_code(country, code)
        for code in (params.get("region_group") or [])
    ]
    if not codes:
        raise ValueError("region_group must contain at least one region code")

    members = []
    for code in codes:
        region = country_module.model.get_region(code)
        if region is None:
            raise ValueError(f"Unsupported {country.upper()} region in group: {code}")
        scoping_strategy = getattr(region, "scoping_strategy", None)
        if not isinstance(scoping_strategy, RowFilterStrategy):
            raise ValueError(
                f"Region group member '{code}' is not a row-filter region "
                f"(got {type(scoping_strategy).__name__}); region groups must "
                "preserve national weights."
            )
        members.append(scoping_strategy)

    return RegionResolution(
        code="region_group/" + "+".join(sorted(codes)),
        dataset_reference=_resolve_dataset_reference(country),
        scoping_strategy=RegionGroupStrategy(members=members),
    )


def _country_module(country: str):
    country = country.lower()
    if country not in {"us", "uk"}:
        raise ValueError(f"Unsupported country: {country}")

    return import_module(f"policyengine.tax_benefit_models.{country}")


def resolve_data_folder() -> str:
    """The folder datasets and baseline artifacts are read from and saved to.

    One home for the default: the precompute downloads store artifacts into
    this folder for ``ensure_datasets``/``Simulation.ensure()`` to find, so
    any divergence from ``_load_dataset``'s resolution strands them.
    """
    return os.environ.get("POLICYENGINE_DATA_FOLDER", "/tmp/policyengine-data")


def _nondefault_data_folder(country: str, name: str, uri: str) -> str:
    """Separate managed datasets even if their source filenames have the same stem."""
    identity = hashlib.sha256(f"{country}:{name}:{uri}".encode()).hexdigest()
    return f"/tmp/policyengine-alternate-data/{identity}"


def _resolve_dataset_selection(
    params: dict[str, Any],
    *,
    region_resolution: RegionResolution | None = None,
) -> DatasetSelection:
    """Resolve the bundle's regional or default dataset to a managed name."""
    if "data" in params or "data_version" in params:
        raise ValueError("Dataset overrides are not supported")
    country = params.get("country", "us").lower()
    bundle = get_country_release_bundle(country)
    if region_resolution is not None and region_resolution.dataset_reference:
        dataset_reference = region_resolution.dataset_reference
    else:
        dataset_reference = bundle.default_dataset

    # Region metadata can contain a URI rather than a managed dataset name.
    for name, uri in bundle.dataset_uris.items():
        if dataset_reference == name or dataset_reference == uri:
            return DatasetSelection(name, uri, name == bundle.default_dataset)
    for name, uri in bundle.dataset_uris.items():
        if dataset_reference == runtime_dataset_uri(
            uri,
            default_revision=bundle.data_package_version,
            artifact_revision=bundle.data_artifact_revision,
            validate_hf=False,
        ):
            return DatasetSelection(name, uri, name == bundle.default_dataset)
    raise ValueError(
        f"Unsupported dataset {dataset_reference!r} for country {country!r}; "
        "choose a name in the certified release manifest"
    )


def _load_dataset(
    params: dict[str, Any],
    *,
    selection: DatasetSelection,
    country_module=None,
):
    country = params.get("country", "us").lower()
    year = _parse_year(params)
    country_module = country_module or _country_module(country)
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
    dataset,
    dataset_selection: DatasetSelection,
    policy: dict[str, Any] | None,
    scoping_strategy=None,
    region_code: str | None = None,
):
    from policyengine.core import Simulation

    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
        deterministic_baseline_id,
    )

    from policyengine_simulation_executor.spm import normalize_runtime_spm

    selection = normalize_runtime_spm(params)
    params = {**params, **({"spm": selection} if selection is not None else {})}
    spm_kwargs = {"spm": selection} if selection is not None else {}
    country = params.get("country", "us")
    country_module = _country_module(country)
    simulation_id = deterministic_baseline_id(
        params,
        country=country,
        dataset_is_default=dataset_selection.is_default,
        policy=policy,
        region_code=region_code,
        scoping_strategy=scoping_strategy,
        year=_parse_year(params),
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
            policy=policy,
            scoping_strategy=scoping_strategy,
        )
    return Simulation(
        **spm_kwargs,
        dataset=dataset,
        tax_benefit_model_version=country_module.model,
        policy=policy,
        scoping_strategy=scoping_strategy,
    )


def _run_simulation_impl_core(
    params: dict,
    *,
    runtime: ObservabilityRuntime,
) -> dict:
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.REQUEST_PARSE)):
        simulation_params, telemetry, metadata = split_internal_payload(params)
    metadata = metadata or {}
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
        simulation_spm_result,
    )

    selection = normalize_runtime_spm(simulation_params)
    if selection is not None:
        simulation_params["spm"] = selection

    logger.info(
        "Starting simulation for country=%s submission_claim_id=%s",
        simulation_params.get("country", "unknown"),
        getattr(telemetry, "submission_claim_id", None),
    )
    if metadata:
        logger.info("Received simulation metadata keys: %s", sorted(metadata))

    country = simulation_params.get("country", "us").lower()
    _set_runtime_attributes(
        runtime=runtime,
        simulation_params=simulation_params,
        telemetry=telemetry,
        metadata=metadata,
        country=country,
    )

    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.COUNTRY_MODULE_LOAD)):
        country_module = _country_module(country)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.REGION_RESOLUTION)):
        region_resolution = _resolve_region(
            country_module=country_module,
            country=country,
            params=simulation_params,
        )
    runtime.set_context(region=region_resolution.code)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.DATASET_RESOLUTION)):
        dataset_selection = _resolve_dataset_selection(
            simulation_params, region_resolution=region_resolution
        )
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.DATASET_LOAD)):
        dataset = _load_dataset(
            simulation_params,
            selection=dataset_selection,
            country_module=country_module,
        )
    from policyengine_simulation_executor.uk_local_authority_metadata import (
        detect_uk_local_authority_metadata,
    )

    uk_local_authority_metadata = detect_uk_local_authority_metadata(
        country, dataset, region_code=region_resolution.code
    )
    _require_certified_uk_weight_matrix(
        region_resolution.scoping_strategy, dataset, dataset_selection
    )
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.POLICY_NORMALIZATION)):
        baseline_policy = _normalise_policy(simulation_params.get("baseline"))
        reform_policy = _normalise_policy(simulation_params.get("reform"))

    logger.info("Initialising baseline and reform simulations")
    with runtime.span(
        ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_BUILD),
        attributes={"simulation_kind": "baseline"},
    ):
        baseline = _build_simulation(
            simulation_params,
            dataset=dataset,
            dataset_selection=dataset_selection,
            policy=baseline_policy,
            scoping_strategy=region_resolution.scoping_strategy,
            region_code=region_resolution.code,
        )
    with runtime.span(
        ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_BUILD),
        attributes={"simulation_kind": "reform"},
    ):
        reform = _build_simulation(
            simulation_params,
            dataset=dataset,
            dataset_selection=dataset_selection,
            policy=reform_policy,
            scoping_strategy=region_resolution.scoping_strategy,
            region_code=region_resolution.code,
        )

    logger.info("Calculating economic impact")
    builder = SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=dataset,
        baseline=baseline,
        reform=reform,
        resolved_data_version=None,
        resolved_region_code=region_resolution.code,
        runtime=runtime,
        uk_local_authority_metadata=uk_local_authority_metadata,
    )
    output = builder.serialize()
    output.update(
        simulation_spm_result(
            baseline, reform, selection, expected_year=_parse_year(simulation_params)
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
        from policyengine_simulation_executor.simulation_microdata import (
            extract_output_microdata,
        )

        output["_microdata"] = extract_output_microdata(baseline, reform)
    return output


def _set_runtime_attributes(
    *,
    runtime: ObservabilityRuntime,
    simulation_params: dict[str, Any],
    telemetry,
    metadata: dict[str, Any],
    country: str,
) -> None:
    runtime.set_context(
        country=country,
        scope=simulation_params.get("scope"),
        simulation_year=_parse_year(simulation_params),
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
