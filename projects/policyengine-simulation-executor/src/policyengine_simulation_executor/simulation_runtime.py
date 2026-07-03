"""
Simulation implementation - pure logic with snapshotted imports.

This module avoids importing policyengine at module level so the worker can
load the requested country module without triggering cross-country imports.
No Modal dependencies here.
"""

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Iterator

from policyengine_observability import segment, set_attribute

from policyengine_simulation_executor.dataset_uri import runtime_dataset_uri
from policyengine_simulation_observability.observability import SegmentName
from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
    resolve_bundle_dataset_name,
    resolve_runtime_bundle_dataset_uri,
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
def setup_gcp_credentials() -> Iterator[None]:
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
        with segment(SegmentName.CREDENTIAL_SETUP):
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


def run_simulation_impl(params: dict) -> dict:
    """
    Execute economic simulation.

    Pure implementation with no Modal dependencies.
    Accepts the gateway simulation payload and returns the legacy macro result dict.
    """
    # Set up GCP credentials if needed. The credentials temp file is
    # cleaned up on exit so we never leave signed JSON material on disk.
    with setup_gcp_credentials():
        return _run_simulation_impl_core(params)


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


def _split_requested_revision(requested_data: str) -> tuple[str, str | None]:
    if "@" not in requested_data:
        return requested_data, None
    dataset_name, revision = requested_data.rsplit("@", maxsplit=1)
    if not dataset_name or not revision:
        raise ValueError(f"Invalid dataset revision reference: {requested_data}")
    return dataset_name, revision


def _requested_data_version(params: dict[str, Any]) -> str | None:
    data_version = params.get("data_version")
    if data_version is not None:
        return str(data_version)

    data = params.get("data")
    if isinstance(data, str) and "@" in data:
        _, revision = _split_requested_revision(data)
        return revision
    return None


def _resolve_dataset_reference(country: str, params: dict[str, Any]) -> str:
    with segment(SegmentName.DATASET_RESOLUTION):
        return _resolve_dataset_reference_inner(country, params)


def _resolve_dataset_reference_inner(country: str, params: dict[str, Any]) -> str:
    requested_data = params.get("data")
    requested_data = requested_data if isinstance(requested_data, str) else None
    requested_data_version = _requested_data_version(params)
    if requested_data is None and requested_data_version is None:
        return resolve_bundle_dataset_name(country, requested_data)
    return resolve_runtime_bundle_dataset_uri(
        country,
        requested_data,
        requested_data_version,
        prefer_local=False,
    )


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
        ),
    )


def _region_parent_dataset_reference(
    country_module,
    country: str,
    region,
    params: dict[str, Any],
) -> str:
    parent_code = getattr(region, "parent_code", None)
    requested_data_version = _requested_data_version(params)
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
                default_revision=bundle.data_version,
                override_revision=requested_data_version,
                artifact_revision=bundle.data_artifact_revision,
                validate_hf=False,
            )
        parent_code = getattr(parent_region, "parent_code", None)

    return _resolve_dataset_reference(country, params)


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
    region_code = _normalise_region_code(country, params.get("region"))
    if region_code == country:
        return RegionResolution(
            code=region_code,
            dataset_reference=_resolve_dataset_reference(country, params),
        )

    region = country_module.model.get_region(region_code)
    if region is None and country == "uk":
        region = _build_uk_weight_replacement_region(region_code)
    if region is None:
        raise ValueError(f"Unsupported {country.upper()} region: {region_code}")
    if country == "us":
        _reject_unscoped_us_place_region(region_code, region)

    dataset_path = getattr(region, "dataset_path", None)
    requested_data_version = _requested_data_version(params)
    if isinstance(dataset_path, str):
        bundle = get_country_release_bundle(country)
        dataset_reference = runtime_dataset_uri(
            dataset_path,
            default_revision=bundle.data_version,
            override_revision=requested_data_version,
            artifact_revision=bundle.data_artifact_revision,
            validate_hf=False,
        )
    else:
        dataset_reference = _region_parent_dataset_reference(
            country_module,
            country,
            region,
            params,
        )

    return RegionResolution(
        code=region_code,
        dataset_reference=dataset_reference,
        scoping_strategy=getattr(region, "scoping_strategy", None),
    )


def _country_module(country: str):
    country = country.lower()
    if country not in {"us", "uk"}:
        raise ValueError(f"Unsupported country: {country}")

    return import_module(f"policyengine.tax_benefit_models.{country}")


def _load_dataset(
    params: dict[str, Any],
    *,
    country_module=None,
    region_resolution: RegionResolution | None = None,
):
    country = params.get("country", "us").lower()
    year = _parse_year(params)
    country_module = country_module or _country_module(country)
    dataset_name = (
        region_resolution.dataset_reference
        if region_resolution is not None and region_resolution.dataset_reference
        else _resolve_dataset_reference(country, params)
    )
    data_folder = os.environ.get("POLICYENGINE_DATA_FOLDER", "/tmp/policyengine-data")
    # TEMPORARY: remove once single-year datasets are published (issue #596).
    # The image bakes default-revision single-year files into
    # POLICYENGINE_DATA_FOLDER, and ensure_datasets keys its cache on a
    # revision-stripped filename stem — a custom dataset or revision whose
    # stem matches the default would silently read the baked files. Only
    # pure default requests may use the baked folder. (Region requests
    # resolve their dataset without the "data" param, so they keep it.)
    if params.get("data") is not None or params.get("data_version") is not None:
        data_folder = "/tmp/policyengine-data"

    start = time.monotonic()
    datasets = country_module.ensure_datasets(
        datasets=[dataset_name],
        years=[year],
        data_folder=data_folder,
    )
    logger.info(
        "Loaded dataset %s year %s from %s in %.1fs",
        dataset_name,
        year,
        data_folder,
        time.monotonic() - start,
    )
    return next(iter(datasets.values()))


def _build_simulation(
    params: dict[str, Any],
    *,
    dataset,
    policy: dict[str, Any] | None,
    scoping_strategy=None,
):
    from policyengine.core import Simulation

    country_module = _country_module(params.get("country", "us"))
    return Simulation(
        dataset=dataset,
        tax_benefit_model_version=country_module.model,
        policy=policy,
        scoping_strategy=scoping_strategy,
    )


def _run_simulation_impl_core(params: dict) -> dict:
    with segment(SegmentName.REQUEST_PARSE):
        simulation_params, telemetry, metadata = split_internal_payload(params)
    metadata = metadata or {}

    logger.info(
        "Starting simulation for country=%s run_id=%s process_id=%s",
        simulation_params.get("country", "unknown"),
        getattr(telemetry, "run_id", None),
        getattr(telemetry, "process_id", None),
    )
    if metadata:
        logger.info("Received simulation metadata keys: %s", sorted(metadata))

    country = simulation_params.get("country", "us").lower()
    _set_runtime_attributes(
        simulation_params=simulation_params,
        telemetry=telemetry,
        metadata=metadata,
        country=country,
    )

    with segment(SegmentName.COUNTRY_MODULE_LOAD):
        country_module = _country_module(country)
    with segment(SegmentName.REGION_RESOLUTION):
        region_resolution = _resolve_region(
            country_module=country_module,
            country=country,
            params=simulation_params,
        )
    set_attribute("region", region_resolution.code)
    with segment(SegmentName.DATASET_LOAD):
        dataset = _load_dataset(
            simulation_params,
            country_module=country_module,
            region_resolution=region_resolution,
        )
    with segment(SegmentName.POLICY_NORMALIZATION):
        baseline_policy = _normalise_policy(simulation_params.get("baseline"))
        reform_policy = _normalise_policy(simulation_params.get("reform"))

    logger.info("Initialising baseline and reform simulations")
    with segment(SegmentName.SIMULATION_BUILD, simulation_kind="baseline"):
        baseline = _build_simulation(
            simulation_params,
            dataset=dataset,
            policy=baseline_policy,
            scoping_strategy=region_resolution.scoping_strategy,
        )
    with segment(SegmentName.SIMULATION_BUILD, simulation_kind="reform"):
        reform = _build_simulation(
            simulation_params,
            dataset=dataset,
            policy=reform_policy,
            scoping_strategy=region_resolution.scoping_strategy,
        )

    logger.info("Calculating economic impact")
    builder = SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=dataset,
        baseline=baseline,
        reform=reform,
        resolved_data_version=_requested_data_version(simulation_params),
    )
    output = builder.serialize()
    logger.info("Comparison complete")
    return output


def _set_runtime_attributes(
    *,
    simulation_params: dict[str, Any],
    telemetry,
    metadata: dict[str, Any],
    country: str,
) -> None:
    set_attribute("country", country)
    set_attribute("scope", simulation_params.get("scope"))
    set_attribute("simulation_year", _parse_year(simulation_params))
    set_attribute("run_id", getattr(telemetry, "run_id", None))
    set_attribute("process_id", getattr(telemetry, "process_id", None))
    set_attribute("request_id", getattr(telemetry, "request_id", None))
    set_attribute("geography_code", getattr(telemetry, "geography_code", None))
    set_attribute("geography_type", getattr(telemetry, "geography_type", None))

    set_attribute("resolved_version", metadata.get("resolved_version"))
    set_attribute("resolved_app_name", metadata.get("resolved_app_name"))
    bundle = metadata.get("policyengine_bundle")
    if isinstance(bundle, dict):
        set_attribute("policyengine_version", bundle.get("policyengine_version"))
        set_attribute("model_version", bundle.get("model_version"))
        set_attribute("data_version", bundle.get("data_version"))
