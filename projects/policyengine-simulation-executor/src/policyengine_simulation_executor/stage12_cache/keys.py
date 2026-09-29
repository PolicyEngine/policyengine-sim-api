"""Content-derived Stage 12 cache keys and object paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    ResolvedStage12Bundle,
)
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    stage12_output_plan_sha256,
)

DATASET_SCHEMA = "stage12-ds1"
BASELINE_SCHEMA = "stage12-bl1"
CURRENT_LAW_POLICY = "current-law"
_ID_DIGEST_CHARS = 16


def canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Stage12DatasetCacheIdentity:
    country: CountryId
    dataset: str
    stem: str
    year: int
    source_sha256: str
    data_artifact_revision: str
    data_package_version: str
    country_package_version: str
    core_package_version: str
    policyengine_version: str
    bundle_manifest_sha256: str

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "schema": DATASET_SCHEMA,
            "country": self.country,
            "dataset": self.dataset,
            "year": self.year,
            "source_sha256": self.source_sha256,
            "data_artifact_revision": self.data_artifact_revision,
            "data_package_version": self.data_package_version,
            "country_package_version": self.country_package_version,
            "core_package_version": self.core_package_version,
            "policyengine_version": self.policyengine_version,
            "bundle_manifest_sha256": self.bundle_manifest_sha256,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload)

    @property
    def filename(self) -> str:
        return f"{self.stem}_year_{self.year}.h5"

    @property
    def store_path(self) -> str:
        return f"datasets/{self.country}/{self.digest}/{self.filename}"


@dataclass(frozen=True)
class Stage12BaselineCacheIdentity:
    dataset: Stage12DatasetCacheIdentity
    region_codes: tuple[str, ...]
    scope_key: str
    output_plan_sha256: str
    optional_configuration: Any = None

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "schema": BASELINE_SCHEMA,
            "country": self.dataset.country,
            "dataset_digest": self.dataset.digest,
            "region_codes": list(self.region_codes),
            "scope_key": self.scope_key,
            "output_plan_sha256": self.output_plan_sha256,
            "optional_configuration": self.optional_configuration,
            "policy": CURRENT_LAW_POLICY,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload)

    @property
    def simulation_id(self) -> str:
        return f"{BASELINE_SCHEMA}-{self.digest[:_ID_DIGEST_CHARS]}"

    @property
    def filename(self) -> str:
        return f"{self.simulation_id}.h5"

    @property
    def store_path(self) -> str:
        return f"baselines/{self.dataset.country}/{self.digest}/{self.filename}"


def collect_dataset_identity(
    resolved: ResolvedStage12Bundle,
    *,
    country: CountryId,
    year: int,
) -> Stage12DatasetCacheIdentity:
    """Collect dataset identity only from the installed Stage 12 bundle."""

    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == country
    )
    dataset = next(
        item
        for item in country_bundle.datasets
        if item.identity == country_bundle.default_dataset
    )
    stem = dataset_logical_name(
        resolve_dataset_reference(country, country_bundle.default_dataset)
    )
    return Stage12DatasetCacheIdentity(
        country=country,
        dataset=dataset.identity,
        stem=stem,
        year=year,
        source_sha256=dataset.sha256,
        data_artifact_revision=dataset.artifact_revision,
        data_package_version=country_bundle.data_package_version,
        country_package_version=country_bundle.country_package_version,
        core_package_version=resolved.bundle.core_package_version,
        policyengine_version=resolved.bundle.policyengine_version,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
    )


def collect_baseline_identity(
    simulation: PlannedSimulationExecutionInput,
    *,
    resolved: ResolvedStage12Bundle,
    region_codes: tuple[str, ...],
    scope_key: str,
) -> Stage12BaselineCacheIdentity:
    """Build the identity shared by the Stage 12 writer and reader."""

    optional_configuration = simulation.options.get("spm")
    return Stage12BaselineCacheIdentity(
        dataset=collect_dataset_identity(
            resolved,
            country=simulation.geography.country,
            year=simulation.year,
        ),
        region_codes=region_codes,
        scope_key=scope_key,
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        optional_configuration=optional_configuration,
    )
