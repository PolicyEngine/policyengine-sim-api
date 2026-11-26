"""Content-addressed identity of the run_simulation app's prebuilt files.

The precompute stores three kinds of file: year files (a dataset's single-year
file, or one subset's rows of it), current-law baselines computed on a year
file, and source files copied as they are. Each file's key is a SHA-256 digest
over every input that determines its bytes, so "is it stored?" and "is it
stale?" are the same question, answered by key equality: the same key means a
byte-identical file, and any input change rotates the key, so the old object
simply stops being referenced.

There is one identity per kind for every dataset level: ``yf1`` year files,
``bl3`` baselines and ``sf1`` source files. A ``None`` input is written as an
explicit null, so presence and absence are distinct keys. The baseline key
nests its year file's digest, so anything that rotates a year file rotates
every baseline on it. Bump a schema label when its payload or the file
format changes.

The level handlers (``stage12_cache.levels``) say which files to build;
this module turns their targets into keys and store paths. The precompute
writer, the runtime reader and the download step all take keys and paths from
here, so a drift between writer and reader is a review diff here, not a
silent miss in production.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_executor.stage12_cache.levels import (
        BaselineScope,
        SourceFileTarget,
        YearFileTarget,
    )

YEAR_FILE_KEY_SCHEMA = "yf1"
BASELINE_KEY_SCHEMA = "bl3"
SOURCE_FILE_KEY_SCHEMA = "sf1"

# Length of the digest prefix in simulation IDs: 16 hex characters (64 bits)
# is far beyond collision range for this population while keeping file names
# readable in logs.
_ID_DIGEST_CHARS = 16

CURRENT_LAW_POLICY = "current-law"
NATIONAL_REGION = "national"


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the canonical JSON encoding of ``payload``.

    Canonical means: keys sorted, no whitespace, ASCII-only escapes. Any
    nested structure must itself be JSON-serializable with deterministic
    content (no sets, no floats that vary in repr).
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- Store layout ---------------------------------------------------------
#
# year-files/<country>/<digest>/<stem>_year_<year>.h5
# baselines/<country>/<digest>/<storage id>.h5
# sources/<country>/<sha256>/<file name>
# manifests/<digest>.json
# deployed/<environment>.json
#
# A file's basename is the name the runtime resolves: the download step puts
# each object under its basename in its prebuilt folder.


def year_file_artifact_path(country: str, digest: str, filename: str) -> str:
    """Return the store key ``year-files/<country>/<digest>/<filename>``."""
    return f"year-files/{country.lower()}/{digest}/{filename}"


def baseline_artifact_path(country: str, digest: str, storage_id: str) -> str:
    """Return the store key ``baselines/<country>/<digest>/<storage id>.h5``."""
    return f"baselines/{country.lower()}/{digest}/{storage_id}.h5"


def source_file_artifact_path(country: str, sha256: str, filename: str) -> str:
    """Return the store key ``sources/<country>/<sha256>/<filename>``."""
    return f"sources/{country.lower()}/{sha256}/{filename}"


def manifest_path(manifest_digest: str) -> str:
    """Return the store key of a deploy manifest, addressed by its own digest."""
    return f"manifests/{manifest_digest}.json"


def deployed_marker_path(environment: str) -> str:
    """Return the key of the marker naming the manifest deployed to ``environment``."""
    return f"deployed/{environment}.json"


# --- Identities -----------------------------------------------------------


@dataclass(frozen=True)
class YearFileIdentity:
    """Everything that can change one year file's bytes, and where it is kept.

    ``subset`` names the rows a per-subset file keeps (``region``,
    ``variable``, ``value``), or is None for a whole dataset. ``packages`` maps
    each installed package that builds the file to its version. ``stem`` and
    ``folder`` name the file and its image folder; they follow from the
    dataset and subset, so they are not in the key.
    """

    country: str
    dataset: str
    dataset_sha256: str | None
    data_version: str
    data_artifact_revision: str
    certification_fingerprint: str | None
    year: int
    subset: Mapping[str, Any] | None = field(hash=False)
    packages: Mapping[str, str] = field(hash=False)
    spm: Mapping[str, Any] | None = field(hash=False)
    stem: str
    folder: str

    def payload(self) -> dict[str, Any]:
        """Return the ``yf1`` payload the digest is computed from."""
        return {
            "schema": YEAR_FILE_KEY_SCHEMA,
            "country": self.country.lower(),
            "dataset": self.dataset,
            "dataset_sha256": self.dataset_sha256,
            "data_version": self.data_version,
            "data_artifact_revision": self.data_artifact_revision,
            "certification_fingerprint": self.certification_fingerprint,
            "year": int(self.year),
            "subset": None if self.subset is None else dict(self.subset),
            "packages": dict(self.packages),
            "spm": None if self.spm is None else dict(self.spm),
        }

    @property
    def digest(self) -> str:
        """Content key over every input that can change this file's bytes."""
        return canonical_digest(self.payload())

    @property
    def filename(self) -> str:
        """Basename .py's ``ensure_datasets`` resolves in the file's folder."""
        return f"{self.stem}_year_{self.year}.h5"

    @property
    def store_path(self) -> str:
        """Store key of this year file."""
        return year_file_artifact_path(self.country, self.digest, self.filename)


@dataclass(frozen=True)
class BaselineIdentity:
    """Everything that can change one current-law baseline's bytes.

    ``scope_key`` is the scoping strategy's ``cache_key`` (None for a run of
    the whole dataset) and ``region`` its readable label (``national``,
    ``region_group/…``). ``spm`` is the request's normalised SPM selection.
    """

    year_file: YearFileIdentity
    region: str
    scope_key: str | None
    spm: Mapping[str, Any] | None = field(default=None, hash=False)
    policy: str = CURRENT_LAW_POLICY

    def payload(self) -> dict[str, Any]:
        """Return the ``bl3`` payload the digest is computed from."""
        return {
            "schema": BASELINE_KEY_SCHEMA,
            "year_file": self.year_file.digest,
            "region": self.region,
            "scope_key": self.scope_key,
            "policy": self.policy,
            "spm": None if self.spm is None else dict(self.spm),
        }

    @property
    def country(self) -> str:
        """The year file's country."""
        return self.year_file.country.lower()

    @property
    def digest(self) -> str:
        """Content key; nests the year file's digest."""
        return canonical_digest(self.payload())

    @property
    def simulation_id(self) -> str:
        """``Simulation.id`` the runtime assigns to this baseline."""
        return f"{BASELINE_KEY_SCHEMA}-{self.digest[:_ID_DIGEST_CHARS]}"

    @property
    def storage_id(self) -> str:
        """File stem .py's ``Simulation.storage_id`` gives: the ID, plus the SPM digest.

        The suffix keeps SPM and non-SPM baselines that share a simulation ID
        from overwriting each other.
        """
        if self.spm is None:
            return self.simulation_id
        return f"{self.simulation_id}-spm-{canonical_digest(self.spm)}"

    @property
    def store_path(self) -> str:
        """Store key of this baseline, named by ``storage_id``."""
        return baseline_artifact_path(self.country, self.digest, self.storage_id)


@dataclass(frozen=True)
class SourceFileIdentity:
    """A certified source dataset copied into the image byte for byte."""

    country: str
    dataset: str
    dataset_sha256: str
    filename: str
    folder: str

    def payload(self) -> dict[str, Any]:
        """Return the ``sf1`` payload the digest is computed from."""
        return {
            "schema": SOURCE_FILE_KEY_SCHEMA,
            "country": self.country.lower(),
            "dataset": self.dataset,
            "dataset_sha256": self.dataset_sha256,
        }

    @property
    def digest(self) -> str:
        """Content key of the copied file."""
        return canonical_digest(self.payload())

    @property
    def store_path(self) -> str:
        """Store key of the copy, addressed by the certified SHA-256."""
        return source_file_artifact_path(
            self.country, self.dataset_sha256, self.filename
        )


# --- Collecting identities in the container --------------------------------


def _installed_package_versions(country: str) -> dict[str, str]:
    """Return the installed versions of .py, policyengine-core and the country package.

    Read where the identity is collected, inside the image, so they are the
    versions that build the file.
    """
    names = ("policyengine", "policyengine-core", f"policyengine-{country.lower()}")
    return {name: version(name) for name in names}


def _release_dataset_sha256(country: str, dataset: str) -> str | None:
    """Return a bundle dataset's certified SHA-256 from the .py release manifest."""
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    reference = getattr(get_release_manifest(country), "datasets", {}).get(dataset)
    digest = getattr(reference, "sha256", None)
    return digest.lower() if isinstance(digest, str) and digest else None


def _certification_fingerprint(country: str) -> str | None:
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    certification = getattr(get_release_manifest(country), "certification", None)
    fingerprint = getattr(certification, "data_build_fingerprint", None)
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def _dataset_stem(country: str, dataset: str) -> str:
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    return dataset_logical_name(resolve_dataset_reference(country, dataset))


def year_file_identity(target: YearFileTarget) -> YearFileIdentity:
    """Return the ``yf1`` identity of a handler's year-file target.

    Versions come from the release bundle, the source's certified hash and
    the certification fingerprint from the .py release manifest, the package
    versions from the installed packages, and the SPM selection from the
    image's capability for the year.
    """
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # identity seam: resolved per call so tests can patch release_bundle
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    country = target.country.lower()
    bundle = get_country_release_bundle(country)
    subset = target.subset
    return YearFileIdentity(
        country=country,
        dataset=target.dataset,
        dataset_sha256=_release_dataset_sha256(country, target.dataset),
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        certification_fingerprint=_certification_fingerprint(country),
        year=int(target.year),
        subset=(
            None
            if subset is None
            else {
                "region": subset.region,
                "variable": subset.variable,
                "value": subset.value,
            }
        ),
        packages=_installed_package_versions(country),
        spm=normalize_runtime_spm({"country": country, "time_period": target.year}),
        stem=_dataset_stem(country, target.dataset),
        folder=target.folder,
    )


def baseline_identity(
    year_file: YearFileIdentity,
    scope: BaselineScope,
    spm: Mapping[str, Any] | None = None,
) -> BaselineIdentity:
    """Return the ``bl3`` identity of a current-law baseline on ``year_file``.

    ``spm`` is normalised against the image's capability first, so a request
    that omits it still keys to the image's default SPM selection.
    """
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    selection = normalize_runtime_spm(
        {
            "country": year_file.country,
            "time_period": year_file.year,
            "spm": None if spm is None else dict(spm),
        }
    )
    return BaselineIdentity(
        year_file=year_file,
        region=scope.region,
        scope_key=scope.scope_key,
        spm=selection,
    )


def source_file_identity(target: SourceFileTarget) -> SourceFileIdentity:
    """Return the ``sf1`` identity of a source dataset copied into the image.

    Raises ``ValueError`` when the release manifest certifies no SHA-256 for
    it: a copy is addressed by that hash.
    """
    from policyengine.provenance.manifest import (
        resolve_dataset_reference,
    )

    country = target.country.lower()
    sha256 = _release_dataset_sha256(country, target.dataset)
    if sha256 is None:
        msg = (
            f"The {country} release manifest has no certified SHA-256 for "
            f"{target.dataset}"
        )
        raise ValueError(msg)
    reference = resolve_dataset_reference(country, target.dataset)
    filename = reference.split("@", 1)[0].rsplit("/", 1)[-1]
    return SourceFileIdentity(
        country=country,
        dataset=target.dataset,
        dataset_sha256=sha256,
        filename=filename,
        folder=target.folder,
    )
