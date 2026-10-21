"""Strict, separately stored Stage 12 v2 worker version manifest."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Protocol

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    Sha256Digest,
    Stage12BundleManifest,
)

V1_ROUTING_STATE_NAME = "simulation-api-routing-state"
V2_VERSION_MANIFEST_NAME = "simulation-api-v2-version-manifest"
# The complete manifest, including each country bundle's region datasets.
REGION_DATASETS_MANIFEST_KEY = "active-with-region-datasets"
# Readers released before region datasets forbid unknown fields, so they keep
# reading this key, which holds the same manifest without them.
ACTIVE_MANIFEST_KEY = "active-with-cache"
# Readers released before Stage 12 cache releases forbid unknown fields, so
# they keep reading this key, which holds the same manifest without them or
# region datasets.
PRE_CACHE_MANIFEST_KEY = "active"
V2_APPLICATION_PREFIX = "policyengine-simulation-v2-py"
V1_APPLICATION_PREFIX = "policyengine-simulation-py"
_MAX_MODAL_APPLICATION_NAME_LENGTH = 64
_CALLABLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")

ManifestText = Annotated[str, Field(min_length=1, max_length=255)]
CallableName = Annotated[str, Field(pattern=_CALLABLE_PATTERN.pattern)]


class StrictManifestModel(BaseModel):
    """Base for v2 version manifest models; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class V2CountryWorker(StrictManifestModel):
    """A country served by a v2 worker, and the callable that runs its simulations."""

    country: CountryId
    single_simulation_callable: CallableName


class Stage12CacheRelease(StrictManifestModel):
    """The US cache a worker image holds: its manifest digest and coverage.

    The cache covers 3 datasets and 60 baselines, one per region-group segment
    and year.
    """

    manifest_sha256: Sha256Digest
    partition_sha256: Sha256Digest
    years: tuple[int, ...]
    segment_count: Literal[20] = 20
    dataset_count: Literal[3] = 3
    baseline_count: Literal[60] = 60


class V2WorkerValidation(StrictManifestModel):
    """Receipt for a successful validation of a deployed v2 worker application.

    Names the application and bundle digest that were validated, when, and the
    validation invocation IDs overall and for each country.
    """

    validated: Literal[True]
    application_name: ManifestText
    bundle_manifest_sha256: Sha256Digest
    validated_at: datetime
    validation_invocation_id: ManifestText
    country_validation_invocation_ids: dict[CountryId, ManifestText]
    cache: Stage12CacheRelease | None = None


class V2WorkerVersion(StrictManifestModel):
    """One deployed v2 worker application and the bundle it runs.

    Lists its report coordinator callable, its per-country workers and its
    validation receipt; the countries must match the bundle's, in order.
    """

    application_name: ManifestText
    report_coordinator_callable: CallableName
    countries: tuple[V2CountryWorker, ...]
    bundle: Stage12BundleManifest
    bundle_manifest_sha256: Sha256Digest
    validation: V2WorkerValidation
    cache: Stage12CacheRelease | None = None

    @model_validator(mode="after")
    def validate_worker(self) -> V2WorkerVersion:
        """Check the worker's name, validation receipt and countries match its bundle.

        The application name must be the v2 name derived from the bundle's
        PolicyEngine.py version, the validation must name this application and
        bundle digest, and the countries must equal the bundle's, in order,
        each with a validation invocation.
        """
        if not self.application_name.startswith(V2_APPLICATION_PREFIX):
            msg = "v2 application uses an invalid name prefix"
            raise ValueError(msg)
        if self.application_name.startswith(V1_APPLICATION_PREFIX):
            msg = "v2 application name collides with the v1 prefix"
            raise ValueError(msg)
        expected_name = v2_application_name(self.bundle.policyengine_version)
        if self.application_name != expected_name:
            msg = "v2 application name does not match its bundle version"
            raise ValueError(msg)
        if self.validation.application_name != self.application_name:
            msg = "worker validation names another application"
            raise ValueError(msg)
        if self.validation.bundle_manifest_sha256 != self.bundle_manifest_sha256:
            msg = "worker validation names another bundle digest"
            raise ValueError(msg)
        if self.validation.cache != self.cache:
            msg = "worker cache metadata differs from worker validation"
            raise ValueError(msg)
        countries = tuple(worker.country for worker in self.countries)
        bundle_countries = tuple(country.country for country in self.bundle.countries)
        if len(countries) != len(set(countries)):
            msg = "v2 worker countries must be unique"
            raise ValueError(msg)
        if countries != bundle_countries:
            msg = "v2 worker countries do not match the normalized bundle"
            raise ValueError(msg)
        if set(self.validation.country_validation_invocation_ids) != set(countries):
            msg = "worker validation does not cover every country"
            raise ValueError(msg)
        return self


class V2VersionManifest(StrictManifestModel):
    """Registry of deployed v2 worker versions, keyed by PolicyEngine.py version.

    ``default_version`` is the version used when a request does not pin one, and
    ``generation`` identifies the publish that produced this manifest.
    """

    schema_version: Literal[1] = 1
    generation: ManifestText
    default_version: ManifestText
    versions: dict[str, V2WorkerVersion]

    @model_validator(mode="after")
    def validate_versions(self) -> V2VersionManifest:
        """Check the default has an entry and each key is its bundle's version."""
        if not self.versions:
            msg = "v2 manifest must contain a worker version"
            raise ValueError(msg)
        if self.default_version not in self.versions:
            msg = "v2 default version has no worker entry"
            raise ValueError(msg)
        for version, worker in self.versions.items():
            if version != worker.bundle.policyengine_version:
                msg = "v2 manifest version key does not match its bundle"
                raise ValueError(msg)
        return self


class V2ManifestStore(Protocol):
    """The part of a Modal Dict's mapping interface the manifest helpers use."""

    def get(self, key: str, default: object = None) -> object:
        """Return the value stored under ``key``, or ``default`` when absent."""
        ...

    def __setitem__(self, key: str, value: object) -> None:
        """Store ``value`` under ``key``, replacing any previous value."""
        ...


def v2_application_name(policyengine_version: str) -> str:
    """Derive the v2 Modal application name from a PolicyEngine.py version.

    Dots and plus signs become hyphens. Raises ValueError for a version with
    characters a Modal name cannot hold, or a name over 64 characters.
    """
    if not policyengine_version or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", policyengine_version
    ):
        msg = "PolicyEngine.py version cannot form a Modal application name"
        raise ValueError(msg)
    name = V2_APPLICATION_PREFIX + policyengine_version.replace(".", "-").replace(
        "+", "-"
    )
    if len(name) > _MAX_MODAL_APPLICATION_NAME_LENGTH:
        msg = (
            "v2 Modal application name exceeds "
            f"{_MAX_MODAL_APPLICATION_NAME_LENGTH} characters"
        )
        raise ValueError(msg)
    return name


def manifest_digest(manifest: V2VersionManifest) -> str:
    """Return the SHA-256 of the manifest's canonical JSON (sorted, compact)."""
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return sha256(payload).hexdigest()


def _is_newer(candidate: str, current: str | None) -> bool:
    if current is None:
        return True
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


_REGION_DATASETS_FIELD = {
    "bundle": {"countries": {"__all__": {"region_datasets"}}},
}


def pre_region_datasets_manifest_payload(
    manifest: V2VersionManifest,
) -> dict[str, object]:
    """Return the manifest as readers released before region datasets accept it."""
    return manifest.model_dump(
        mode="json",
        exclude={"versions": {"__all__": _REGION_DATASETS_FIELD}},
    )


def pre_cache_manifest_payload(manifest: V2VersionManifest) -> dict[str, object]:
    """Return the manifest as readers released before cache releases accept it."""
    return manifest.model_dump(
        mode="json",
        exclude={
            "versions": {
                "__all__": {
                    "cache": True,
                    "validation": {"cache"},
                    **_REGION_DATASETS_FIELD,
                }
            }
        },
    )


def _is_current_region_dataset_copy(value: object, cache_copy: object) -> bool:
    """Return whether ``value`` is the copy the last publish wrote with ``cache_copy``.

    Code released before region datasets publishes only the older keys, for
    example after a rollback, which leaves an out-of-date region dataset copy
    behind. A current copy, with its region datasets removed, is exactly the
    manifest under ``active-with-cache``.
    """
    if value is None:
        return False
    if cache_copy is None:
        return True
    try:
        manifest = V2VersionManifest.model_validate(value)
    except (TypeError, ValueError):
        return False
    return pre_region_datasets_manifest_payload(manifest) == cache_copy


def _current_manifest(store: V2ManifestStore) -> object:
    # The newest key the last publish wrote: each older key holds the same
    # manifest without the fields its readers do not accept.
    cache_copy = store.get(ACTIVE_MANIFEST_KEY)
    region_dataset_copy = store.get(REGION_DATASETS_MANIFEST_KEY)
    if _is_current_region_dataset_copy(region_dataset_copy, cache_copy):
        return region_dataset_copy
    if cache_copy is not None:
        return cache_copy
    # Until the first publish that writes the cache copy.
    return store.get(PRE_CACHE_MANIFEST_KEY)


def build_next_v2_manifest(
    *,
    current: object,
    worker: V2WorkerVersion,
    force_default: bool = False,
) -> V2VersionManifest:
    """Return ``current`` with ``worker`` added under its bundle version.

    ``current`` is the raw stored manifest, or None before the first publish.
    The default moves to the new version only when it is newer by PEP 440
    ordering (an unparsable version never is) or ``force_default`` is set.
    """
    if current is None:
        versions: dict[str, V2WorkerVersion] = {}
        previous_default = None
    else:
        parsed = V2VersionManifest.model_validate(current)
        versions = deepcopy(parsed.versions)
        previous_default = parsed.default_version
    version = worker.bundle.policyengine_version
    versions[version] = worker
    default_version = (
        version
        if force_default or _is_newer(version, previous_default)
        else previous_default
    )
    if default_version is None:  # pragma: no cover - first entry always advances
        default_version = version
    return V2VersionManifest(
        generation=f"{version}:{worker.application_name}",
        default_version=default_version,
        versions=versions,
    )


def publish_v2_manifest(
    *,
    store: V2ManifestStore,
    worker: V2WorkerVersion,
    force_default: bool = False,
) -> V2VersionManifest:
    """Add ``worker`` to the stored manifest, write it back and return it.

    The whole manifest is written as one store value, so readers never see a
    partial update.
    """
    next_manifest = build_next_v2_manifest(
        current=_current_manifest(store),
        worker=worker,
        force_default=force_default,
    )
    # Each key holds one entire validated document, and a reader reads only
    # one key, so no reader observes a partial update assembled from several.
    store[REGION_DATASETS_MANIFEST_KEY] = next_manifest.model_dump(mode="json")
    store[ACTIVE_MANIFEST_KEY] = pre_region_datasets_manifest_payload(next_manifest)
    store[PRE_CACHE_MANIFEST_KEY] = pre_cache_manifest_payload(next_manifest)
    return next_manifest


class V2ManifestLoader:
    """Load v2 state while retaining only this loader's last valid v2 copy."""

    def __init__(self, store: V2ManifestStore) -> None:
        """Wrap ``store``; nothing is read until the first ``load``."""
        self._store = store
        self._last_valid: V2VersionManifest | None = None

    def load(self) -> V2VersionManifest:
        """Return the stored manifest, or this loader's last valid copy.

        The fallback covers a missing or invalid stored value; with no earlier
        valid copy, ValueError is raised instead.
        """
        value = _current_manifest(self._store)
        try:
            manifest = V2VersionManifest.model_validate(value)
        except (TypeError, ValueError):
            if self._last_valid is None:
                msg = "no valid v2 version manifest is available"
                raise ValueError(msg) from None
            return self._last_valid
        self._last_valid = manifest
        return manifest

    def resolve(self, version: str | None = None) -> tuple[str, V2WorkerVersion, str]:
        """Return ``(version, worker, manifest digest)`` for ``version`` or the default.

        Raises ValueError when the manifest has no worker for that version.
        """
        manifest = self.load()
        selected = version or manifest.default_version
        try:
            worker = manifest.versions[selected]
        except KeyError:
            msg = f"v2 worker version {selected!r} is not available"
            raise ValueError(msg) from None
        return selected, worker, manifest_digest(manifest)


def assert_separate_manifest_names(*, v1_name: str, v2_name: str) -> None:
    """Raise ValueError unless both names are set and differ.

    This keeps a v2 manifest publish from overwriting the v1 routing state.
    """
    if not v1_name or not v2_name:
        msg = "v1 and v2 manifest names must be non-empty"
        raise ValueError(msg)
    if v1_name == v2_name:
        msg = "v1 and v2 manifests must use different storage names"
        raise ValueError(msg)


def validate_manifest_has_no_credentials(value: Mapping[str, object]) -> None:
    """Raise ValueError if any key, at any depth, looks like a credential.

    Keys containing password, secret, token, credential or private_key match
    case-insensitively; the error names the dotted path to the key.
    """
    prohibited = {"password", "secret", "token", "credential", "private_key"}

    def visit(item: object, path: tuple[str, ...]) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).lower()
                if any(part in key_text for part in prohibited):
                    raise ValueError(
                        "v2 manifest contains a credential-like field at "
                        + ".".join((*path, str(key)))
                    )
                visit(child, (*path, str(key)))
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, (*path, str(index)))

    visit(value, ())
