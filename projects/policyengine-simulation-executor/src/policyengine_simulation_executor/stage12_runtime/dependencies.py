"""Runtime interfaces and concrete dependency construction for Stage 12."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
    ComparisonReportRecord,
    ComparisonSimulationPersistenceResult,
    ComparisonSimulationRecord,
)
from policyengine_stage12_persistence import Stage12PersistenceStore

from policyengine_simulation_executor.stage12_artifacts import Stage12ArtifactStore


class ComparisonStore(Protocol):
    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult: ...

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord: ...

    def replace_report(
        self, record: ComparisonReportRecord
    ) -> ComparisonReportRecord: ...

    def replace_report_result_comparison(
        self, record: ComparisonReportRecord
    ) -> ComparisonReportRecord: ...

    def create_or_resolve_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationPersistenceResult: ...

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> ComparisonSimulationRecord: ...

    def replace_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationRecord: ...

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonSimulationRecord: ...


class ChildCall(Protocol):
    object_id: str

    def get(self, *, timeout: float | None = None) -> object: ...


class ChildInvoker(Protocol):
    def spawn(
        self,
        *,
        application_name: str,
        function_name: str,
        environment: str,
        simulation: dict[str, Any],
        context: dict[str, Any],
        observability_context: dict[str, Any] | None,
    ) -> ChildCall: ...

    def restore(self, invocation_id: str) -> ChildCall: ...


class ModalChildInvoker:
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
        from importlib import import_module

        modal: Any = import_module("modal")
        function = modal.Function.from_name(
            application_name,
            function_name,
            environment_name=environment,
        )
        return function.spawn(simulation, context, observability_context)

    def restore(self, invocation_id: str) -> ChildCall:
        from importlib import import_module

        modal: Any = import_module("modal")
        return modal.FunctionCall.from_id(invocation_id)


def runtime_store() -> Stage12PersistenceStore:
    return Stage12PersistenceStore(os.environ.get("STAGE12_DATABASE_URL", ""))


def artifact_store() -> Stage12ArtifactStore:
    return Stage12ArtifactStore(os.environ.get("STAGE12_ARTIFACT_BUCKET", ""))
