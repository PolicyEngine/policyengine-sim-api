"""The default v2 worker's household functions, and starting their calls.

The household routes use only the v2 version manifest's default worker: the
Modal application deployed for one .py version, which names each country's
household function and the bundle it runs. A calculation's request carries
that bundle, and the household function refuses any other. The manifest is a
Modal Dict, read through the contract's ``V2ManifestLoader``; the function is
called by name with ``spawn``, so the route waits only for Modal to accept
the call.

This is the household path's own Modal access, apart from the Stage 12
comparison path's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import modal
from policyengine_simulation_contract.household_execution import (
    HouseholdBundleProvenance,
)
from policyengine_simulation_contract.stage12_manifest import V2ManifestLoader

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_contract.stage12_manifest import (
        V2ManifestStore,
        V2VersionManifest,
    )


class HouseholdFunctionUnavailableError(LookupError):
    """The default v2 worker has no household function for the country."""


class VersionManifestUnavailableError(RuntimeError):
    """The v2 version manifest could not be read; ask again later."""


@dataclass(frozen=True)
class HouseholdWorker:
    """A country's household function in the default worker, and its bundle."""

    application_name: str
    function_name: str
    bundle: HouseholdBundleProvenance


class HouseholdWorkerSource(Protocol):
    """Find the default worker's household function (``ManifestHouseholdWorkers``)."""

    def default_worker(self, country: str) -> HouseholdWorker:
        """Return the country's household function, or raise why there is none."""
        ...


class HouseholdFunctionCaller(Protocol):
    """Start one household function call (``ModalHouseholdFunctionCaller``)."""

    async def spawn(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        household_job_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the call without waiting for it and return its Modal call ID."""
        ...


class HouseholdReportRunCaller(Protocol):
    """Start one household report run's call (``ModalHouseholdFunctionCaller``)."""

    async def start_report_run(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        report_run_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the call without waiting for it and return its Modal call ID."""
        ...


def default_household_worker(
    manifest: V2VersionManifest, country: str
) -> HouseholdWorker:
    """Return the manifest's default worker's household function for ``country``.

    Raise HouseholdFunctionUnavailableError when it has none.
    """
    version = manifest.default_version
    worker = manifest.versions[version]
    country_worker = worker.household_worker(country)
    if country_worker is None or country_worker.household_callable is None:
        msg = (
            f"the default v2 worker, .py {version}, has no household function "
            f"for {country}"
        )
        raise HouseholdFunctionUnavailableError(msg)
    return HouseholdWorker(
        application_name=worker.application_name,
        function_name=country_worker.household_callable,
        bundle=HouseholdBundleProvenance.from_bundle(
            worker.bundle,
            bundle_manifest_sha256=worker.bundle_manifest_sha256,
            country=country,
        ),
    )


class ManifestHouseholdWorkers:
    """Find household functions in the v2 version manifest's default worker.

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
    ) -> ManifestHouseholdWorkers:
        """Read the manifest from its Modal Dict, which must already exist."""
        return cls(
            modal.Dict.from_name(
                manifest_name, environment_name=environment, create_if_missing=False
            )
        )

    def default_worker(self, country: str) -> HouseholdWorker:
        """Return the default worker's household function for ``country``.

        Raise HouseholdFunctionUnavailableError when it has none, and
        VersionManifestUnavailableError when the manifest cannot be read.
        """
        try:
            manifest = self._loader.load()
        # Modal raises its own errors, and any of them means the same here.
        except Exception as error:
            msg = "the v2 version manifest could not be read"
            raise VersionManifestUnavailableError(msg) from error
        return default_household_worker(manifest, country)


class ModalHouseholdFunctionCaller:
    """Start household function calls on Modal, by application and function name."""

    def __init__(self, environment: str) -> None:
        """Look up household functions in this Modal environment."""
        self._environment = environment

    async def spawn(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        household_job_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the household function's call and return its Modal call ID.

        Raise RuntimeError when Modal returns no call ID.
        """
        return await self._spawn(
            worker,
            payload,
            household_job_id=str(household_job_id),
            observability_context=observability_context,
        )

    async def start_report_run(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        report_run_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Start the household function on a household report run's calculation.

        ``payload`` is the report run's ``HouseholdReportRunCalculation``.
        Return the call's Modal call ID; raise RuntimeError when Modal
        returns none.
        """
        return await self._spawn(
            worker,
            payload,
            household_report_run_id=str(report_run_id),
            observability_context=observability_context,
        )

    async def _spawn(
        self, worker: HouseholdWorker, payload: dict[str, Any], **keywords: object
    ) -> str:
        function = modal.Function.from_name(
            worker.application_name,
            worker.function_name,
            environment_name=self._environment,
        )
        call = await function.spawn.aio(payload, **keywords)
        call_id = getattr(call, "object_id", None)
        if not isinstance(call_id, str) or not call_id:
            msg = "Modal returned no call ID for the household function's call"
            raise RuntimeError(msg)
        return call_id
