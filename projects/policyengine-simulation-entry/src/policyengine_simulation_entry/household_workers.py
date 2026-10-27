"""Find a country's household function in the v2 version manifest.

The Public API names each calculation's catalog version and that version's
PolicyEngine.py version. The v2 version manifest (a Modal Dict that each v2
deployment publishes to) maps the PolicyEngine.py version to its deployed v2
worker application, which names each country's household function and the
bundle it runs. The calculation's request carries that bundle, and the
household function refuses any other.

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import modal
from policyengine_simulation_contract.household_execution import (
    HouseholdBundleProvenance,
)
from policyengine_simulation_contract.stage12_manifest import (
    V2_VERSION_MANIFEST_NAME,
    V2ManifestLoader,
)

from policyengine_simulation_entry.modal_calls import OWN_PROCESS_SIGNALS, ModalTarget

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_manifest import (
        V2ManifestStore,
        V2VersionManifest,
    )


class HouseholdFunctionUnavailableError(LookupError):
    """The manifest has no household function for this country and version."""


class HouseholdWorkersUnavailableError(RuntimeError):
    """The manifest could not be read; ask again later."""


@dataclass(frozen=True)
class HouseholdWorker:
    """A country's deployed household function and the bundle it runs."""

    target: ModalTarget
    bundle: HouseholdBundleProvenance


class ManifestHouseholdWorkers:
    """Resolve household functions from the v2 version manifest.

    Each resolution reads the manifest, which is small, so a newly published
    version is found at once. ``V2ManifestLoader`` keeps its last valid copy
    for a stored value that does not validate.
    """

    def __init__(self, loader: V2ManifestLoader) -> None:
        """Read the manifest through ``loader``."""
        self._loader = loader

    @classmethod
    def from_store(cls, store: V2ManifestStore) -> ManifestHouseholdWorkers:
        """Read the manifest from ``store``."""
        return cls(V2ManifestLoader(store))

    @classmethod
    def from_modal_environment(cls, environment: str) -> ManifestHouseholdWorkers:
        """Read the manifest from its Modal Dict in ``environment``.

        Nothing is read from Modal until the first resolution.
        """
        return cls.from_store(
            modal.Dict.from_name(V2_VERSION_MANIFEST_NAME, environment_name=environment)
        )

    def _manifest(self) -> V2VersionManifest:
        try:
            return self._loader.load()
        except BaseException as error:
            if isinstance(error, OWN_PROCESS_SIGNALS):
                raise
            msg = "the v2 version manifest could not be read"
            raise HouseholdWorkersUnavailableError(msg) from error

    def resolve(self, country: str, policyengine_version: str) -> HouseholdWorker:
        """Return ``country``'s household function in ``policyengine_version``.

        Raise HouseholdFunctionUnavailableError when the manifest has no such
        version, or the version no household function for the country, and
        HouseholdWorkersUnavailableError when the manifest cannot be read.
        """
        worker = self._manifest().versions.get(policyengine_version)
        if worker is None:
            msg = (
                f"PolicyEngine.py {policyengine_version} has no deployed v2 worker "
                "application"
            )
            raise HouseholdFunctionUnavailableError(msg)
        country_worker = worker.household_worker(country)
        if country_worker is None or country_worker.household_callable is None:
            msg = (
                f"PolicyEngine.py {policyengine_version} has no household function "
                f"for country {country!r}"
            )
            raise HouseholdFunctionUnavailableError(msg)
        return HouseholdWorker(
            target=ModalTarget(
                application=worker.application_name,
                function=country_worker.household_callable,
            ),
            bundle=HouseholdBundleProvenance.from_bundle(
                worker.bundle,
                bundle_manifest_sha256=worker.bundle_manifest_sha256,
                country=country,
            ),
        )
