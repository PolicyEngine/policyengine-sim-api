"""Runtime interfaces and concrete dependency construction for Stage 12."""

from __future__ import annotations

import os
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_stage12_persistence import Stage12PersistenceStore

from policyengine_simulation_executor.stage12_artifacts import Stage12ArtifactStore

from .database_retry import open_connection_with_retry

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonReportPersistenceResult,
        ComparisonReportRecord,
        ComparisonSimulationPersistenceResult,
        ComparisonSimulationRecord,
    )


class ComparisonStore(Protocol):
    """Report and simulation persistence; ``Stage12PersistenceStore`` implements it."""

    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult:
        """Insert the report, or return the stored one with the same identity."""
        ...

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord:
        """Return the stored report; raise ``LookupError`` if there is none."""
        ...

    def replace_report(self, record: ComparisonReportRecord) -> ComparisonReportRecord:
        """Write the report's lifecycle and aggregation fields over the stored one."""
        ...

    def replace_report_result_comparison(
        self, record: ComparisonReportRecord
    ) -> ComparisonReportRecord:
        """Write the report's result-comparison fields over the stored one."""
        ...

    def create_or_resolve_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationPersistenceResult:
        """Insert the simulation, or return the stored one with the same identity."""
        ...

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> ComparisonSimulationRecord:
        """Return the stored simulation; raise ``LookupError`` if there is none."""
        ...

    def replace_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationRecord:
        """Write the simulation's mutable fields over the stored one."""
        ...

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonSimulationRecord:
        """Replace ``expected_placeholder`` with the child's real Modal invocation id.

        Repeating an attach that already succeeded returns the stored record.
        """
        ...


class ChildCall(Protocol):
    """Handle to a spawned child simulation, such as a ``modal.FunctionCall``."""

    object_id: str

    def get(self, *, timeout: float | None = None) -> object:
        """Wait up to ``timeout`` seconds (indefinitely if None) for the result."""
        ...


class ChildInvoker(Protocol):
    """Launcher for child simulation functions that can reattach to running calls."""

    def spawn(
        self,
        *,
        application_name: str,
        function_name: str,
        environment: str,
        simulation: dict[str, Any],
        context: dict[str, Any],
        observability_context: dict[str, Any] | None,
    ) -> ChildCall:
        """Start the named deployed function on one simulation without waiting."""
        ...

    def restore(self, invocation_id: str) -> ChildCall:
        """Return a handle to a call previously started, by its invocation id."""
        ...


class ModalChildInvoker:
    """``ChildInvoker`` backed by functions deployed to Modal."""

    def spawn(
        self,
        *,
        application_name: str,
        function_name: str,
        environment: str,
        simulation: dict[str, Any],
        context: dict[str, Any],
        observability_context: dict[str, Any] | None,
    ) -> ChildCall:
        """Look up ``application_name.function_name`` in ``environment``; spawn it."""
        from importlib import import_module

        modal: Any = import_module("modal")
        function = modal.Function.from_name(
            application_name,
            function_name,
            environment_name=environment,
        )
        return function.spawn(
            simulation,
            context,
            observability_context=observability_context,
        )

    def restore(self, invocation_id: str) -> ChildCall:
        """Return the ``modal.FunctionCall`` with this id."""
        from importlib import import_module

        modal: Any = import_module("modal")
        return modal.FunctionCall.from_id(invocation_id)


def runtime_store(
    runtime: ObservabilityRuntime | None = None,
) -> Stage12PersistenceStore:
    """Build the persistence store from ``STAGE12_DATABASE_URL``.

    Raises ``ValueError`` when the variable is unset or not a complete
    ``postgresql://`` URL.
    """
    # Every operation opens its own pooler session, so each one retries a
    # refused connection rather than failing the report during a burst.
    return Stage12PersistenceStore(
        os.environ.get("STAGE12_DATABASE_URL", ""),
        open_connection=partial(open_connection_with_retry, runtime=runtime),
    )


def artifact_store() -> Stage12ArtifactStore:
    """Build the store for ``STAGE12_ARTIFACT_BUCKET``; ValueError if it is unset."""
    return Stage12ArtifactStore(os.environ.get("STAGE12_ARTIFACT_BUCKET", ""))
