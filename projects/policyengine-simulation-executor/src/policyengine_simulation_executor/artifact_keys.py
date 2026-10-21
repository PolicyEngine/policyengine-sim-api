"""Content-addressed identity for precomputed simulation artifacts.

The artifact pipeline caches three artifact types in a shared store: single-year
dataset files, per-state single-year dataset files (a regional dataset's
year file filtered to one state's households) and precomputed baseline
simulation outputs. An artifact's key is
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
from typing import Any

DATASET_KEY_SCHEMA = "ds1"
STATE_DATASET_KEY_SCHEMA = "sds1"
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


@dataclass(frozen=True)
class DatasetKeyInputs:
    """Every input that can change one single-year dataset artifact's bytes.

    ``model_version`` and ``policyengine_version`` belong here because the
    single-year build runs a full Microsimulation pass — its output depends
    on model code, not just source data. ``source_sha256`` is the installed
    multi-year source's content hash from the bundle receipt (byte identity;
    closes the same-version re-release hole), ``data_build_fingerprint`` the
    certification fingerprint; both may be None when unavailable.
    """

    country: str
    dataset: str
    year: int
    data_version: str
    data_artifact_revision: str
    source_sha256: str | None
    data_build_fingerprint: str | None
    model_version: str
    policyengine_version: str
    spm: dict | None = None


def dataset_key(inputs: DatasetKeyInputs) -> str:
    """Digest identifying one single-year dataset artifact."""
    return canonical_digest(
        {
            "schema": DATASET_KEY_SCHEMA if inputs.spm is None else "ds2-spm",
            **({"spm": inputs.spm} if inputs.spm is not None else {}),
            "country": inputs.country.lower(),
            "dataset": inputs.dataset,
            "year": int(inputs.year),
            "data_version": inputs.data_version,
            "data_artifact_revision": inputs.data_artifact_revision,
            "source_sha256": inputs.source_sha256,
            "data_build_fingerprint": inputs.data_build_fingerprint,
            "model_version": inputs.model_version,
            "policyengine_version": inputs.policyengine_version,
        }
    )


@dataclass(frozen=True)
class StateDatasetKeyInputs:
    """Every input that can change one per-state single-year dataset's bytes.

    The file is the regional dataset's year file (built by a full
    Microsimulation pass, so model and .py versions matter) filtered to the
    households whose ``filter_variable`` equals ``state_fips``.
    ``dataset_uri`` pins the source to its Hugging Face revision and
    ``source_sha256`` is the bundle's certified hash of that source, which may
    be None when the bundle has none.
    """

    country: str
    dataset: str
    dataset_uri: str
    source_sha256: str | None
    year: int
    state_code: str
    state_fips: int
    filter_variable: str
    model_version: str
    policyengine_version: str
    spm: dict | None = None


def state_dataset_key(inputs: StateDatasetKeyInputs) -> str:
    """Digest identifying one per-state single-year dataset artifact."""
    return canonical_digest(
        {
            "schema": STATE_DATASET_KEY_SCHEMA,
            "spm": inputs.spm,
            "country": inputs.country.lower(),
            "dataset": inputs.dataset,
            "dataset_uri": inputs.dataset_uri,
            "source_sha256": inputs.source_sha256,
            "year": int(inputs.year),
            "state_code": inputs.state_code.upper(),
            "state_fips": int(inputs.state_fips),
            "filter_variable": inputs.filter_variable,
            "model_version": inputs.model_version,
            "policyengine_version": inputs.policyengine_version,
        }
    )


@dataclass(frozen=True)
class BaselineKeyInputs:
    """Every input that can change one precomputed baseline artifact's bytes.

    ``scope_key`` is the ``ScopingStrategy.cache_key`` string (None for an
    unscoped national run); ``region`` is the human-readable label
    ("national", "state/ca", ...). The simulation year rides in via
    ``dataset_digest``.
    """

    country: str
    region: str
    scope_key: str | None
    dataset_digest: str
    model_version: str
    policyengine_version: str
    spm: dict | None = None
    policy: str = CURRENT_LAW_POLICY


def baseline_key(inputs: BaselineKeyInputs) -> str:
    """Digest identifying one precomputed baseline simulation artifact."""
    return canonical_digest(
        {
            "schema": BASELINE_KEY_SCHEMA if inputs.spm is None else "bl2-spm",
            **({"spm": inputs.spm} if inputs.spm is not None else {}),
            "country": inputs.country.lower(),
            "region": inputs.region,
            "scope_key": inputs.scope_key,
            "dataset_key": inputs.dataset_digest,
            "policy": inputs.policy,
            "model_version": inputs.model_version,
            "policyengine_version": inputs.policyengine_version,
        }
    )


def baseline_simulation_id(baseline_digest: str) -> str:
    """Return the deterministic ``Simulation.id`` for a baseline artifact.

    The id doubles as the artifact filename stem (``{id}.h5`` beside the
    dataset — the path ``Simulation.ensure()`` already loads from), so it
    must be filename-safe and must not collide with the dataset filename
    pattern ``{stem}_year_{year}.h5``.
    """
    return f"{BASELINE_KEY_SCHEMA}-{baseline_digest[:_ID_DIGEST_CHARS]}"


# --- Store layout ---------------------------------------------------------
#
# datasets/<country>/<digest>/<stem>_year_<year>.h5
# state-datasets/<country>/<digest>/<stem>_year_<year>.h5
# baselines/<country>/<digest>/<id>.h5
# manifests/<digest>.json
# deployed/<environment>.json
#
# The filename inside a dataset object's path is EXACTLY the name the
# runtime's ensure_datasets existence check resolves — the fetch layer
# downloads objects to POLICYENGINE_DATA_FOLDER under their basenames, so
# the object basename IS the runtime filename contract.


def dataset_artifact_filename(stem: str, year: int) -> str:
    """Return the single-year dataset basename the runtime's existence check expects."""
    return f"{stem}_year_{year}.h5"


def baseline_artifact_filename(simulation_id: str) -> str:
    """Return the ``{id}.h5`` basename ``Simulation.ensure()`` loads a baseline from."""
    return f"{simulation_id}.h5"


def dataset_artifact_path(
    country: str, dataset_digest: str, stem: str, year: int
) -> str:
    """Return the store key ``datasets/<country>/<digest>/<stem>_year_<year>.h5``."""
    return (
        f"datasets/{country.lower()}/{dataset_digest}/"
        f"{dataset_artifact_filename(stem, year)}"
    )


def state_dataset_artifact_path(
    country: str, state_dataset_digest: str, stem: str, year: int
) -> str:
    """Return the store key ``state-datasets/<country>/<digest>/<stem>_year_<year>.h5``.

    Every state's file has the same basename, the one the runtime's
    ``ensure_datasets`` resolves; the image keeps them apart by folder.
    """
    return (
        f"state-datasets/{country.lower()}/{state_dataset_digest}/"
        f"{dataset_artifact_filename(stem, year)}"
    )


def baseline_artifact_path(
    country: str, baseline_digest: str, simulation_id: str
) -> str:
    """Return the store key ``baselines/<country>/<digest>/<id>.h5``."""
    return (
        f"baselines/{country.lower()}/{baseline_digest}/"
        f"{baseline_artifact_filename(simulation_id)}"
    )


def manifest_path(manifest_digest: str) -> str:
    """Return the store key of a deploy manifest, addressed by its own digest."""
    return f"manifests/{manifest_digest}.json"


def deployed_marker_path(environment: str) -> str:
    """Return the key of the marker naming the manifest deployed to ``environment``."""
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
    source_sha256: str | None
    data_build_fingerprint: str | None
    model_version: str
    policyengine_version: str
    spm: dict | None = None

    @property
    def digest(self) -> str:
        """Content key over every input that can change this dataset's bytes."""
        return dataset_key(
            DatasetKeyInputs(
                country=self.country,
                spm=self.spm,
                dataset=self.dataset,
                year=self.year,
                data_version=self.data_version,
                data_artifact_revision=self.data_artifact_revision,
                source_sha256=self.source_sha256,
                data_build_fingerprint=self.data_build_fingerprint,
                model_version=self.model_version,
                policyengine_version=self.policyengine_version,
            )
        )

    @property
    def filename(self) -> str:
        """Basename the runtime resolves under ``POLICYENGINE_DATA_FOLDER``."""
        return dataset_artifact_filename(self.stem, self.year)

    @property
    def store_path(self) -> str:
        """Store key of this dataset artifact."""
        return dataset_artifact_path(self.country, self.digest, self.stem, self.year)


@dataclass(frozen=True)
class StateDatasetArtifactIdentity:
    """Everything needed to key and name one per-state single-year dataset."""

    country: str
    dataset: str
    dataset_uri: str
    source_sha256: str | None
    stem: str
    year: int
    state_code: str
    state_fips: int
    filter_variable: str
    model_version: str
    policyengine_version: str
    spm: dict | None = None

    @property
    def digest(self) -> str:
        """Content key over every input that can change this file's bytes."""
        return state_dataset_key(
            StateDatasetKeyInputs(
                country=self.country,
                dataset=self.dataset,
                dataset_uri=self.dataset_uri,
                source_sha256=self.source_sha256,
                year=self.year,
                state_code=self.state_code,
                state_fips=self.state_fips,
                filter_variable=self.filter_variable,
                model_version=self.model_version,
                policyengine_version=self.policyengine_version,
                spm=self.spm,
            )
        )

    @property
    def filename(self) -> str:
        """Basename the runtime resolves inside the state's folder."""
        return dataset_artifact_filename(self.stem, self.year)

    @property
    def store_path(self) -> str:
        """Store key of this per-state dataset artifact."""
        return state_dataset_artifact_path(
            self.country, self.digest, self.stem, self.year
        )


@dataclass(frozen=True)
class BaselineArtifactIdentity:
    """Everything needed to key, id, and name one baseline artifact."""

    country: str
    region: str
    scope_key: str | None
    dataset: DatasetArtifactIdentity
    spm: dict | None = None

    @property
    def digest(self) -> str:
        """Content key; nests the dataset digest, so a dataset change rotates it."""
        return baseline_key(
            BaselineKeyInputs(
                country=self.country,
                region=self.region,
                spm=self.spm,
                scope_key=self.scope_key,
                dataset_digest=self.dataset.digest,
                model_version=self.dataset.model_version,
                policyengine_version=self.dataset.policyengine_version,
            )
        )

    @property
    def simulation_id(self) -> str:
        """``Simulation.id`` the runtime assigns to this baseline."""
        return baseline_simulation_id(self.digest)

    @property
    def storage_id(self) -> str:
        """Artifact filename stem: the simulation id, plus the SPM digest when set.

        The suffix keeps SPM and non-SPM baselines that share a simulation id
        from overwriting each other.
        """
        if self.spm is None:
            return self.simulation_id
        return f"{self.simulation_id}-spm-{canonical_digest(self.spm)}"

    @property
    def store_path(self) -> str:
        """Store key of this baseline artifact, named by ``storage_id``."""
        return baseline_artifact_path(self.country, self.digest, self.storage_id)


def _receipt_source_sha256(country: str, data_version: str) -> str | None:
    """Content hash of the installed multi-year source, when certifiable.

    Only trusted when the receipt entry's version matches the bundle's data
    version (mirroring ``resolve_local_bundle_dataset_path``) — a receipt
    left over from a different install must not leak into the key.
    """
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # identity seam: resolved per call so tests can patch release_bundle._receipt_dataset
        _receipt_dataset,
    )

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


def _certification_fingerprint(country: str) -> str | None:
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    certification = getattr(get_release_manifest(country), "certification", None)
    fingerprint = getattr(certification, "data_build_fingerprint", None)
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def collect_dataset_identity(country: str, year: int) -> DatasetArtifactIdentity:
    """Identity of the default single-year dataset for this installed bundle.

    Reads the same sources the runtime trusts: the release bundle (versions),
    the bundle receipt (content sha), and the manifest certification
    (fingerprint). The stem comes through the same manifest helpers the
    runtime lookup uses, so filename coupling is inherited, not
    re-derived.
    """
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # identity seam: resolved per call so tests can patch release_bundle
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle(country)
    stem = dataset_logical_name(
        resolve_dataset_reference(bundle.country, bundle.default_dataset)
    )
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    selection = normalize_runtime_spm({"country": country, "time_period": year})
    return DatasetArtifactIdentity(
        spm=selection,
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


def _release_dataset_sha256(country: str, dataset: str) -> str | None:
    """Certified SHA-256 of a bundle dataset, from the .py release manifest."""
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    reference = getattr(get_release_manifest(country), "datasets", {}).get(dataset)
    digest = getattr(reference, "sha256", None)
    return digest.lower() if isinstance(digest, str) and digest else None


def collect_state_dataset_identity(  # noqa: PLR0913  # one keyword per input of the per-state key
    country: str,
    year: int,
    *,
    dataset: str,
    state_code: str,
    state_fips: int,
    filter_variable: str,
) -> StateDatasetArtifactIdentity:
    """Identity of one state's year file filtered from a bundle dataset.

    ``dataset`` names a non-default bundle dataset, such as the regional
    dataset state regions load. Versions and the revision-pinned URI come from
    the release bundle, the source hash from the .py release manifest, and the
    stem through the same manifest helpers the runtime lookup uses.
    """
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # identity seam: resolved per call so tests can patch release_bundle
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    bundle = get_country_release_bundle(country)
    dataset_uri = bundle.dataset_uris.get(dataset)
    if dataset_uri is None:
        msg = f"Dataset {dataset!r} is not in the {country} release bundle"
        raise ValueError(msg)
    return StateDatasetArtifactIdentity(
        country=bundle.country,
        dataset=dataset,
        dataset_uri=dataset_uri,
        source_sha256=_release_dataset_sha256(bundle.country, dataset),
        stem=dataset_logical_name(resolve_dataset_reference(bundle.country, dataset)),
        year=int(year),
        state_code=state_code.upper(),
        state_fips=int(state_fips),
        filter_variable=filter_variable,
        model_version=bundle.model_version,
        policyengine_version=bundle.policyengine_version,
        spm=normalize_runtime_spm({"country": country, "time_period": year}),
    )


def collect_baseline_identity(
    country: str,
    year: int,
    *,
    region: str,
    scope_key: str | None,
    spm: dict | None = None,
) -> BaselineArtifactIdentity:
    """Return the identity of this bundle's baseline for one region and year.

    ``spm`` is normalized against the installed image's capability first, so
    a request that omits it still keys to the image's default SPM selection.
    """
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    selection = normalize_runtime_spm(
        {"country": country, "time_period": year, "spm": spm}
    )
    return BaselineArtifactIdentity(
        spm=selection,
        country=country.lower(),
        region=region,
        scope_key=scope_key,
        dataset=collect_dataset_identity(country, year),
    )
