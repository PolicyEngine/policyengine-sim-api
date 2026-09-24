"""Acknowledgement-only Stage 12 dispatch from the Simulation Entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import modal
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    ReportExecutionInput,
    ResultComparisonStatus,
)
from policyengine_simulation_contract.stage12_manifest import (
    V2CountryWorker,
    V2ManifestLoader,
    V2WorkerVersion,
)
from policyengine_stage12_persistence import Stage12PersistenceStore
from policyengine_simulation_observability.identifiers import (
    normalize_observability_id,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    Stage,
)

from policyengine_simulation_entry.config import Settings
from policyengine_simulation_entry.stage12_adapter import adapt_annual_comparison

logger = logging.getLogger(__name__)
AUTOMATIC_REPORT_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://policyengine.org/internal/stage12/automatic-reports",
)


class ComparisonStore(Protocol):
    """Read temporary comparison state from the API-owned schema."""

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord: ...

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonSimulationRecord, ...]: ...


class ReportInvoker(Protocol):
    async def spawn(
        self,
        *,
        worker: V2WorkerVersion,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
        parent_payload: dict[str, Any],
        observability_context: dict[str, Any] | None = None,
    ) -> str: ...


type PreparedReport = tuple[
    ComparisonReportRecord,
    ReportExecutionInput,
    V2WorkerVersion,
    V2CountryWorker,
    str,
    str,
]


class TemporaryStage12UnsupportedRequest(ValueError):
    """A temporary direct request cannot be normalized by the reviewed adapter."""

    def __init__(self, reason: str) -> None:
        super().__init__("Stage 12 does not support this request")
        self.reason = reason


class TemporaryStage12DispatchFailed(RuntimeError):
    """Modal did not acknowledge a Stage 12 coordinator submission."""

    def __init__(self, report: ComparisonReportRecord) -> None:
        super().__init__("Stage 12 comparison-run dispatch failed")
        self.report = report


class ModalReportInvoker:
    def __init__(self, environment: str) -> None:
        self._environment = environment

    async def spawn(
        self,
        *,
        worker: V2WorkerVersion,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
        parent_payload: dict[str, Any],
        observability_context: dict[str, Any] | None = None,
    ) -> str:
        function = modal.Function.from_name(
            worker.application_name,
            worker.report_coordinator_callable,
            environment_name=self._environment,
        )
        # Await only Modal's acknowledgement. The coordinator creates all
        # durable Stage 12 state after this Cloud Run request is released.
        call = await function.spawn.aio(
            report_payload,
            context_payload,
            parent_payload,
            observability_context,
        )
        invocation_id = getattr(call, "object_id", None)
        if not isinstance(invocation_id, str) or not invocation_id:
            raise RuntimeError(
                "Modal report dispatch returned no invocation identifier"
            )
        return invocation_id


class Stage12ComparisonBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        manifest_loader: V2ManifestLoader,
        store: ComparisonStore,
        invoker: ReportInvoker,
        runtime: ObservabilityRuntime,
    ) -> None:
        self._settings = settings
        self._manifest_loader = manifest_loader
        self._store = store
        self._invoker = invoker
        self._runtime = runtime

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        runtime: ObservabilityRuntime,
    ) -> Stage12ComparisonBackend:
        manifest_store = modal.Dict.from_name(
            settings.stage12_v2_manifest_name,
            environment_name=settings.stage12_v2_manifest_environment,
            create_if_missing=False,
        )
        return cls(
            settings,
            manifest_loader=V2ManifestLoader(manifest_store),
            store=Stage12PersistenceStore(settings.stage12_database_url),
            invoker=ModalReportInvoker(settings.stage12_v2_manifest_environment),
            runtime=runtime,
        )

    async def dispatch_after_production(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
        observability_id: str | None = None,
    ) -> None:
        prepared = await asyncio.to_thread(
            self._prepare_automatic_report,
            request_payload=request_payload,
            production_response=production_response,
            request_id=request_id,
        )
        if prepared is None:
            return
        parent, report, worker, country_worker, version, manifest_sha256 = prepared
        await self._spawn_report(
            parent=parent,
            report=report,
            worker=worker,
            country_worker=country_worker,
            version=version,
            manifest_sha256=manifest_sha256,
            request_id=request_id,
            production_function_call_id=parent.production_identity,
            observability_id=observability_id,
        )

    # TEMPORARY(Stage 12): Remove this operator submission seam when Stage 14
    # makes the production report submission and polling records authoritative.
    async def submit_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
        observability_id: str | None = None,
    ) -> ComparisonReportRecord:
        """Submit one direct report and return after Modal acknowledges it."""

        prepared = await asyncio.to_thread(
            self._prepare_temporary_report,
            request_payload=request_payload,
            request_id=request_id,
        )
        parent, report, worker, country_worker, version, manifest_sha256 = prepared
        return await self._spawn_report(
            parent=parent,
            report=report,
            worker=worker,
            country_worker=country_worker,
            version=version,
            manifest_sha256=manifest_sha256,
            request_id=request_id,
            production_function_call_id=None,
            observability_id=observability_id,
        )

    async def get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]:
        """Read coordinator-owned durable state without polling Modal."""

        return await asyncio.to_thread(self._get_temporary_report, evaluation_id)

    def _get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]:
        report = self._store.get_report(evaluation_id)
        if report.environment != self._settings.environment:
            raise LookupError(f"comparison report {evaluation_id} does not exist")
        return report, self._store.list_simulations(evaluation_id)

    def _resolve_report(
        self,
        *,
        request_payload: dict[str, Any],
        evaluation_id: UUID,
    ) -> tuple[
        ReportExecutionInput | None,
        V2WorkerVersion,
        V2CountryWorker,
        str,
        str,
        str | None,
    ]:
        version, worker, manifest_sha256 = self._manifest_loader.resolve(
            self._settings.stage12_v2_worker_version
        )
        adapted = adapt_annual_comparison(
            request_payload,
            evaluation_id=evaluation_id,
            worker=worker,
        )
        country = request_payload.get("country")
        bundle_country = next(
            (item for item in worker.bundle.countries if item.country == country),
            worker.bundle.countries[0],
        )
        country_worker = next(
            item for item in worker.countries if item.country == bundle_country.country
        )
        skip_reason = adapted.skip_reason.value if adapted.skip_reason else None
        return (
            adapted.report,
            worker,
            country_worker,
            version,
            manifest_sha256,
            skip_reason,
        )

    def _parent_record(
        self,
        *,
        evaluation_id: UUID,
        request_id: str,
        production_identity: str,
        incumbent_execution_id: str | None,
        worker: V2WorkerVersion,
        report: ReportExecutionInput,
        version: str,
        manifest_sha256: str,
        comparison_status: ResultComparisonStatus,
    ) -> ComparisonReportRecord:
        bundle = report.baseline.bundle
        now = datetime.now(UTC)
        return ComparisonReportRecord(
            evaluation_id=evaluation_id,
            status=ComparisonRunLifecycleStatus.PENDING,
            aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
            comparison_status=comparison_status,
            environment=self._settings.environment,
            calculation_flow="economy",
            originating_request_id=request_id,
            production_identity=production_identity,
            incumbent_execution_id=incumbent_execution_id,
            worker_version=version,
            modal_application=worker.application_name,
            report_coordinator_callable=worker.report_coordinator_callable,
            version_manifest_sha256=manifest_sha256,
            policyengine_version=bundle.policyengine_version,
            country_package_name=bundle.country_package_name,
            country_package_version=bundle.country_package_version,
            country=report.baseline.geography.country,
            dataset_identity=bundle.dataset.identity,
            dataset_uri=bundle.dataset.uri,
            data_package_name=bundle.dataset.data_package_name,
            data_package_version=bundle.dataset.data_package_version,
            data_artifact_revision=bundle.dataset.artifact_revision,
            created_at=now,
            updated_at=now,
            retention_expires_at=now + timedelta(days=30),
        )

    def _prepare_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
    ) -> PreparedReport:
        evaluation_id = uuid4()
        report, worker, country_worker, version, manifest_sha256, skip_reason = (
            self._resolve_report(
                request_payload=request_payload,
                evaluation_id=evaluation_id,
            )
        )
        if report is None:
            raise TemporaryStage12UnsupportedRequest(skip_reason or "unsupported_input")
        parent = self._parent_record(
            evaluation_id=evaluation_id,
            request_id=request_id,
            production_identity=f"direct:{evaluation_id}",
            incumbent_execution_id=None,
            worker=worker,
            report=report,
            version=version,
            manifest_sha256=manifest_sha256,
            comparison_status=ResultComparisonStatus.NOT_REQUESTED,
        )
        return parent, report, worker, country_worker, version, manifest_sha256

    def _prepare_automatic_report(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
    ) -> PreparedReport | None:
        response = json.loads(production_response)
        production_identity = (
            response.get("job_id") if isinstance(response, dict) else None
        )
        if not isinstance(production_identity, str) or not production_identity:
            raise ValueError("accepted production response has no job identifier")

        version, worker, manifest_sha256 = self._manifest_loader.resolve(
            self._settings.stage12_v2_worker_version
        )
        evaluation_id = uuid5(
            AUTOMATIC_REPORT_NAMESPACE,
            "|".join(
                (
                    self._settings.environment,
                    "economy",
                    production_identity,
                    version,
                    manifest_sha256,
                )
            ),
        )
        adapted = adapt_annual_comparison(
            request_payload,
            evaluation_id=evaluation_id,
            worker=worker,
        )
        if adapted.report is None:
            self._runtime.event(
                "stage12_comparison_run_skipped",
                attributes={
                    "request_id": request_id,
                    "production_identity": production_identity,
                    "reason": (
                        adapted.skip_reason.value
                        if adapted.skip_reason
                        else "unsupported_input"
                    ),
                },
            )
            return None
        country = request_payload.get("country")
        bundle_country = next(
            (item for item in worker.bundle.countries if item.country == country),
            worker.bundle.countries[0],
        )
        country_worker = next(
            item for item in worker.countries if item.country == bundle_country.country
        )
        parent = self._parent_record(
            evaluation_id=evaluation_id,
            request_id=request_id,
            production_identity=production_identity,
            incumbent_execution_id=production_identity,
            worker=worker,
            report=adapted.report,
            version=version,
            manifest_sha256=manifest_sha256,
            comparison_status=ResultComparisonStatus.PENDING,
        )
        return (
            parent,
            adapted.report,
            worker,
            country_worker,
            version,
            manifest_sha256,
        )

    async def _spawn_report(
        self,
        *,
        parent: ComparisonReportRecord,
        report: ReportExecutionInput,
        worker: V2WorkerVersion,
        country_worker: V2CountryWorker,
        version: str,
        manifest_sha256: str,
        request_id: str,
        production_function_call_id: str | None,
        observability_id: str | None,
    ) -> ComparisonReportRecord:
        """Return after Modal accepts the coordinator; perform no database writes."""

        artifact_prefix = (
            f"stage-12-runs/{self._settings.environment}/"
            f"{parent.created_at:%Y}/{parent.created_at:%m}/"
            f"{parent.evaluation_id}"
        )
        try:
            raw_context = self._runtime.capture_context()
        except Exception:
            raw_context = None
        captured_context = dict(raw_context) if isinstance(raw_context, dict) else {}
        resolved_observability_id = normalize_observability_id(
            observability_id or captured_context.get("observability_id")
        )
        if resolved_observability_id is not None:
            captured_context["observability_id"] = resolved_observability_id
            parent = parent.model_copy(
                update={"observability_id": resolved_observability_id}
            )
        stage_plan = (
            STAGE12_SHADOW_REPORT_STAGES
            if production_function_call_id is not None
            else STAGE12_CANONICAL_REPORT_STAGES
        )
        try:
            with self._runtime.span(
                stage_plan.name(Stage.STAGE12_ENTRY_DISPATCH),
                attributes={
                    "evaluation_id": str(parent.evaluation_id),
                    "execution_mode": (
                        "shadow"
                        if production_function_call_id is not None
                        else "authoritative"
                    ),
                    "runner_name": "stage12",
                },
            ):
                invocation_id = await self._invoker.spawn(
                    worker=worker,
                    report_payload=report.model_dump(mode="json"),
                    parent_payload=parent.model_dump(mode="json"),
                    observability_context=captured_context or None,
                    context_payload={
                        "request_id": request_id,
                        "environment": self._settings.environment,
                        "modal_environment": (
                            self._settings.stage12_v2_manifest_environment
                        ),
                        "worker_version": version,
                        "modal_application": worker.application_name,
                        "simulation_callable": (
                            country_worker.single_simulation_callable
                        ),
                        "version_manifest_sha256": manifest_sha256,
                        "bundle_manifest_sha256": worker.bundle_manifest_sha256,
                        "artifact_prefix": artifact_prefix,
                        "production_function_call_id": production_function_call_id,
                        "created_at": parent.created_at.isoformat(),
                        "retention_expires_at": (
                            parent.retention_expires_at.isoformat()
                        ),
                    },
                )
        except Exception as error:  # noqa: BLE001
            failed_at = datetime.now(UTC)
            failed = parent.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "error_code": "comparison_dispatch_failed",
                    "error_summary": type(error).__name__,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
            raise TemporaryStage12DispatchFailed(failed) from None

        acknowledged_at = datetime.now(UTC)
        acknowledged = parent.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.RUNNING,
                "coordinator_invocation_id": invocation_id,
                "started_at": acknowledged_at,
                "updated_at": acknowledged_at,
            }
        )
        self._runtime.event(
            "stage12_comparison_run_dispatched",
            attributes={
                "request_id": request_id,
                "evaluation_id": str(parent.evaluation_id),
                "production_identity": parent.production_identity,
                "worker_version": version,
                "modal_application": worker.application_name,
                "coordinator_invocation_id": invocation_id,
            },
        )
        return acknowledged
