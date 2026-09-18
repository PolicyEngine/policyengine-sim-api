"""Direct and automatic Stage 12 dispatch from the Simulation Entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import modal
from policyengine_observability import record_event
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
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
from policyengine_simulation_contract.stage12_persistence import (
    PostgresComparisonStore,
)

from policyengine_simulation_entry.config import Settings
from policyengine_simulation_entry.stage12_adapter import adapt_annual_comparison

logger = logging.getLogger(__name__)


class ComparisonStore(Protocol):
    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord: ...

    def get_report_for_production(
        self,
        *,
        environment: str,
        calculation_flow: str,
        production_identity: str,
    ) -> ComparisonReportRecord | None: ...

    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult: ...

    def replace_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord: ...

    def replace_report_result_comparison(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord: ...

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonSimulationRecord, ...]: ...

    def attach_report_invocation(
        self,
        evaluation_id,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonReportRecord: ...


class ReportInvoker(Protocol):
    async def spawn(
        self,
        *,
        worker: V2WorkerVersion,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
    ) -> str: ...


class TemporaryStage12UnsupportedRequest(ValueError):
    """A temporary direct request cannot be normalized by the reviewed adapter."""

    def __init__(self, reason: str) -> None:
        super().__init__("Stage 12 does not support this request")
        self.reason = reason


class TemporaryStage12DispatchFailed(RuntimeError):
    """Modal dispatch failed after the temporary parent record was persisted."""

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
    ) -> str:
        function = modal.Function.from_name(
            worker.application_name,
            worker.report_coordinator_callable,
            environment_name=self._environment,
        )
        # Await only Modal's acknowledgement that it accepted the invocation.
        # The coordinator and all calculation work continue outside this request.
        call = await function.spawn.aio(report_payload, context_payload)
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
    ) -> None:
        self._settings = settings
        self._manifest_loader = manifest_loader
        self._store = store
        self._invoker = invoker

    @classmethod
    def from_settings(cls, settings: Settings) -> Stage12ComparisonBackend:
        manifest_store = modal.Dict.from_name(
            settings.stage12_v2_manifest_name,
            environment_name=settings.stage12_v2_manifest_environment,
            create_if_missing=False,
        )
        return cls(
            settings,
            manifest_loader=V2ManifestLoader(manifest_store),
            store=PostgresComparisonStore(settings.stage12_database_url),
            invoker=ModalReportInvoker(settings.stage12_v2_manifest_environment),
        )

    async def dispatch_after_production(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
    ) -> None:
        prepared = await asyncio.to_thread(
            self._prepare_automatic_report,
            request_payload=request_payload,
            production_response=production_response,
            request_id=request_id,
        )
        if prepared is None:
            return
        persisted, report, worker, country_worker, version, manifest_sha256 = prepared
        await self._spawn_report(
            persisted=persisted,
            report=report,
            worker=worker,
            country_worker=country_worker,
            version=version,
            manifest_sha256=manifest_sha256,
            request_id=request_id,
            production_function_call_id=persisted.production_identity,
        )

    # TEMPORARY(Stage 12): Remove this operator submission seam when Stage 14
    # makes the production report submission and polling records authoritative.
    async def submit_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
    ) -> ComparisonReportRecord:
        """Persist and spawn one direct v2 report without waiting for computation."""

        prepared = await asyncio.to_thread(
            self._prepare_temporary_report,
            request_payload=request_payload,
            request_id=request_id,
        )
        persisted, report, worker, country_worker, version, manifest_sha256 = prepared
        return await self._spawn_report(
            persisted=persisted,
            report=report,
            worker=worker,
            country_worker=country_worker,
            version=version,
            manifest_sha256=manifest_sha256,
            request_id=request_id,
            production_function_call_id=None,
        )

    async def get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]:
        """Read temporary durable state without polling a Modal invocation."""

        return await asyncio.to_thread(self._get_temporary_report, evaluation_id)

    def _prepare_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
    ) -> tuple[
        ComparisonReportRecord,
        ReportExecutionInput,
        V2WorkerVersion,
        V2CountryWorker,
        str,
        str,
    ]:
        version, worker, manifest_sha256 = self._manifest_loader.resolve(
            self._settings.stage12_v2_worker_version
        )
        evaluation_id = uuid4()
        adapted = adapt_annual_comparison(
            request_payload,
            evaluation_id=evaluation_id,
            worker=worker,
        )
        if not adapted.eligible:
            reason = (
                adapted.skip_reason.value
                if adapted.skip_reason
                else "unsupported_input"
            )
            raise TemporaryStage12UnsupportedRequest(reason)
        report = adapted.report
        if report is None:  # pragma: no cover - guarded by eligible
            raise TemporaryStage12UnsupportedRequest("unsupported_input")

        country = request_payload.get("country")
        bundle_country = next(
            (item for item in worker.bundle.countries if item.country == country),
            worker.bundle.countries[0],
        )
        country_worker = next(
            item for item in worker.countries if item.country == bundle_country.country
        )
        now = datetime.now(UTC)
        parent = ComparisonReportRecord(
            evaluation_id=evaluation_id,
            status=ComparisonRunLifecycleStatus.PENDING,
            aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
            environment=self._settings.environment,
            calculation_flow="economy",
            originating_request_id=request_id,
            production_identity=f"direct:{evaluation_id}",
            incumbent_execution_id=None,
            worker_version=version,
            modal_application=worker.application_name,
            report_coordinator_callable=worker.report_coordinator_callable,
            version_manifest_sha256=manifest_sha256,
            policyengine_version=worker.bundle.policyengine_version,
            country_package_name=bundle_country.country_package_name,
            country_package_version=bundle_country.country_package_version,
            country=bundle_country.country,
            dataset_identity=bundle_country.default_dataset,
            dataset_uri=bundle_country.default_dataset_uri,
            data_package_name=bundle_country.data_package_name,
            data_package_version=bundle_country.data_package_version,
            data_artifact_revision=bundle_country.data_artifact_revision,
            created_at=now,
            updated_at=now,
            retention_expires_at=now + timedelta(days=30),
        )
        persisted = self._store.create_or_resolve_report(parent).record
        return (
            persisted,
            report,
            worker,
            country_worker,
            version,
            manifest_sha256,
        )

    def _get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]:
        report = self._store.get_report(evaluation_id)
        if report.environment != self._settings.environment:
            raise LookupError(f"comparison report {evaluation_id} does not exist")
        return report, self._store.list_simulations(evaluation_id)

    async def _spawn_report(
        self,
        *,
        persisted: ComparisonReportRecord,
        report: ReportExecutionInput,
        worker: V2WorkerVersion,
        country_worker: V2CountryWorker,
        version: str,
        manifest_sha256: str,
        request_id: str,
        production_function_call_id: str | None,
    ) -> ComparisonReportRecord:
        """Record an acknowledged spawn; the report continues entirely in Modal."""

        dispatch_started = datetime.now(UTC)
        dispatch_placeholder = f"dispatch-pending-{uuid4()}"
        dispatching = persisted.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.RUNNING,
                "aggregation_status": persisted.aggregation_status,
                "coordinator_invocation_id": dispatch_placeholder,
                "error_code": None,
                "error_summary": None,
                "started_at": persisted.started_at or dispatch_started,
                "updated_at": dispatch_started,
                "completed_at": None,
            }
        )
        await asyncio.to_thread(self._store.replace_report, dispatching)
        try:
            artifact_prefix = (
                f"stage-12-runs/{self._settings.environment}/"
                f"{persisted.created_at:%Y}/{persisted.created_at:%m}/"
                f"{persisted.evaluation_id}"
            )
            invocation_id = await self._invoker.spawn(
                worker=worker,
                report_payload=report.model_dump(mode="json"),
                context_payload={
                    "request_id": request_id,
                    "environment": self._settings.environment,
                    "modal_environment": (
                        self._settings.stage12_v2_manifest_environment
                    ),
                    "worker_version": version,
                    "modal_application": worker.application_name,
                    "simulation_callable": country_worker.single_simulation_callable,
                    "version_manifest_sha256": manifest_sha256,
                    "bundle_manifest_sha256": worker.bundle_manifest_sha256,
                    "artifact_prefix": artifact_prefix,
                    "production_function_call_id": production_function_call_id,
                    "created_at": persisted.created_at.isoformat(),
                    "retention_expires_at": (
                        persisted.retention_expires_at.isoformat()
                    ),
                },
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._persist_dispatch_failure(
                    dispatching,
                    error_code="comparison_dispatch_timeout",
                    error_summary="TimeoutError",
                )
            )
            raise
        # Normalize every Modal or persistence integration failure into the
        # temporary route's explicit dispatch-failure result.
        except Exception as error:  # noqa: BLE001
            failed = await self._persist_dispatch_failure(
                dispatching,
                error_code="comparison_dispatch_failed",
                error_summary=type(error).__name__,
            )
            raise TemporaryStage12DispatchFailed(failed) from None
        running = await asyncio.to_thread(
            self._store.attach_report_invocation,
            persisted.evaluation_id,
            expected_placeholder=dispatch_placeholder,
            modal_invocation_id=invocation_id,
            updated_at=datetime.now(UTC),
        )
        record_event(
            "stage12_comparison_run_dispatched",
            request_id=request_id,
            evaluation_id=str(running.evaluation_id),
            production_identity=running.production_identity,
            worker_version=version,
            modal_application=worker.application_name,
            coordinator_invocation_id=invocation_id,
        )
        return running

    async def _persist_dispatch_failure(
        self,
        dispatching: ComparisonReportRecord,
        *,
        error_code: str,
        error_summary: str,
    ) -> ComparisonReportRecord:
        failed_at = datetime.now(UTC)
        failed = await asyncio.to_thread(
            self._store.replace_report,
            dispatching.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "error_code": error_code,
                    "error_summary": error_summary,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            ),
        )
        if failed.comparison_status is ResultComparisonStatus.PENDING:
            failed = await asyncio.to_thread(
                self._store.replace_report_result_comparison,
                failed.model_copy(
                    update={
                        "comparison_status": ResultComparisonStatus.FAILED,
                        "comparison_completed_at": failed_at,
                        "comparison_error_code": error_code,
                        "comparison_error_summary": error_summary,
                        "updated_at": failed_at,
                    }
                ),
            )
        return failed

    def _prepare_automatic_report(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
    ) -> (
        tuple[
            ComparisonReportRecord,
            ReportExecutionInput,
            V2WorkerVersion,
            V2CountryWorker,
            str,
            str,
        ]
        | None
    ):
        response = json.loads(production_response)
        production_identity = (
            response.get("job_id") if isinstance(response, dict) else None
        )
        if not isinstance(production_identity, str) or not production_identity:
            raise ValueError("accepted production response has no job identifier")
        existing = self._store.get_report_for_production(
            environment=self._settings.environment,
            calculation_flow="economy",
            production_identity=production_identity,
        )
        if existing is not None and existing.status not in {
            ComparisonRunLifecycleStatus.FAILED,
            ComparisonRunLifecycleStatus.INCOMPLETE,
        }:
            return
        if existing is None:
            version, worker, manifest_sha256 = self._manifest_loader.resolve(
                self._settings.stage12_v2_worker_version
            )
            evaluation_id = uuid4()
        else:
            version, worker, _ = self._manifest_loader.resolve(existing.worker_version)
            manifest_sha256 = existing.version_manifest_sha256
            evaluation_id = existing.evaluation_id
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
        now = datetime.now(UTC)
        skipped = not adapted.eligible
        parent = ComparisonReportRecord(
            evaluation_id=evaluation_id,
            status=(
                ComparisonRunLifecycleStatus.SKIPPED
                if skipped
                else ComparisonRunLifecycleStatus.PENDING
            ),
            aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
            comparison_status=(
                ResultComparisonStatus.NOT_REQUESTED
                if skipped
                else ResultComparisonStatus.PENDING
            ),
            environment=self._settings.environment,
            calculation_flow="economy",
            originating_request_id=request_id,
            production_identity=production_identity,
            incumbent_execution_id=production_identity,
            worker_version=version,
            modal_application=worker.application_name,
            report_coordinator_callable=worker.report_coordinator_callable,
            version_manifest_sha256=manifest_sha256,
            policyengine_version=worker.bundle.policyengine_version,
            country_package_name=bundle_country.country_package_name,
            country_package_version=bundle_country.country_package_version,
            country=bundle_country.country,
            dataset_identity=bundle_country.default_dataset,
            dataset_uri=bundle_country.default_dataset_uri,
            data_package_name=bundle_country.data_package_name,
            data_package_version=bundle_country.data_package_version,
            data_artifact_revision=bundle_country.data_artifact_revision,
            error_code=(adapted.skip_reason.value if adapted.skip_reason else None),
            created_at=now,
            updated_at=now,
            completed_at=(now if skipped else None),
            retention_expires_at=now + timedelta(days=30),
        )
        persistence = (
            self._store.create_or_resolve_report(parent) if existing is None else None
        )
        persisted = persistence.record if persistence is not None else existing
        if persisted is None:  # pragma: no cover - exhaustive branch guard
            raise RuntimeError("comparison persistence returned no report")
        if persistence is not None and not persistence.created:
            if persisted.status not in {
                ComparisonRunLifecycleStatus.FAILED,
                ComparisonRunLifecycleStatus.INCOMPLETE,
            }:
                return
            version, worker, _ = self._manifest_loader.resolve(persisted.worker_version)
            manifest_sha256 = persisted.version_manifest_sha256
            evaluation_id = persisted.evaluation_id
            adapted = adapt_annual_comparison(
                request_payload,
                evaluation_id=evaluation_id,
                worker=worker,
            )
            skipped = not adapted.eligible
        if skipped:
            return
        report = adapted.report
        if report is None:  # pragma: no cover - guarded by skipped
            return
        return (
            persisted,
            report,
            worker,
            country_worker,
            version,
            manifest_sha256,
        )
