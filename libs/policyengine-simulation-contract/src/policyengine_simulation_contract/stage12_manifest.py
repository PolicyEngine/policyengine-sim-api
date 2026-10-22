"""Strict, separately stored Stage 12 v2 worker version manifest.

Schema version 2 records, for each country a worker application serves,
whether it can run economy reports and which function calculates its
households. It is stored under its own key. Whenever schema version 1 can
describe the same manifest, it is also written in that form, without those
fields, under the keys that readers released before schema version 2 read:
with region datasets, without them, and without them or cache releases.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    Sha256Digest,
    Stage12BundleManifest,
)

if TYPE_CHECKING:
    from pydantic.main import IncEx

V1_ROUTING_STATE_NAME = "simulation-api-routing-state"
V2_VERSION_MANIFEST_NAME = "simulation-api-v2-version-manifest"
# The schema version 2 manifest. Readers released with schema version 2 read
# this key first.
SCHEMA_2_MANIFEST_KEY = "active-schema-2"
# Readers released before schema version 2 forbid unknown fields, so they read
# the keys below, which hold the same manifest in schema version 1 form. This
# one keeps each country bundle's region datasets.
REGION_DATASETS_MANIFEST_KEY = "active-with-region-datasets"
# Readers released before region datasets forbid unknown fields, so they keep
# reading this key, which holds the schema version 1 form without them.
ACTIVE_MANIFEST_KEY = "active-with-cache"
# Readers released before Stage 12 cache releases forbid unknown fields, so
# they keep reading this key, which holds the schema version 1 form without
# the cache fields or region datasets.
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
    """A country served by a v2 worker application, and what it can calculate.

    An economy-capable country names the callable that runs one of its report
    simulations; only such a country's reports are given to the report
    coordinator. ``household_callable`` names the function that calculates
    its households, and is None when the application has none or it was not
    validated.
    """

    country: CountryId
    economy_capable: bool
    single_simulation_callable: CallableName | None = None
    household_callable: CallableName | None = None

    @model_validator(mode="after")
    def validate_capabilities(self) -> V2CountryWorker:
        """Require a simulation callable exactly when the country is economy-capable.

        A country must also have at least one capability.
        """
        if self.economy_capable != (self.single_simulation_callable is not None):
            msg = (
                "an economy-capable country, and only one, names a simulation callable"
            )
            raise ValueError(msg)
        if not self.economy_capable and self.household_callable is None:
            msg = "a country needs at least one capability"
            raise ValueError(msg)
        return self


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
    validation invocation IDs: overall, for each economy-capable country's
    simulation worker, and for each country's household function.
    """

    validated: Literal[True]
    application_name: ManifestText
    bundle_manifest_sha256: Sha256Digest
    validated_at: datetime
    validation_invocation_id: ManifestText
    country_validation_invocation_ids: dict[CountryId, ManifestText]
    household_validation_invocation_ids: dict[CountryId, ManifestText] = Field(
        default_factory=dict
    )
    cache: Stage12CacheRelease | None = None


def _require_function_receipts(
    countries: tuple[V2CountryWorker, ...], validation: V2WorkerValidation
) -> None:
    """Require a validation invocation for exactly the functions countries name."""
    economy_countries = {
        worker.country for worker in countries if worker.economy_capable
    }
    if set(validation.country_validation_invocation_ids) != economy_countries:
        msg = "worker validation does not cover exactly the economy-capable countries"
        raise ValueError(msg)
    household_countries = {
        worker.country for worker in countries if worker.household_callable is not None
    }
    if set(validation.household_validation_invocation_ids) != household_countries:
        msg = "worker validation does not cover exactly the household functions"
        raise ValueError(msg)


class V2WorkerVersion(StrictManifestModel):
    """One deployed v2 worker application and the bundle it runs.

    Lists its report coordinator callable, its per-country workers and its
    validation receipt; the countries must match the bundle's, in order, and
    every function a country names must have been validated.
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
        each named function with a validation invocation.
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
        _require_function_receipts(self.countries, self.validation)
        return self

    def economy_worker(self, country: str) -> V2CountryWorker | None:
        """Return ``country``'s worker when it can run economy reports."""
        return next(
            (
                worker
                for worker in self.countries
                if worker.country == country and worker.economy_capable
            ),
            None,
        )

    def household_worker(self, country: str) -> V2CountryWorker | None:
        """Return ``country``'s worker when it has a household function."""
        return next(
            (
                worker
                for worker in self.countries
                if worker.country == country and worker.household_callable is not None
            ),
            None,
        )


class V2VersionManifest(StrictManifestModel):
    """Registry of deployed v2 worker versions, keyed by PolicyEngine.py version.

    ``default_version`` is the version used when a request does not pin one, and
    ``generation`` identifies the publish that produced this manifest.
    """

    schema_version: Literal[2] = 2
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


def schema_1_manifest_payload(
    manifest: V2VersionManifest,
    *,
    include_cache: bool = True,
    include_region_datasets: bool = True,
) -> dict[str, object] | None:
    """Return the manifest as readers released before schema version 2 accept it.

    Without ``include_region_datasets`` each country bundle's region datasets
    are left out, for readers released before them; without ``include_cache``
    the cache fields are left out too, for readers released before cache
    releases. Returns None when a country is not economy-capable, which
    schema version 1 cannot describe.
    """
    if any(
        not country.economy_capable
        for worker in manifest.versions.values()
        for country in worker.countries
    ):
        return None
    validation_exclude = {"household_validation_invocation_ids"}
    worker_exclude: dict[str, IncEx | bool] = {
        "countries": {"__all__": {"economy_capable", "household_callable"}},
        "validation": validation_exclude,
    }
    if not include_region_datasets:
        worker_exclude["bundle"] = {"countries": {"__all__": {"region_datasets"}}}
    if not include_cache:
        worker_exclude["cache"] = True
        validation_exclude.add("cache")
    payload = manifest.model_dump(
        mode="json",
        exclude={"versions": {"__all__": worker_exclude}},
    )
    payload["schema_version"] = 1
    return payload


def pre_region_datasets_manifest_payload(
    manifest: V2VersionManifest,
) -> dict[str, object] | None:
    """Return the manifest as readers released before region datasets accept it."""
    return schema_1_manifest_payload(manifest, include_region_datasets=False)


def pre_cache_manifest_payload(manifest: V2VersionManifest) -> dict[str, object] | None:
    """Return the manifest as readers released before cache releases accept it."""
    return schema_1_manifest_payload(
        manifest, include_cache=False, include_region_datasets=False
    )


def _schema_1_copies(
    manifest: V2VersionManifest,
) -> dict[str, dict[str, object]] | None:
    """Return the schema version 1 documents a publish writes, by key.

    Returns None when schema version 1 cannot describe the manifest.
    """
    complete = schema_1_manifest_payload(manifest)
    without_region_datasets = pre_region_datasets_manifest_payload(manifest)
    without_cache = pre_cache_manifest_payload(manifest)
    if complete is None or without_region_datasets is None or without_cache is None:
        return None
    return {
        REGION_DATASETS_MANIFEST_KEY: complete,
        ACTIVE_MANIFEST_KEY: without_region_datasets,
        PRE_CACHE_MANIFEST_KEY: without_cache,
    }


def _upgraded_schema_1_document(document: Mapping[str, object]) -> dict[str, object]:
    """Return a schema version 1 document in schema version 2 form.

    Schema version 1 listed only countries whose report simulations a worker
    runs, and no household functions, so every country is economy-capable.
    """
    upgraded = deepcopy(dict(document))
    upgraded["schema_version"] = 2
    versions = upgraded.get("versions")
    if isinstance(versions, dict):
        for worker in versions.values():
            countries = worker.get("countries") if isinstance(worker, dict) else None
            if isinstance(countries, list):
                for country in countries:
                    if isinstance(country, dict):
                        country.setdefault("economy_capable", True)
    return upgraded


def parse_v2_manifest(value: object) -> V2VersionManifest:
    """Validate a stored manifest, reading a schema version 1 document as version 2.

    Raises ValueError (as a pydantic ValidationError) for an invalid document.
    """
    if isinstance(value, Mapping) and value.get("schema_version") == 1:
        value = _upgraded_schema_1_document(value)
    return V2VersionManifest.model_validate(value)


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
        manifest = parse_v2_manifest(value)
    except (TypeError, ValueError):
        return False
    return pre_region_datasets_manifest_payload(manifest) == cache_copy


def _current_schema_1_manifest(store: V2ManifestStore) -> object:
    """Return the schema version 1 document the last publish wrote."""
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


def _newest_validation(manifest: V2VersionManifest) -> datetime:
    return max(worker.validation.validated_at for worker in manifest.versions.values())


def _is_current_schema_2_document(store: V2ManifestStore, document: object) -> bool:
    """Return whether ``document``, under the schema version 2 key, is the latest.

    Code released before schema version 2 publishes only the older keys, for
    example after a rollback, which leaves an out-of-date schema version 2
    document behind. Whenever schema version 1 can describe a manifest, its
    publish writes the schema version 1 forms to the older keys, so the
    document is current while each older key holds its form. Otherwise (a
    country that is not economy-capable) the publish left the older keys
    alone, and the document is current unless the document under them was
    validated later. An unreadable document counts as current, so readers and
    publishers report it.
    """
    try:
        manifest = parse_v2_manifest(document)
    except (TypeError, ValueError):
        return True
    schema_1_copies = _schema_1_copies(manifest)
    if schema_1_copies is not None:
        return all(
            store.get(key) in (None, payload)
            for key, payload in schema_1_copies.items()
        )
    older = _current_schema_1_manifest(store)
    if older is None:
        return True
    try:
        older_manifest = parse_v2_manifest(older)
    except (TypeError, ValueError):
        return True
    return _newest_validation(older_manifest) <= _newest_validation(manifest)


def _current_manifest(store: V2ManifestStore) -> object:
    # Until the first schema version 2 publish, only the older keys exist.
    schema_2_document = store.get(SCHEMA_2_MANIFEST_KEY)
    if schema_2_document is not None and _is_current_schema_2_document(
        store, schema_2_document
    ):
        return schema_2_document
    return _current_schema_1_manifest(store)


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
        parsed = parse_v2_manifest(current)
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

    The whole manifest is written as one store value per key, so readers never
    see a partial update. The schema version 1 keys are rewritten only when
    schema version 1 can describe the manifest; otherwise their readers keep
    the last manifest written there.
    """
    next_manifest = build_next_v2_manifest(
        current=_current_manifest(store),
        worker=worker,
        force_default=force_default,
    )
    # Each key holds one entire validated document, and a reader uses one
    # whole document (the region dataset copy only while it matches the cache
    # copy), so no reader observes a partial update assembled from several.
    store[SCHEMA_2_MANIFEST_KEY] = next_manifest.model_dump(mode="json")
    schema_1_copies = _schema_1_copies(next_manifest)
    if schema_1_copies is not None:
        for key, payload in schema_1_copies.items():
            store[key] = payload
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
            manifest = parse_v2_manifest(value)
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
