"""Content-addressed identity for precomputed simulation artifacts.

The artifact pipeline caches two artifact types in a shared store: single-year
dataset files and precomputed baseline simulation outputs. An artifact's key is
a sha256 digest over the FULL closure of inputs that determine its bytes, so
"is it cached?" and "is it stale?" are the same question, answered by key
equality: same key implies a byte-identical artifact; any input change rotates
the key and the old object simply stops being referenced (garbage collection
deletes by key with no correctness risk — a wrongly deleted artifact costs a
recompute, never a wrong answer).

Key discipline: every field that can change an artifact's bytes must appear in
its payload. Version labels alone are not enough — a data re-release under an
unchanged version label is a known event — so the payloads also carry the
receipt's content sha256 and the certification data-build fingerprint when
available. ``None`` fields are serialized as explicit nulls, so presence and
absence are distinct identities. Bump the schema constants when the payload
shape or the artifact format itself changes.

The baseline payload nests the dataset digest, so anything that rotates a
dataset key transitively rotates every baseline built on it. ``country`` and
``region`` are partially redundant with ``scope_key`` and the nested dataset
key; the redundancy is deliberate — for a cache key, explicitness only ever
makes identity more specific, and it means UK or single-state baselines need
no schema change later.

This module is the single source of truth for keys, ids, and store paths: the
precompute writer, the runtime reader, and the GC planner all import it, so a
drift between writer and reader is a code-review diff here, not a silent
cache miss in production.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

DATASET_KEY_SCHEMA = "ds1"
BASELINE_KEY_SCHEMA = "bl1"

# Length of the digest prefix used in simulation ids and store paths. 16 hex
# chars (64 bits) is far beyond collision range for this population while
# keeping filenames readable in logs.
_ID_DIGEST_CHARS = 16

CURRENT_LAW_POLICY = "current-law"
NATIONAL_REGION = "national"


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON encoding of ``payload``.

    Canonical means: keys sorted, no whitespace, ASCII-only escapes. Any
    nested structure must itself be JSON-serializable with deterministic
    content (no sets, no floats that vary in repr).
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dataset_key(
    *,
    country: str,
    dataset: str,
    year: int,
    data_version: str,
    data_artifact_revision: str,
    source_sha256: Optional[str],
    data_build_fingerprint: Optional[str],
    model_version: str,
    policyengine_version: str,
) -> str:
    """Digest identifying one single-year dataset artifact.

    ``model_version`` and ``policyengine_version`` belong here because the
    single-year build runs a full Microsimulation pass — its output depends
    on model code, not just source data. ``source_sha256`` is the installed
    multi-year source's content hash from the bundle receipt (byte identity;
    closes the same-version re-release hole), ``data_build_fingerprint`` the
    certification fingerprint; both may be None when unavailable.
    """
    return canonical_digest(
        {
            "schema": DATASET_KEY_SCHEMA,
            "country": country.lower(),
            "dataset": dataset,
            "year": int(year),
            "data_version": data_version,
            "data_artifact_revision": data_artifact_revision,
            "source_sha256": source_sha256,
            "data_build_fingerprint": data_build_fingerprint,
            "model_version": model_version,
            "policyengine_version": policyengine_version,
        }
    )


def baseline_key(
    *,
    country: str,
    region: str,
    scope_key: Optional[str],
    dataset_digest: str,
    model_version: str,
    policyengine_version: str,
    policy: str = CURRENT_LAW_POLICY,
) -> str:
    """Digest identifying one precomputed baseline simulation artifact.

    ``scope_key`` is the ``ScopingStrategy.cache_key`` string (None for an
    unscoped national run); ``region`` is the human-readable label
    ("national", "state/ca", ...). The simulation year rides in via
    ``dataset_digest``.
    """
    return canonical_digest(
        {
            "schema": BASELINE_KEY_SCHEMA,
            "country": country.lower(),
            "region": region,
            "scope_key": scope_key,
            "dataset_key": dataset_digest,
            "policy": policy,
            "model_version": model_version,
            "policyengine_version": policyengine_version,
        }
    )


def baseline_simulation_id(baseline_digest: str) -> str:
    """The deterministic ``Simulation.id`` for a baseline artifact.

    The id doubles as the artifact filename stem (``{id}.h5`` beside the
    dataset — the path ``Simulation.ensure()`` already loads from), so it
    must be filename-safe and must not collide with the dataset filename
    pattern ``{stem}_year_{year}.h5``.
    """
    return f"{BASELINE_KEY_SCHEMA}-{baseline_digest[:_ID_DIGEST_CHARS]}"


# --- Store layout ---------------------------------------------------------
#
# datasets/<country>/<digest>/<stem>_year_<year>.h5
# baselines/<country>/<digest>/<id>.h5
# manifests/<digest>.json
# deployed/<environment>.json
#
# The filename inside a dataset object's path is EXACTLY the name the
# runtime's ensure_datasets existence check resolves — the fetch layer
# downloads objects to POLICYENGINE_DATA_FOLDER under their basenames, so
# the object basename IS the runtime filename contract.


def dataset_artifact_filename(stem: str, year: int) -> str:
    return f"{stem}_year_{year}.h5"


def dataset_artifact_path(
    country: str, dataset_digest: str, stem: str, year: int
) -> str:
    return (
        f"datasets/{country.lower()}/{dataset_digest}/"
        f"{dataset_artifact_filename(stem, year)}"
    )


def baseline_artifact_path(
    country: str, baseline_digest: str, simulation_id: str
) -> str:
    return f"baselines/{country.lower()}/{baseline_digest}/{simulation_id}.h5"


def manifest_path(manifest_digest: str) -> str:
    return f"manifests/{manifest_digest}.json"


def deployed_marker_path(environment: str) -> str:
    return f"deployed/{environment}.json"


# --- Version-identity collection ------------------------------------------


@dataclass(frozen=True)
class DatasetArtifactIdentity:
    """Everything needed to key and name one single-year dataset artifact."""

    country: str
    dataset: str
    stem: str
    year: int
    data_version: str
    data_artifact_revision: str
    source_sha256: Optional[str]
    data_build_fingerprint: Optional[str]
    model_version: str
    policyengine_version: str

    @property
    def digest(self) -> str:
        return dataset_key(
            country=self.country,
            dataset=self.dataset,
            year=self.year,
            data_version=self.data_version,
            data_artifact_revision=self.data_artifact_revision,
            source_sha256=self.source_sha256,
            data_build_fingerprint=self.data_build_fingerprint,
            model_version=self.model_version,
            policyengine_version=self.policyengine_version,
        )

    @property
    def filename(self) -> str:
        return dataset_artifact_filename(self.stem, self.year)

    @property
    def store_path(self) -> str:
        return dataset_artifact_path(self.country, self.digest, self.stem, self.year)


@dataclass(frozen=True)
class BaselineArtifactIdentity:
    """Everything needed to key, id, and name one baseline artifact."""

    country: str
    region: str
    scope_key: Optional[str]
    dataset: DatasetArtifactIdentity

    @property
    def digest(self) -> str:
        return baseline_key(
            country=self.country,
            region=self.region,
            scope_key=self.scope_key,
            dataset_digest=self.dataset.digest,
            model_version=self.dataset.model_version,
            policyengine_version=self.dataset.policyengine_version,
        )

    @property
    def simulation_id(self) -> str:
        return baseline_simulation_id(self.digest)

    @property
    def store_path(self) -> str:
        return baseline_artifact_path(self.country, self.digest, self.simulation_id)


def _receipt_source_sha256(country: str, data_version: str) -> Optional[str]:
    """Content hash of the installed multi-year source, when certifiable.

    Only trusted when the receipt entry's version matches the bundle's data
    version (mirroring ``resolve_local_bundle_dataset_path``) — a receipt
    left over from a different install must not leak into the key.
    """
    from policyengine_simulation_executor.release_bundle import _receipt_dataset

    entry = _receipt_dataset(country)
    if not isinstance(entry, Mapping):
        return None
    if entry.get("version") != data_version:
        return None
    for field in ("installed_sha256", "expected_sha256"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _certification_fingerprint(country: str) -> Optional[str]:
    from policyengine.provenance.manifest import get_release_manifest

    certification = getattr(get_release_manifest(country), "certification", None)
    fingerprint = getattr(certification, "data_build_fingerprint", None)
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def collect_dataset_identity(country: str, year: int) -> DatasetArtifactIdentity:
    """Identity of the default single-year dataset for this installed bundle.

    Reads the same sources the runtime trusts: the release bundle (versions),
    the bundle receipt (content sha), and the manifest certification
    (fingerprint). The stem comes through the same manifest helpers the
    prebuild and runtime lookups use, so filename coupling is inherited, not
    re-derived.
    """
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle(country)
    stem = dataset_logical_name(
        resolve_dataset_reference(bundle.country, bundle.default_dataset)
    )
    return DatasetArtifactIdentity(
        country=bundle.country,
        dataset=bundle.default_dataset,
        stem=stem,
        year=int(year),
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        source_sha256=_receipt_source_sha256(bundle.country, bundle.data_version),
        data_build_fingerprint=_certification_fingerprint(bundle.country),
        model_version=bundle.model_version,
        policyengine_version=bundle.policyengine_version,
    )


def collect_baseline_identity(
    country: str,
    year: int,
    *,
    region: str,
    scope_key: Optional[str],
) -> BaselineArtifactIdentity:
    return BaselineArtifactIdentity(
        country=country.lower(),
        region=region,
        scope_key=scope_key,
        dataset=collect_dataset_identity(country, year),
    )
