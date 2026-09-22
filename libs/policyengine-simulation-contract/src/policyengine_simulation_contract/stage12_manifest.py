"""Strict, separately stored Stage 12 v2 worker version manifest."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import re
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
ACTIVE_MANIFEST_KEY = "active"
V2_APPLICATION_PREFIX = "policyengine-simulation-v2-py"
V1_APPLICATION_PREFIX = "policyengine-simulation-py"
_CALLABLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")

ManifestText = Annotated[str, Field(min_length=1, max_length=255)]
CallableName = Annotated[str, Field(pattern=_CALLABLE_PATTERN.pattern)]


class StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class V2CountryWorker(StrictManifestModel):
    country: CountryId
    single_simulation_callable: CallableName


class V2WorkerValidation(StrictManifestModel):
    validated: Literal[True]
    application_name: ManifestText
    bundle_manifest_sha256: Sha256Digest
    validated_at: datetime
    validation_invocation_id: ManifestText
    country_validation_invocation_ids: dict[CountryId, ManifestText]


class V2WorkerVersion(StrictManifestModel):
    application_name: ManifestText
    report_coordinator_callable: CallableName
    countries: tuple[V2CountryWorker, ...]
    bundle: Stage12BundleManifest
    bundle_manifest_sha256: Sha256Digest
    validation: V2WorkerValidation

    @model_validator(mode="after")
    def validate_worker(self) -> V2WorkerVersion:
        if not self.application_name.startswith(V2_APPLICATION_PREFIX):
            raise ValueError("v2 application uses an invalid name prefix")
        if self.application_name.startswith(V1_APPLICATION_PREFIX):
            raise ValueError("v2 application name collides with the v1 prefix")
        expected_name = v2_application_name(self.bundle.policyengine_version)
        if self.application_name != expected_name:
            raise ValueError("v2 application name does not match its bundle version")
        if self.validation.application_name != self.application_name:
            raise ValueError("worker validation names another application")
        if self.validation.bundle_manifest_sha256 != self.bundle_manifest_sha256:
            raise ValueError("worker validation names another bundle digest")
        countries = tuple(worker.country for worker in self.countries)
        bundle_countries = tuple(country.country for country in self.bundle.countries)
        if len(countries) != len(set(countries)):
            raise ValueError("v2 worker countries must be unique")
        if countries != bundle_countries:
            raise ValueError("v2 worker countries do not match the normalized bundle")
        if set(self.validation.country_validation_invocation_ids) != set(countries):
            raise ValueError("worker validation does not cover every country")
        return self


class V2VersionManifest(StrictManifestModel):
    schema_version: Literal[1] = 1
    generation: ManifestText
    default_version: ManifestText
    versions: dict[str, V2WorkerVersion]

    @model_validator(mode="after")
    def validate_versions(self) -> V2VersionManifest:
        if not self.versions:
            raise ValueError("v2 manifest must contain a worker version")
        if self.default_version not in self.versions:
            raise ValueError("v2 default version has no worker entry")
        for version, worker in self.versions.items():
            if version != worker.bundle.policyengine_version:
                raise ValueError("v2 manifest version key does not match its bundle")
        return self


class V2ManifestStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def __setitem__(self, key: str, value: object) -> None: ...


def v2_application_name(policyengine_version: str) -> str:
    if not policyengine_version or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", policyengine_version
    ):
        raise ValueError("PolicyEngine.py version cannot form a Modal application name")
    name = V2_APPLICATION_PREFIX + policyengine_version.replace(".", "-").replace(
        "+", "-"
    )
    if len(name) > 64:
        raise ValueError("v2 Modal application name exceeds 64 characters")
    return name


def manifest_digest(manifest: V2VersionManifest) -> str:
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


def build_next_v2_manifest(
    *,
    current: object,
    worker: V2WorkerVersion,
    force_default: bool = False,
) -> V2VersionManifest:
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
    next_manifest = build_next_v2_manifest(
        current=store.get(ACTIVE_MANIFEST_KEY),
        worker=worker,
        force_default=force_default,
    )
    # The entire validated document replaces one store value. Readers cannot
    # observe a partial update assembled from several keys.
    store[ACTIVE_MANIFEST_KEY] = next_manifest.model_dump(mode="json")
    return next_manifest


class V2ManifestLoader:
    """Load v2 state while retaining only this loader's last valid v2 copy."""

    def __init__(self, store: V2ManifestStore):
        self._store = store
        self._last_valid: V2VersionManifest | None = None

    def load(self) -> V2VersionManifest:
        value = self._store.get(ACTIVE_MANIFEST_KEY)
        try:
            manifest = V2VersionManifest.model_validate(value)
        except (TypeError, ValueError):
            if self._last_valid is None:
                raise ValueError("no valid v2 version manifest is available") from None
            return self._last_valid
        self._last_valid = manifest
        return manifest

    def resolve(self, version: str | None = None) -> tuple[str, V2WorkerVersion, str]:
        manifest = self.load()
        selected = version or manifest.default_version
        try:
            worker = manifest.versions[selected]
        except KeyError:
            raise ValueError(
                f"v2 worker version {selected!r} is not available"
            ) from None
        return selected, worker, manifest_digest(manifest)


def assert_separate_manifest_names(*, v1_name: str, v2_name: str) -> None:
    if not v1_name or not v2_name:
        raise ValueError("v1 and v2 manifest names must be non-empty")
    if v1_name == v2_name:
        raise ValueError("v1 and v2 manifests must use different storage names")


def validate_manifest_has_no_credentials(value: Mapping[str, object]) -> None:
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
