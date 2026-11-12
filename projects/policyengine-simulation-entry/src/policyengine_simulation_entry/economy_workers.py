"""The default v2 worker's economy functions, and starting their calls.

The economy routes use only the v2 version manifest's default worker: the
Modal application deployed for one .py version, which names its report
coordinator, each country's simulation function, and the bundle they run.
A report run is calculated by the coordinator, which starts a simulation
function for each simulation run it doesn't reuse; a standalone simulation
run is calculated by the simulation function alone. Each is called by name
with ``spawn``, so a route waits only for Modal to accept the call, and is
given the run IDs it claims and records, never a Stage 12 evaluation
record.

This is the economy path's own Modal access, apart from the Stage 12
comparison path's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import modal
from policyengine_simulation_contract.stage12_manifest import (
    V2ManifestLoader,
    manifest_digest,
)
from policyengine_simulation_persistence import RunVersions

from policyengine_simulation_entry.household_workers import (
    VersionManifestUnavailableError,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import Stage12CountryBundle
    from policyengine_simulation_contract.stage12_manifest import (
        V2ManifestStore,
        V2VersionManifest,
    )


class EconomyWorkerUnavailableError(LookupError):
    """The default v2 worker cannot calculate the country's economy runs."""


@dataclass(frozen=True)
class EconomyWorker:
    """A country's economy functions in the default worker, and its bundle.

    ``version`` is the worker's .py version, the manifest's default, and
    ``version_manifest_sha256`` the digest of the manifest it was read from.
    """

    version: str
    version_manifest_sha256: str
    application_name: str
    coordinator_callable: str
    simulation_callable: str
    bundle_manifest_sha256: str
    policyengine_version: str
    country: Stage12CountryBundle

    @property
    def versions(self) -> RunVersions:
        """Return the versions every run this worker calculates records."""
        return RunVersions(
            country_package_version=self.country.country_package_version,
            policyengine_version=self.policyengine_version,
        )


def default_economy_worker(manifest: V2VersionManifest, country: str) -> EconomyWorker:
    """Return the manifest's default worker's economy functions for ``country``.

    Raise EconomyWorkerUnavailableError when it cannot calculate the
    country's economy runs.
    """
    version = manifest.default_version
    return _economy_worker(
        manifest,
        country,
        version,
        described_as=f"the default v2 worker, .py {version},",
    )


def economy_worker_for_version(
    manifest: V2VersionManifest, country: str, policyengine_version: str
) -> EconomyWorker:
    """Return the economy functions of the manifest's worker for a .py version.

    A run is calculated by the worker of the .py version it records, which
    may no longer be the default. Raise EconomyWorkerUnavailableError when the
    manifest no longer holds that worker, or it cannot calculate the
    country's economy runs.
    """
    if policyengine_version not in manifest.versions:
        msg = f"the v2 version manifest has no worker for .py {policyengine_version}"
        raise EconomyWorkerUnavailableError(msg)
    return _economy_worker(
        manifest,
        country,
        policyengine_version,
        described_as=f"the v2 worker for .py {policyengine_version}",
    )


def _economy_worker(
    manifest: V2VersionManifest, country: str, version: str, *, described_as: str
) -> EconomyWorker:
    """Return the economy functions of the manifest's worker ``version``.

    ``described_as`` names the worker in the error raised when it cannot
    calculate the country's economy runs.
    """
    worker = manifest.versions[version]
    country_worker = worker.economy_worker(country)
    country_bundle = next(
        (item for item in worker.bundle.countries if item.country == country), None
    )
    if (
        country_worker is None
        or country_worker.single_simulation_callable is None
        or country_bundle is None
    ):
        msg = f"{described_as} cannot calculate economy runs for {country}"
        raise EconomyWorkerUnavailableError(msg)
    return EconomyWorker(
        version=version,
        version_manifest_sha256=manifest_digest(manifest),
        application_name=worker.application_name,
        coordinator_callable=worker.report_coordinator_callable,
        simulation_callable=country_worker.single_simulation_callable,
        bundle_manifest_sha256=worker.bundle_manifest_sha256,
        policyengine_version=worker.bundle.policyengine_version,
        country=country_bundle,
    )


class EconomyWorkerSource(Protocol):
    """Find the default worker's economy functions (``ManifestEconomyWorkers``)."""

    def default_worker(self, country: str) -> EconomyWorker:
        """Return the country's economy functions, or raise why there are none."""
        ...


class ManifestEconomyWorkers:
    """Find economy functions in the v2 version manifest's default worker.

    Each lookup reads the manifest, which is small, so a moved default is
    used at once. The loader keeps its last valid copy for a stored value
    that does not validate; a store that cannot be read is unavailable.
    """

    def __init__(self, store: V2ManifestStore) -> None:
        """Read the manifest from ``store``; nothing is read until a lookup."""
        self._loader = V2ManifestLoader(store)

    @classmethod
    def from_modal(
        cls, *, manifest_name: str, environment: str
    ) -> ManifestEconomyWorkers:
        """Read the manifest from its Modal Dict, which must already exist."""
        return cls(
            modal.Dict.from_name(
                manifest_name, environment_name=environment, create_if_missing=False
            )
        )

    def default_worker(self, country: str) -> EconomyWorker:
        """Return the default worker's economy functions for ``country``.

        Raise EconomyWorkerUnavailableError when it has none, and
        VersionManifestUnavailableError when the manifest cannot be read.
        """
        return default_economy_worker(self._manifest(), country)

    def worker_for_version(
        self, country: str, policyengine_version: str
    ) -> EconomyWorker:
        """Return the economy functions of the worker for a .py version.

        Raise EconomyWorkerUnavailableError when the manifest has none, and
        VersionManifestUnavailableError when it cannot be read.
        """
        return economy_worker_for_version(
            self._manifest(), country, policyengine_version
        )

    def _manifest(self) -> V2VersionManifest:
        try:
            return self._loader.load()
        # Modal raises its own errors, and any of them means the same here.
        except Exception as error:
            msg = "the v2 version manifest could not be read"
            raise VersionManifestUnavailableError(msg) from error


class EconomyRunCaller(Protocol):
    """Start the Modal call that calculates a run (``ModalEconomyRunCaller``)."""

    async def start_report_run(
        self,
        worker: EconomyWorker,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the report run's coordinator and return its Modal call ID."""
        ...

    async def start_simulation_run(
        self,
        worker: EconomyWorker,
        simulation_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the standalone simulation run's function; return its call ID."""
        ...


class ModalEconomyRunCaller:
    """Start economy calls on Modal, by application and function name."""

    def __init__(self, environment: str) -> None:
        """Look up the functions in this Modal environment."""
        self._environment = environment

    async def start_report_run(
        self,
        worker: EconomyWorker,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the coordinator with the report and its run IDs; return its call ID.

        Raise RuntimeError when Modal returns no call ID.
        """
        return await self._spawn(
            worker,
            worker.coordinator_callable,
            report_payload,
            context_payload,
            observability_context=observability_context,
        )

    async def start_simulation_run(
        self,
        worker: EconomyWorker,
        simulation_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the simulation function with its run's ID; return its call ID.

        Raise RuntimeError when Modal returns no call ID.
        """
        return await self._spawn(
            worker,
            worker.simulation_callable,
            simulation_payload,
            context_payload,
            observability_context=observability_context,
        )

    async def _spawn(
        self,
        worker: EconomyWorker,
        function_name: str,
        payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        function = modal.Function.from_name(
            worker.application_name,
            function_name,
            environment_name=self._environment,
        )
        call = await function.spawn.aio(
            payload, context_payload, observability_context=observability_context
        )
        call_id = getattr(call, "object_id", None)
        if not isinstance(call_id, str) or not call_id:
            msg = f"Modal returned no call ID for {function_name}'s call"
            raise RuntimeError(msg)
        return call_id
