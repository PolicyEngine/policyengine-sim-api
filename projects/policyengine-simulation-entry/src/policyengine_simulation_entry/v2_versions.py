"""``/versions`` answers built from the v2 version manifest.

The old gateway answers ``GET /versions`` and ``GET /versions/{kind}`` from
v1's routing state. These functions build answers with the same response
models from the v2 version manifest instead, for the v2 side of the
entrypoint. They share no code with the routes that forward to the old
gateway.

What the manifest can and cannot say:

- Each route names the v2 worker application (``policyengine-simulation-v2-py…``)
  that serves a version, where the old gateway names the v1 application.
- A country model version that several bundles contain routes to the newest
  of them by version order. The old gateway routes it to the last bundle
  published, which is the newest unless an older bundle was republished.
- ``latest`` is the manifest's default version, and each country's ``latest``
  is that bundle's model version.
- ``spm_capabilities`` is always empty: the manifest records no SPM
  capability.

``ModalV2VersionsSource`` reads the manifest from the Modal Dict that the
deploy publishes it to, the same one the Stage 12 comparison path reads.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Protocol

import modal
from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_contract.gateway_models import (
    PolicyEngineBundle,
    VersionMap,
    VersionsResponse,
)
from policyengine_simulation_contract.stage12_manifest import V2ManifestLoader

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        Stage12BundleManifest,
        Stage12CountryBundle,
    )
    from policyengine_simulation_contract.stage12_manifest import (
        V2ManifestStore,
        V2VersionManifest,
        V2WorkerVersion,
    )

    from policyengine_simulation_entry.config import Settings

POLICYENGINE_KIND = "policyengine"
COUNTRY_KINDS = ("us", "uk")
VERSION_KINDS = (POLICYENGINE_KIND, *COUNTRY_KINDS)
LATEST_KEY = "latest"


class UnknownVersionKindError(LookupError):
    """Raised for a ``/versions/{kind}`` kind that is not policyengine, us or uk."""

    def __init__(self, kind: str) -> None:
        """Record the response detail the old gateway sends for this kind."""
        self.detail = f"Unknown version kind: {kind}"
        super().__init__(self.detail)


def is_version_kind(kind: str) -> bool:
    """Return whether ``kind`` is policyengine, us or uk, in any case."""
    return kind.lower() in VERSION_KINDS


class V2VersionsSource(Protocol):
    """Where the entrypoint reads the v2 version manifest from."""

    async def load_manifest(self) -> V2VersionManifest:
        """Return the current manifest; raise if none can be read."""
        ...


class ModalV2VersionsSource:
    """Reads the v2 version manifest from its Modal Dict.

    The loader keeps the last valid manifest it read, and returns it if the
    stored value later goes missing or becomes invalid. With no valid
    manifest read yet, ``load_manifest`` raises ValueError.
    """

    def __init__(self, store: V2ManifestStore) -> None:
        """Read from ``store``; nothing is read until ``load_manifest``."""
        self._loader = V2ManifestLoader(store)
        # The Modal client blocks, so reads run on a thread. One of this
        # source's own is enough, and keeps a stalled read from holding
        # threads of the event loop's default executor, which the Stage 12
        # comparison path uses. One thread also means one read at a time, as
        # the loader expects.
        self._read_thread = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="v2-version-manifest"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> ModalV2VersionsSource | None:
        """Open the configured manifest Dict, or return None if none is configured.

        The deploy sets the manifest's Modal environment in staging and
        production. Opening the Dict is lazy: nothing is read here, and the
        Dict is never created.
        """
        if not settings.stage12_v2_manifest_environment:
            return None
        store = modal.Dict.from_name(
            settings.stage12_v2_manifest_name,
            environment_name=settings.stage12_v2_manifest_environment,
        )
        return cls(store)

    async def load_manifest(self) -> V2VersionManifest:
        """Read the manifest on this source's thread."""
        return await asyncio.get_running_loop().run_in_executor(
            self._read_thread, self._loader.load
        )


def _version_order(policyengine_version: str) -> tuple[bool, tuple[int, ...], str]:
    """Order versions oldest first.

    A version made of dot-separated numbers, such as ``6.10.0``, is ordered by
    those numbers. Any other version sorts before every such version, by its
    text.
    """
    parts = policyengine_version.split(".")
    if all(part.isascii() and part.isdigit() for part in parts):
        return (True, tuple(int(part) for part in parts), policyengine_version)
    return (False, (), policyengine_version)


def _oldest_first(
    manifest: V2VersionManifest,
) -> list[tuple[str, V2WorkerVersion]]:
    return sorted(
        manifest.versions.items(),
        key=lambda entry: _version_order(entry[0]),
    )


def _country_bundle(
    worker: V2WorkerVersion, country: str
) -> Stage12CountryBundle | None:
    return next(
        (
            country_bundle
            for country_bundle in worker.bundle.countries
            if country_bundle.country == country
        ),
        None,
    )


def _policyengine_routes(manifest: V2VersionManifest) -> dict[str, str]:
    routes = {
        version: worker.application_name for version, worker in _oldest_first(manifest)
    }
    routes[LATEST_KEY] = manifest.default_version
    return routes


def _country_routes(manifest: V2VersionManifest, country: str) -> dict[str, str]:
    routes: dict[str, str] = {}
    # Oldest first, so a model version that several bundles contain ends up
    # routed to the newest of them.
    for _, worker in _oldest_first(manifest):
        country_bundle = _country_bundle(worker, country)
        if country_bundle is not None:
            routes[country_bundle.country_package_version] = worker.application_name
    default_bundle = _country_bundle(
        manifest.versions[manifest.default_version], country
    )
    if default_bundle is not None:
        routes[LATEST_KEY] = default_bundle.country_package_version
    return routes


def version_map_from_manifest(manifest: V2VersionManifest, kind: str) -> VersionMap:
    """Return ``kind``'s routes and latest version, as ``GET /versions/{kind}`` does.

    ``kind`` is matched without regard to case. Raises UnknownVersionKindError
    for any kind other than policyengine, us or uk.
    """
    kind_lower = kind.lower()
    if kind_lower not in VERSION_KINDS:
        raise UnknownVersionKindError(kind)
    if kind_lower == POLICYENGINE_KIND:
        return VersionMap(_policyengine_routes(manifest))
    return VersionMap(_country_routes(manifest, kind_lower))


def _default_dataset_sha256(country_bundle: Stage12CountryBundle) -> str | None:
    return next(
        (
            dataset.sha256
            for dataset in country_bundle.datasets
            if dataset.identity == country_bundle.default_dataset
        ),
        None,
    )


def _runtime_dataset(country_bundle: Stage12CountryBundle) -> str | None:
    """Return the default dataset's runtime URI, or None if it cannot be resolved.

    As in the old gateway, one entry with an unresolvable dataset is reported
    without one rather than failing the whole answer. ``validate_hf=False``
    trusts the bundle and makes no network call.
    """
    try:
        return runtime_dataset_uri(
            country_bundle.default_dataset_uri,
            default_revision=country_bundle.data_package_version,
            artifact_revision=country_bundle.data_artifact_revision,
            validate_hf=False,
        )
    except ValueError:
        # HuggingFaceDatasetReferenceError is a ValueError.
        return None


def _country_identity(
    bundle: Stage12BundleManifest,
    country_bundle: Stage12CountryBundle,
) -> PolicyEngineBundle:
    return PolicyEngineBundle(
        model_version=country_bundle.country_package_version,
        policyengine_version=bundle.policyengine_version,
        data_version=country_bundle.data_release_version,
        dataset=_runtime_dataset(country_bundle),
        dataset_sha256=_default_dataset_sha256(country_bundle),
    )


def _bundle_identities(
    manifest: V2VersionManifest,
) -> dict[str, dict[str, PolicyEngineBundle]]:
    return {
        version: {
            country_bundle.country: _country_identity(worker.bundle, country_bundle)
            for country_bundle in worker.bundle.countries
        }
        for version, worker in _oldest_first(manifest)
    }


def versions_from_manifest(manifest: V2VersionManifest) -> VersionsResponse:
    """Return the whole ``GET /versions`` answer for ``manifest``."""
    return VersionsResponse(
        spm_capabilities={},
        policyengine=version_map_from_manifest(manifest, POLICYENGINE_KIND),
        us=version_map_from_manifest(manifest, "us"),
        uk=version_map_from_manifest(manifest, "uk"),
        bundles=_bundle_identities(manifest),
    )
