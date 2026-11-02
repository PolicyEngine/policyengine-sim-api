"""Inputs, records, runtimes and failure checks shared by the Stage 12 tests."""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
    validate_spm_calculation_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    BundleProvenance,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    EntityOutputPlan,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportExecutionInput,
    ReportOutputRequirements,
    ResultComparisonStatus,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
)
from policyengine_simulation_executor.stage12_runtime import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    simulation_input_sha256,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordinate_report as coordinate_report_impl,
)
from sqlalchemy.exc import OperationalError

NOW = datetime(2026, 9, 15, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000002")
REFORM_ID = UUID("00000000-0000-0000-0000-000000000003")


def spm_calculation_provenance():
    selection = SPMSelection(
        forecast_content_sha256="f" * 64,
        scenario="official",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )
    receipt = build_spm_provenance(
        forecast_id="forecast-1",
        forecast_sha256="f" * 64,
        selection=selection,
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "6.2.1",
                "policyengine-core": "3.32.10",
                "policyengine-us": "2.2.1",
                "spm-calculator": "1.0.0",
            }
        ),
    )
    return validate_spm_calculation_provenance(config=selection, receipt=receipt)


def bundle_provenance() -> BundleProvenance:
    return BundleProvenance(
        policyengine_version="5.2.0",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        dataset=DatasetProvenance(
            identity="populace_us_2024",
            uri="hf://policyengine/data/populace_us_2024.h5@revision",
            artifact_revision="revision",
            data_package_name="populace-data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )


def simulation_execution_input(role: SimulationRole) -> SimulationExecutionInput:
    return SimulationExecutionInput(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=(
            BASELINE_ID if role is SimulationRole.BASELINE else REFORM_ID
        ),
        role=role,
        policy={} if role is SimulationRole.BASELINE else {"reform": {"2026": 1}},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=bundle_provenance().dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="a" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country="us", region="us"),
        bundle=bundle_provenance(),
    )


def shared_output_plan() -> Stage12OutputPlan:
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=False,
        ),
        entities=(
            EntityOutputPlan(
                entity="household",
                materialized_variables=("household_id", "household_net_income"),
            ),
            EntityOutputPlan(
                entity="person",
                materialized_variables=("age", "household_id", "person_id"),
            ),
        ),
    )


def planned_simulation(role: SimulationRole) -> PlannedSimulationExecutionInput:
    return PlannedSimulationExecutionInput.model_validate(
        {
            **simulation_execution_input(role).model_dump(mode="json"),
            "output_plan": shared_output_plan().model_dump(mode="json"),
        }
    )


def report_execution_input() -> ReportExecutionInput:
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=simulation_execution_input(SimulationRole.BASELINE),
        reform=simulation_execution_input(SimulationRole.REFORM),
        requested_aggregates=tuple(ReportAggregate),
    )


def aggregate_payload(result=None) -> dict:
    """Return a stand-in aggregate that satisfies the artifact contract.

    Aggregators that must not run return one too, so an unexpected run shows
    up as a written aggregate instead of a rejected one.
    """
    report = report_execution_input()
    return AggregateReportArtifactPayload(
        evaluation_id=report.evaluation_id,
        requested_aggregates=report.requested_aggregates,
        bundle=report.baseline.bundle,
        result={} if result is None else result,
    ).model_dump(mode="json")


def coordinate_report(*payloads, application_name, coordinator_invocation_id, **seams):
    seams.setdefault("output_plan_resolver", lambda _: shared_output_plan())
    return coordinate_report_impl(
        *payloads,
        invocation=CoordinatorInvocation(
            application_name=application_name,
            invocation_id=coordinator_invocation_id,
        ),
        dependencies=CoordinatorDependencies(**seams),
    )


class RecordingRuntime:
    def __init__(self) -> None:
        self.operations: list[tuple[str, dict[str, object]]] = []
        self.recorded_exceptions: list[Exception] = []

    @contextmanager
    def operation(self, name, *, attributes=None, **_kwargs):
        self.operations.append((name, dict(attributes or {})))
        yield

    def span(self, *_args, **_kwargs):
        return nullcontext()

    def capture_context(self):
        return {}

    def record_exception(self, error, *, handled, status_code=None):
        assert handled
        self.recorded_exceptions.append(error)


def invocation_context() -> Stage12InvocationContext:
    return Stage12InvocationContext(
        request_id="request-1",
        environment="staging",
        modal_environment="staging",
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="b" * 64,
        bundle_manifest_sha256="b" * 64,
        artifact_prefix=(
            "stage-12-runs/staging/2026/09/00000000-0000-0000-0000-000000000001"
        ),
        created_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def parent_record() -> ComparisonReportRecord:
    return ComparisonReportRecord(
        evaluation_id=EVALUATION_ID,
        status=ComparisonRunLifecycleStatus.PENDING,
        aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
        environment="staging",
        calculation_flow="economy",
        originating_request_id="request-1",
        production_identity=f"direct:{EVALUATION_ID}",
        incumbent_execution_id=None,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        report_coordinator_callable="coordinate_report",
        version_manifest_sha256="b" * 64,
        policyengine_version="5.2.0",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        country="us",
        dataset_identity="populace_us_2024",
        dataset_uri=bundle_provenance().dataset.uri,
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_artifact_revision="revision",
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def child_record(simulation: SimulationExecutionInput):
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonSimulationRecord,
    )

    return ComparisonSimulationRecord(
        simulation_execution_id=simulation.simulation_execution_id,
        evaluation_id=simulation.evaluation_id,
        role=simulation.role,
        input_sha256=simulation_input_sha256(simulation),
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="b" * 64,
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def automatic_parent_record() -> ComparisonReportRecord:
    return parent_record().model_copy(
        update={
            "production_identity": "production-job-1",
            "incumbent_execution_id": "production-job-1",
            "comparison_status": ResultComparisonStatus.PENDING,
        }
    )


class _StoppedAfterLoadingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


def run_until_dataset_loaded(
    monkeypatch, *, region, selection, baked_folder, planned=None
):
    """Run a Stage 12 calculation up to its dataset load; return what it did."""
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.stage12_cache import state_datasets
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    loads = []
    lookups = []

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        loads.append({"selection": selection, "data_folder": data_folder})
        return object()

    def lookup(**kwargs):
        lookups.append(kwargs)
        return baked_folder

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _country: object()
    )
    monkeypatch.setattr(simulation_runtime, "resolve_region", lambda **_: region)
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(state_datasets, "baked_state_data_folder", lookup)
    monkeypatch.setattr(
        worker, "detect_uk_run_dataset_metadata", lambda *_a, **_k: None
    )
    monkeypatch.setattr(worker, "build_stage12_simulation", stop_after_loading)

    planned = planned or planned_simulation(SimulationRole.BASELINE)
    with pytest.raises(_StoppedAfterLoadingError):
        worker.calculate_simulation_frames(planned)
    return SimpleNamespace(loads=loads, lookups=lookups, year=planned.year)


def connection_limit_error() -> OperationalError:
    # SQLAlchemy wraps a failure to open a connection without a statement.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


CORRELATION_SUFFIX = re.compile(r" \(correlation_id=(?P<correlation_id>[0-9a-f]+)\)\Z")


def summary_correlation_id(error_summary: str) -> str:
    """Return the correlation ID that ends a redacted failure summary."""
    match = CORRELATION_SUFFIX.search(error_summary)
    assert match is not None, error_summary
    return match["correlation_id"]


def summary_failure_cause(error_summary: str) -> str:
    """Return the redacted cause before a failure summary's correlation ID."""
    summary_correlation_id(error_summary)
    return CORRELATION_SUFFIX.sub("", error_summary)


class TelemetryRecordingRuntime(RecordingRuntime):
    """Record telemetry in the same ordered list as database writes."""

    def __init__(self, ordered_events: list[tuple[str, object]]) -> None:
        super().__init__()
        self.ordered_events = ordered_events

    def log(self, message, *, severity="INFO", attributes=None, error=None):
        self.ordered_events.append(
            ("telemetry", (message, dict(attributes or {}), error))
        )
