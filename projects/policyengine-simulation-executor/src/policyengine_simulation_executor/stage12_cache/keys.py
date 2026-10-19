"""Content-derived Stage 12 cache keys and object paths."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    stage12_output_plan_sha256,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        ResolvedStage12Bundle,
    )

DATASET_SCHEMA = "stage12-ds1"
BASELINE_SCHEMA = "stage12-bl1"
CURRENT_LAW_POLICY = "current-law"
_ID_DIGEST_CHARS = 16


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of ``payload`` as canonical JSON.

    Keys are sorted, separators are compact and the text is ASCII, so equal
    payloads always give equal digests. NaN or infinity raises ``ValueError``.
    """
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
    """Identity of one cached dataset year, taken from the installed bundle.

    Any change to the dataset, its source digest or a package version gives
    a new digest, and so a new cache path.
    """

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
        """The fields hashed into ``digest``."""
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
        """The canonical digest of ``payload``; it names the cache directory."""
        return canonical_digest(self.payload)

    @property
    def filename(self) -> str:
        """The dataset file name: its logical name and year."""
        return f"{self.stem}_year_{self.year}.h5"

    @property
    def store_path(self) -> str:
        """The object path in the cache bucket."""
        return f"datasets/{self.country}/{self.digest}/{self.filename}"


@dataclass(frozen=True)
class Stage12BaselineCacheIdentity:
    """Identity of one cached current-law baseline for a group of regions.

    It combines the dataset identity, the region codes and scope key, the
    output plan digest and the resolved SPM selection.
    """

    dataset: Stage12DatasetCacheIdentity
    region_codes: tuple[str, ...]
    scope_key: str
    output_plan_sha256: str
    spm: Mapping[str, Any] | None = None

    @property
    def payload(self) -> dict[str, Any]:
        """The fields hashed into ``digest``; the policy is always current law."""
        return {
            "schema": BASELINE_SCHEMA,
            "country": self.dataset.country,
            "dataset_digest": self.dataset.digest,
            "region_codes": list(self.region_codes),
            "scope_key": self.scope_key,
            "output_plan_sha256": self.output_plan_sha256,
            "spm": dict(self.spm) if self.spm is not None else None,
            "policy": CURRENT_LAW_POLICY,
        }

    @property
    def digest(self) -> str:
        """The canonical digest of ``payload``; it names the cache directory."""
        return canonical_digest(self.payload)

    @property
    def simulation_id(self) -> str:
        """The simulation ID: the schema and the digest's first 16 characters."""
        return f"{BASELINE_SCHEMA}-{self.digest[:_ID_DIGEST_CHARS]}"

    @property
    def storage_id(self) -> str:
        """The name .py saves and loads this baseline under.

        Mirrors ``policyengine.core.Simulation.storage_id``: a US simulation
        always has a resolved SPM selection, so .py appends its digest.
        """
        if self.spm is None:
            return self.simulation_id
        return f"{self.simulation_id}-spm-{canonical_digest(self.spm)}"

    @property
    def filename(self) -> str:
        """The file name .py saves and loads: ``storage_id`` plus ``.h5``."""
        return f"{self.storage_id}.h5"

    @property
    def store_path(self) -> str:
        """The object path in the cache bucket."""
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
    spm: Mapping[str, Any] | None,
) -> Stage12BaselineCacheIdentity:
    """Build the identity shared by the Stage 12 writer and reader.

    ``spm`` is the resolved selection the simulation is built with, so the
    identity's file name matches the one .py saves and loads.
    """
    return Stage12BaselineCacheIdentity(
        dataset=collect_dataset_identity(
            resolved,
            country=simulation.geography.country,
            year=simulation.year,
        ),
        region_codes=region_codes,
        scope_key=scope_key,
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        spm=spm,
    )
