"""Strict, separately stored Stage 12 v2 worker version manifest.

Schema version 2 records, for each country a worker application serves,
whether it can run economy reports and which function calculates its
households. It is stored under its own key. Whenever schema version 1 can
describe the same manifest, it is also written in that form, without those
fields, under the keys that readers released before schema version 2 read:
with region datasets, without them, and without them or cache releases. The
UK entry's ``local_area_reports`` is written only under its own key, which
readers released with it read first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from packaging.version import InvalidVersion, Version

from policyengine_simulation_contract.stage12_manifest_models import (
    LOCAL_AREA_REPORTS_VERSION,
    V1_APPLICATION_PREFIX,
    V2_APPLICATION_PREFIX,
    CallableName,
    LocalAreaReports,
    ManifestText,
    Stage12CacheRelease,
    StrictManifestModel,
    V2CountryWorker,
    V2VersionManifest,
    V2WorkerValidation,
    V2WorkerVersion,
    v2_application_name,
)

if TYPE_CHECKING:
    from datetime import datetime

    from pydantic.main import IncEx

__all__ = [
    "ACTIVE_MANIFEST_KEY",
    "LOCAL_AREA_REPORTS_MANIFEST_KEY",
    "LOCAL_AREA_REPORTS_VERSION",
    "PRE_CACHE_MANIFEST_KEY",
    "REGION_DATASETS_MANIFEST_KEY",
    "SCHEMA_2_MANIFEST_KEY",
    "V1_APPLICATION_PREFIX",
    "V1_ROUTING_STATE_NAME",
    "V2_APPLICATION_PREFIX",
    "V2_MANIFEST_KEYS",
    "V2_VERSION_MANIFEST_NAME",
    "CallableName",
    "LocalAreaReports",
    "ManifestText",
    "Stage12CacheRelease",
    "StrictManifestModel",
    "V2CountryWorker",
    "V2ManifestLoader",
    "V2ManifestStore",
    "V2VersionManifest",
    "V2WorkerValidation",
    "V2WorkerVersion",
    "add_v2_worker",
    "assert_separate_manifest_names",
    "build_next_v2_manifest",
    "manifest_digest",
    "move_v2_default",
    "parse_v2_manifest",
    "pre_cache_manifest_payload",
    "pre_region_datasets_manifest_payload",
    "publish_v2_manifest",
    "read_v2_manifest",
    "schema_1_manifest_payload",
    "schema_2_manifest_payload",
    "v2_application_name",
    "v2_manifest_copies",
    "validate_manifest_has_no_credentials",
    "write_v2_manifest",
]

V1_ROUTING_STATE_NAME = "simulation-api-routing-state"
V2_VERSION_MANIFEST_NAME = "simulation-api-v2-version-manifest"
# The schema version 2 manifest with the UK entry's ``local_area_reports``.
# Readers released with it read this key first.
LOCAL_AREA_REPORTS_MANIFEST_KEY = "active-schema-2-with-local-area-reports"
# The schema version 2 manifest without ``local_area_reports``, which readers
# released before it forbid. They read this key first.
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
# Every reader's key, in the order a write writes them (newest reader first).
# A write leaves the schema version 1 keys alone when schema version 1 can't
# describe the manifest.
V2_MANIFEST_KEYS = (
    LOCAL_AREA_REPORTS_MANIFEST_KEY,
    SCHEMA_2_MANIFEST_KEY,
    REGION_DATASETS_MANIFEST_KEY,
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
)


class V2ManifestStore(Protocol):
    """The part of a Modal Dict's mapping interface the manifest helpers use."""

    def get(self, key: str, default: object = None) -> object:
        """Return the value stored under ``key``, or ``default`` when absent."""
        ...

    def __setitem__(self, key: str, value: object) -> None:
        """Store ``value`` under ``key``, replacing any previous value."""
        ...


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
        "countries": {
            "__all__": {"economy_capable", "household_callable", "local_area_reports"}
        },
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


def schema_2_manifest_payload(manifest: V2VersionManifest) -> dict[str, object]:
    """Return the manifest as readers released before local-area reports accept it."""
    return manifest.model_dump(
        mode="json",
        exclude={
            "versions": {"__all__": {"countries": {"__all__": {"local_area_reports"}}}}
        },
    )


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


def _is_current_local_area_copy(store: V2ManifestStore, document: object) -> bool:
    """Return whether ``document``, under the local-area report key, is the latest.

    Code released before ``LocalAreaReports`` publishes only the older keys,
    for example after a rollback, which leaves an out-of-date copy behind. A
    current copy, without the field, is exactly the manifest under
    ``active-schema-2``, which is itself current. Without an
    ``active-schema-2`` copy it is not current: deleting that key is how an
    operator sends every reader back to the older keys before a rollback.
    An unreadable document counts as current, so readers and publishers
    report it.
    """
    try:
        manifest = parse_v2_manifest(document)
    except (TypeError, ValueError):
        return True
    without_field = schema_2_manifest_payload(manifest)
    if store.get(SCHEMA_2_MANIFEST_KEY) != without_field:
        return False
    return _is_current_schema_2_document(store, without_field)


def _current_manifest(store: V2ManifestStore) -> object:
    local_area_document = store.get(LOCAL_AREA_REPORTS_MANIFEST_KEY)
    if local_area_document is not None and _is_current_local_area_copy(
        store, local_area_document
    ):
        return local_area_document
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


def add_v2_worker(*, current: object, worker: V2WorkerVersion) -> V2VersionManifest:
    """Return ``current`` with ``worker`` added under its bundle version.

    The default version stays as it is, so requests that pin no version keep
    reaching the same worker; only the first worker (``current`` is None)
    becomes the default. A deployment adds its worker this way before its
    checks, only into an existing manifest, and moves the default
    (:func:`move_v2_default`) after them; an operator starts a missing
    manifest with its first worker.
    """
    if current is None:
        versions: dict[str, V2WorkerVersion] = {}
        default_version = worker.bundle.policyengine_version
    else:
        parsed = parse_v2_manifest(current)
        versions = deepcopy(parsed.versions)
        default_version = parsed.default_version
    version = worker.bundle.policyengine_version
    versions[version] = worker
    return V2VersionManifest(
        generation=f"{version}:{worker.application_name}",
        default_version=default_version,
        versions=versions,
    )


def move_v2_default(
    *, current: object, version: str, force_default: bool = False
) -> V2VersionManifest:
    """Return ``current`` with its default moved to an added worker ``version``.

    The default moves only when ``version`` is newer by PEP 440 ordering (an
    unparsable version never is) or ``force_default`` is set; otherwise the
    manifest is returned as it was, generation included, so its digest (and
    every Stage 12 identity built from it) stays. Raises ValueError when
    ``current`` has no worker for ``version``.
    """
    parsed = parse_v2_manifest(current)
    worker = parsed.versions.get(version)
    if worker is None:
        msg = f"the v2 manifest has no v2 worker {version!r} to make the default"
        raise ValueError(msg)
    if not (force_default or _is_newer(version, parsed.default_version)):
        return parsed
    default_version = version
    return V2VersionManifest(
        generation=f"default:{version}:{worker.application_name}",
        default_version=default_version,
        versions=deepcopy(parsed.versions),
    )


def read_v2_manifest(store: V2ManifestStore) -> V2VersionManifest | None:
    """Return the stored manifest, or None before the first publish.

    Reads the key a :class:`V2ManifestLoader` reads, and raises ValueError
    for a stored value that is not a valid manifest.
    """
    current = _current_manifest(store)
    if current is None:
        return None
    return parse_v2_manifest(current)


def v2_manifest_copies(manifest: V2VersionManifest) -> dict[str, object]:
    """Return each reader's key and its copy of ``manifest``, in write order.

    The schema version 1 keys are included only when schema version 1 can
    describe the manifest; otherwise a write leaves them, and their readers
    keep the last manifest written there.
    """
    copies: dict[str, object] = {
        LOCAL_AREA_REPORTS_MANIFEST_KEY: manifest.model_dump(mode="json"),
        SCHEMA_2_MANIFEST_KEY: schema_2_manifest_payload(manifest),
    }
    schema_1_copies = _schema_1_copies(manifest)
    if schema_1_copies is not None:
        copies.update(schema_1_copies)
    return copies


def write_v2_manifest(*, store: V2ManifestStore, manifest: V2VersionManifest) -> None:
    """Write ``manifest`` under each reader's key, each as one whole document."""
    # Each key holds one entire validated document, and a reader uses one
    # whole document (the region dataset copy only while it matches the cache
    # copy), so no reader observes a document assembled from several. The
    # keys are written one after another, so a write that stops part-way
    # leaves some keys on the previous manifest.
    for key, copy in v2_manifest_copies(manifest).items():
        store[key] = copy


def publish_v2_manifest(
    *,
    store: V2ManifestStore,
    worker: V2WorkerVersion,
    force_default: bool = False,
) -> V2VersionManifest:
    """Add ``worker`` to the stored manifest, write it back and return it.

    The default moves as :func:`build_next_v2_manifest` moves it, in the same
    write (see :func:`write_v2_manifest`).
    """
    next_manifest = build_next_v2_manifest(
        current=_current_manifest(store),
        worker=worker,
        force_default=force_default,
    )
    write_v2_manifest(store=store, manifest=next_manifest)
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
