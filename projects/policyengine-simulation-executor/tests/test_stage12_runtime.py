"""Tests for independent Stage 12 simulations and report coordination."""

from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from uuid import UUID, uuid4

import pandas as pd
import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    ServiceIdentity,
    StdoutLogDestination,
    configure,
)
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.spm import (
    SPMComparisonProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
    validate_spm_calculation_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ArtifactMediaType,
    ArtifactReference,
    BundleProvenance,
    ComparisonReportPersistenceResult,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationPersistenceResult,
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
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    canonical_json_bytes,
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    _build_spm_result,
    build_aggregate_report,
    database_retry,
    run_single_simulation,
    simulation_input_sha256,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordinate_report as coordinate_report_impl,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_simulation_executor.stage12_runtime.aggregation import (
    validate_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_runtime.dependencies import (
    runtime_store,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata,
)
from policyengine_stage12_persistence import store as persistence_store_module
from sqlalchemy.exc import OperationalError

NOW = datetime(2026, 9, 15, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000002")
REFORM_ID = UUID("00000000-0000-0000-0000-000000000003")


def _spm_calculation_provenance():
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


def _bundle() -> BundleProvenance:
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


def _simulation(role: SimulationRole) -> SimulationExecutionInput:
    return SimulationExecutionInput(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=(
            BASELINE_ID if role is SimulationRole.BASELINE else REFORM_ID
        ),
        role=role,
        policy={} if role is SimulationRole.BASELINE else {"reform": {"2026": 1}},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=_bundle().dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="a" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country="us", region="us"),
        bundle=_bundle(),
    )


def _output_plan() -> Stage12OutputPlan:
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


def _planned_simulation(role: SimulationRole) -> PlannedSimulationExecutionInput:
    return PlannedSimulationExecutionInput.model_validate(
        {
            **_simulation(role).model_dump(mode="json"),
            "output_plan": _output_plan().model_dump(mode="json"),
        }
    )


def _report() -> ReportExecutionInput:
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=_simulation(SimulationRole.BASELINE),
        reform=_simulation(SimulationRole.REFORM),
        requested_aggregates=tuple(ReportAggregate),
    )


def _aggregate_payload(result=None) -> dict:
    """Return a stand-in aggregate that satisfies the artifact contract.

    Aggregators that must not run return one too, so an unexpected run shows
    up as a written aggregate instead of a rejected one.
    """
    report = _report()
    return AggregateReportArtifactPayload(
        evaluation_id=report.evaluation_id,
        requested_aggregates=report.requested_aggregates,
        bundle=report.baseline.bundle,
        result={} if result is None else result,
    ).model_dump(mode="json")


def coordinate_report(*args, **kwargs):
    kwargs.setdefault("output_plan_resolver", lambda _: _output_plan())
    return coordinate_report_impl(*args, **kwargs)


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


def _context() -> Stage12InvocationContext:
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


def _parent() -> ComparisonReportRecord:
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
        dataset_uri=_bundle().dataset.uri,
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_artifact_revision="revision",
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _child(simulation: SimulationExecutionInput):
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


def _automatic_parent() -> ComparisonReportRecord:
    return _parent().model_copy(
        update={
            "production_identity": "production-job-1",
            "incumbent_execution_id": "production-job-1",
            "comparison_status": ResultComparisonStatus.PENDING,
        }
    )


class FakeStore:
    def __init__(self):
        self.parent = None
        self.children = {}
        self.lock = Lock()

    def create_or_resolve_report(self, record):
        with self.lock:
            if self.parent is None:
                self.parent = record
                return ComparisonReportPersistenceResult(
                    record=record,
                    created=True,
                )
            return ComparisonReportPersistenceResult(
                record=self.parent,
                created=False,
            )

    def get_report(self, evaluation_id):
        assert evaluation_id == EVALUATION_ID
        assert self.parent is not None
        return self.parent

    def replace_report(self, record):
        self.parent = record
        return record

    def replace_report_result_comparison(self, record):
        self.parent = record
        return record

    def create_or_resolve_simulation(self, record):
        with self.lock:
            existing = self.children.get(record.simulation_execution_id)
            if existing is None:
                self.children[record.simulation_execution_id] = record
                return ComparisonSimulationPersistenceResult(
                    record=record, created=True
                )
            return ComparisonSimulationPersistenceResult(
                record=existing,
                created=False,
            )

    def get_simulation(self, simulation_execution_id):
        return self.children[simulation_execution_id]

    def replace_simulation(self, record):
        with self.lock:
            self.children[record.simulation_execution_id] = record
        return record

    def attach_simulation_invocation(
        self,
        simulation_execution_id,
        *,
        expected_placeholder,
        modal_invocation_id,
        updated_at,
    ):
        with self.lock:
            child = self.children[simulation_execution_id]
            assert child.modal_invocation_id == expected_placeholder
            child = child.model_copy(
                update={
                    "modal_invocation_id": modal_invocation_id,
                    "updated_at": updated_at,
                }
            )
            self.children[simulation_execution_id] = child
        return child


class FakeArtifacts:
    def __init__(self):
        self.payloads = {}
        self.aggregate_writes = []
        self.comparison_writes = []
        self.input_writes = []
        # Aggregates go through the real store, so every coordinator test
        # enforces the aggregate artifact contract that production enforces.
        self.report_store = Stage12ArtifactStore("private", store=InMemoryObjectStore())

    def write_input(self, *, prefix, simulation):
        self.input_writes.append((prefix, simulation.role))

    def add_simulation(self, simulation, frames=None):
        payload, row_identity = serialize_simulation_frames(frames or _frames())
        digest = sha256(payload).hexdigest()
        uri = f"gs://private/{simulation.role.value}.parquet"
        self.payloads[uri] = payload
        return SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=ArtifactReference(
                uri=uri,
                media_type=ArtifactMediaType.PARQUET,
                content_sha256=digest,
                size_bytes=len(payload),
            ),
            output_plan_sha256=stage12_output_plan_sha256(_output_plan()),
            row_identity=row_identity,
            bundle=simulation.bundle,
        )

    def write_simulation(
        self,
        *,
        prefix,
        simulation,
        frames,
        calculation_provenance=None,
        uk_local_authority_metadata=None,
    ):
        payload, row_identity = serialize_simulation_frames(
            frames,
            calculation_provenance=calculation_provenance,
            uk_local_authority_metadata=uk_local_authority_metadata,
        )
        uri = f"gs://private/{simulation.role.value}.parquet"
        self.payloads[uri] = payload
        descriptor = SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=ArtifactReference(
                uri=uri,
                media_type=ArtifactMediaType.PARQUET,
                content_sha256=sha256(payload).hexdigest(),
                size_bytes=len(payload),
            ),
            output_plan_sha256=stage12_output_plan_sha256(_output_plan()),
            row_identity=row_identity,
            bundle=simulation.bundle,
        )
        return descriptor.model_copy(
            update={"calculation_provenance": calculation_provenance}
        )

    def read(self, uri):
        if uri in self.payloads:
            return self.payloads[uri]
        return self.report_store.read(uri)

    def write_aggregate(self, *, prefix, payload):
        reference = self.report_store.write_aggregate(prefix=prefix, payload=payload)
        self.aggregate_writes.append(payload)
        return reference

    def write_comparison(self, *, prefix, payload):
        encoded = canonical_json_bytes(payload)
        digest = sha256(encoded).hexdigest()
        uri = "gs://private/comparison.json"
        self.payloads[uri] = encoded
        self.comparison_writes.append(payload)
        return ArtifactReference(
            uri=uri,
            media_type=ArtifactMediaType.JSON,
            content_sha256=digest,
            size_bytes=len(encoded),
        )


def _frames(value=100.0):
    return {
        "household": pd.DataFrame(
            {"household_id": [1], "household_net_income": [value]}
        ),
        "person": pd.DataFrame({"person_id": [1], "household_id": [1], "age": [40]}),
    }


def test_calculator_uses_the_current_dataset_selection_contract(monkeypatch) -> None:
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    country_module = object()
    region = type("Region", (), {"code": "us", "scoping_strategy": None})()
    selection = object()
    dataset = object()
    received: dict[str, object] = {}

    class ModelVersion:
        def resolve_entity_variables(self, _simulation):
            return {entity: list(frame.columns) for entity, frame in _frames().items()}

    class Model:
        spm_config = None
        extra_variables = {}
        tax_benefit_model_version = ModelVersion()
        output_dataset = type(
            "OutputDataset",
            (),
            {"data": type("OutputData", (), {"entity_data": _frames()})()},
        )()

        def ensure(self) -> None:
            received["ensured"] = True
            received["extras_at_ensure"] = self.extra_variables.copy()

    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(
        simulation_runtime,
        "setup_gcp_credentials",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_country_module",
        lambda country: country_module,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_region",
        lambda **kwargs: region,
    )

    def resolve_dataset(params, *, region_resolution):
        received["params"] = params
        received["region"] = region_resolution
        return selection

    def load_dataset(params, *, selection, country_module):
        received["load_selection"] = selection
        received["load_country_module"] = country_module
        return dataset

    def build_simulation(params, *, dataset, dataset_selection, **kwargs):
        received["build_dataset"] = dataset
        received["build_selection"] = dataset_selection
        return Model()

    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        resolve_dataset,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(simulation_runtime, "_build_simulation", build_simulation)

    result = worker.calculate_simulation_frames(
        _planned_simulation(SimulationRole.BASELINE)
    )

    assert "data" not in received["params"]
    assert "data_version" not in received["params"]
    assert received["region"] is region
    assert received["load_selection"] is selection
    assert received["load_country_module"] is country_module
    assert received["build_dataset"] is dataset
    assert received["build_selection"] is selection
    assert received["ensured"] is True
    assert received["extras_at_ensure"] == {"household": [], "person": []}
    assert set(result.frames) == {"household", "person"}


def test_uk_boundary_version_is_detected_from_the_unscoped_dataset() -> None:
    dataset = type(
        "Dataset",
        (),
        {
            "data": type(
                "Data",
                (),
                {
                    "entity_data": {
                        "household": pd.DataFrame(
                            {"la_code_oa": ["E06000001", "E07000026"]}
                        )
                    }
                },
            )()
        },
    )()

    metadata = detect_uk_local_authority_metadata("uk", dataset)

    assert metadata == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )


def test_country_metadata_alignment_rejects_missing_or_mismatched_uk_values() -> None:
    lad22 = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )
    lad23 = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )

    with pytest.raises(ValueError, match="missing"):
        validate_uk_local_authority_metadata("uk", None, lad22)
    with pytest.raises(ValueError, match="do not match"):
        validate_uk_local_authority_metadata("uk", lad22, lad23)
    assert validate_uk_local_authority_metadata("uk", lad22, lad22) == lad22
    assert validate_uk_local_authority_metadata("us", None, None) is None


@pytest.mark.parametrize(
    ("region_code", "routed", "accepted"),
    [
        ("uk", True, True),
        ("country/england", True, True),
        ("uk", False, False),
        ("constituency/E14001063", True, False),
        ("local_authority/E06000063", True, False),
        (None, True, False),
    ],
)
def test_uk_artifacts_without_metadata_need_a_routed_national_request(
    monkeypatch, region_code: str | None, routed: bool, accepted: bool
) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )

    if accepted:
        assert (
            validate_uk_local_authority_metadata(
                "uk", None, None, region_code=region_code
            )
            is None
        )
    else:
        with pytest.raises(ValueError, match="metadata is missing"):
            validate_uk_local_authority_metadata(
                "uk", None, None, region_code=region_code
            )


def test_single_worker_accepts_one_policy_and_persists_one_artifact() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    artifacts = FakeArtifacts()

    result = run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        store=store,
        artifacts=artifacts,
        calculator=lambda _: _frames(),
    )

    assert result["role"] == "baseline"
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert child.output_uri == result["artifact"]["uri"]
    assert artifacts.input_writes == [
        (_context().artifact_prefix, SimulationRole.BASELINE)
    ]


def test_single_worker_retains_detached_calculation_provenance() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    artifacts = FakeArtifacts()
    provenance = _spm_calculation_provenance()

    result = run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        store=store,
        artifacts=artifacts,
        calculator=lambda _: SimulationCalculation(
            frames=_frames(),
            calculation_provenance=provenance,
        ),
    )

    assert result["calculation_provenance"] == provenance.model_dump(
        mode="json", by_alias=True
    )
    assert "spm_config" not in result["calculation_provenance"]


def test_single_worker_exposes_only_a_bounded_failure() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)

    def fail(_simulation):
        msg = "country package calculation failed"
        raise RuntimeError(msg)

    with pytest.raises(
        RuntimeError, match="Stage 12 simulation execution failed"
    ) as error:
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=fail,
        )

    assert "country package calculation failed" not in str(error.value)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_execution_failed"
    assert child.error_summary == (
        "RuntimeError in stage12_calculation (baseline): "
        "country package calculation failed"
    )


def test_single_worker_persists_and_records_a_redacted_cause() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.REFORM)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    runtime = RecordingRuntime()

    def fail(_simulation):
        msg = "could not reach postgresql://runtime:database-secret@db.example/postgres"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=fail,
            runtime=runtime,
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_summary == (
        "RuntimeError in stage12_calculation (reform): "
        "could not reach postgresql://[REDACTED]@db.example/postgres"
    )
    assert "database-secret" not in child.error_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == child.error_summary


def test_single_worker_names_the_failing_artifact_write_stage() -> None:
    class FailingSimulationArtifacts(FakeArtifacts):
        def write_simulation(self, **_kwargs):
            msg = "bucket write rejected"
            raise OSError(msg)

    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FailingSimulationArtifacts(),
            calculator=lambda _: _frames(),
        )

    assert store.children[simulation.simulation_execution_id].error_summary == (
        "OSError in stage12_simulation_artifact_write (baseline): bucket write rejected"
    )


def _connection_limit_error() -> OperationalError:
    # SQLAlchemy wraps a failure to open a connection without a statement.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


class TelemetryRecordingRuntime(RecordingRuntime):
    """Record telemetry in the same ordered list as database writes."""

    def __init__(self, ordered_events: list[tuple[str, object]]) -> None:
        super().__init__()
        self.ordered_events = ordered_events

    def log(self, message, *, severity="INFO", attributes=None, error=None):
        self.ordered_events.append(
            ("telemetry", (message, dict(attributes or {}), error))
        )


def test_single_worker_builds_its_store_with_the_worker_runtime(monkeypatch) -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    store_runtimes = []

    def record_store_runtime(runtime):
        store_runtimes.append(runtime)
        return store

    monkeypatch.setattr(simulation_module, "runtime_store", record_store_runtime)
    runtime = RecordingRuntime()

    run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        artifacts=FakeArtifacts(),
        calculator=lambda _: _frames(),
        runtime=runtime,
    )

    # The runtime store reports each connection retry through this runtime.
    assert store_runtimes == [runtime]


def test_single_worker_reports_failure_before_an_unwritable_failure_record() -> None:
    ordered_events: list[tuple[str, object]] = []

    class UnwritableFailureStore(FakeStore):
        def replace_simulation(self, record):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_summary))
                raise _connection_limit_error()
            return super().replace_simulation(record)

    store = UnwritableFailureStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    calculation_error = ValueError("country column missing from household data")

    def fail(_simulation):
        raise calculation_error

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=fail,
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    original_report, failure_write, persistence_report = ordered_events
    assert original_report[0] == "telemetry"
    _message, attributes, reported_error = original_report[1]
    assert reported_error is calculation_error
    assert attributes == {
        "stage12_failure_scope": "simulation_execution",
        "simulation_role": "baseline",
    }
    assert failure_write == (
        "failure_write",
        "ValueError in stage12_calculation (baseline): "
        "country column missing from household data",
    )
    assert persistence_report[0] == "telemetry"
    assert persistence_report[1][0] == (
        "Stage 12 failure record could not be persisted"
    )
    assert isinstance(persistence_report[1][2], OperationalError)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.RUNNING


def test_single_worker_rejects_frames_that_do_not_satisfy_the_output_plan() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    frames = _frames()
    frames["person"] = frames["person"].drop(columns=["age"])

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=lambda _: frames,
        )

    assert store.children[simulation.simulation_execution_id].status is (
        ComparisonRunLifecycleStatus.FAILED
    )


def test_aggregate_combines_detached_spm_receipts() -> None:
    provenance = _spm_calculation_provenance()

    report = _report()
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"options": {"spm": {"scenario": "official"}}}
            ),
            "reform": report.reform.model_copy(
                update={"options": {"spm": {"scenario": "official"}}}
            ),
        }
    )
    baseline = (
        FakeArtifacts()
        .add_simulation(report.baseline)
        .model_copy(update={"calculation_provenance": provenance})
    )
    reform = (
        FakeArtifacts()
        .add_simulation(report.reform)
        .model_copy(update={"calculation_provenance": provenance})
    )

    result = _build_spm_result(
        report=report,
        baseline_descriptor=baseline,
        reform_descriptor=reform,
    )

    assert "spm_config" not in result
    assert result["spm_provenance"]["baseline"]["execution_count"] == 1
    assert result["spm_provenance"]["reform"]["execution_count"] == 1
    assert result["spm_provenance"]["baseline"]["receipt"]["scenario"] == "official"


def test_aggregate_stand_ins_preserve_policy_and_cliff_options(monkeypatch) -> None:
    from policyengine.outputs import labor_supply_response_is_active
    from policyengine_simulation_executor import simulation_output_builder

    report = _report()
    options = {"include_cliffs": True}
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(update={"options": options}),
            "reform": report.reform.model_copy(
                update={
                    "options": options,
                    "policy": {
                        "gov.simulation.labor_supply_responses.elasticities.income": 0.1
                    },
                }
            ),
        }
    )
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in (
            "person",
            "marital_unit",
            "family",
            "spm_unit",
            "tax_unit",
            "household",
        )
    }
    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"captured": True}

    monkeypatch.setattr(
        simulation_output_builder,
        "SimulationOutputBuilder",
        CapturingBuilder,
    )
    artifacts = FakeArtifacts()

    result = build_aggregate_report(
        report=report,
        baseline_frames=frames,
        reform_frames=frames,
        baseline_descriptor=artifacts.add_simulation(report.baseline),
        reform_descriptor=artifacts.add_simulation(report.reform),
    )

    assert result["result"] == {"captured": True}
    assert observed["simulation_params"]["include_cliffs"] is True
    assert observed["uk_local_authority_metadata"] is None
    assert labor_supply_response_is_active(
        observed["baseline"],
        observed["reform"],
        country_code="us",
    )


def test_aggregate_stand_ins_keep_bounded_policy_end_dates(monkeypatch) -> None:
    from datetime import datetime

    from policyengine_simulation_executor import simulation_output_builder

    path = "gov.simulation.labor_supply_responses.elasticities.income"
    report = _report()
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"policy": {path: {"2026-01-01.2026-12-31": 0.05}}}
            ),
            "reform": report.reform.model_copy(
                update={"policy": {path: {"2026-01-01.2027-12-31": 0.1}}}
            ),
        }
    )
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in (
            "person",
            "marital_unit",
            "family",
            "spm_unit",
            "tax_unit",
            "household",
        )
    }
    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"captured": True}

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", CapturingBuilder
    )
    artifacts = FakeArtifacts()

    build_aggregate_report(
        report=report,
        baseline_frames=frames,
        reform_frames=frames,
        baseline_descriptor=artifacts.add_simulation(report.baseline),
        reform_descriptor=artifacts.add_simulation(report.reform),
    )

    for role, stop in (("baseline", "2026-12-31"), ("reform", "2027-12-31")):
        (value,) = observed[role].policy.parameter_values
        assert value.end_date == datetime.strptime(stop, "%Y-%m-%d"), role


def _uk_report() -> ReportExecutionInput:
    uk_bundle = _bundle().model_copy(
        update={
            "country_package_name": "policyengine-uk",
            "country_package_version": "2.102.3",
        }
    )
    report_payload = _report().model_dump(mode="json")
    for role, policy in (
        ("baseline", {}),
        (
            "reform",
            {
                "gov.hmrc.income_tax.charges.CB_HITC.phase_out_start": {
                    "2026-01-01.2100-12-31": 70_000
                }
            },
        ),
    ):
        report_payload[role].update(
            {
                "policy": policy,
                "geography": {"country": "uk", "region": "uk"},
                "bundle": uk_bundle.model_dump(mode="json"),
            }
        )
    return ReportExecutionInput.model_validate(report_payload)


def test_uk_aggregate_without_spm_provenance_is_written(monkeypatch) -> None:
    from policyengine_simulation_executor import simulation_output_builder

    report = _uk_report()
    assert report.baseline.options.get("spm") is None
    uk_output = {
        **{
            key: value
            for key, value in CURRENT_SINGLE_YEAR_MACRO_RESULT.items()
            if key != "spm_provenance"
        },
        "congressional_district_impact": None,
    }

    class UKOutputBuilder:
        def __init__(self, **_values):
            pass

        def serialize(self):
            # The real builder dumps SingleYearMacroOutput, which serializes
            # the unset optional SPM receipt as an explicit null.
            return SingleYearMacroOutput.model_validate(uk_output).model_dump(
                mode="json"
            )

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", UKOutputBuilder
    )
    uk_frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()

    aggregate = build_aggregate_report(
        report=report,
        baseline_frames=uk_frames,
        reform_frames=uk_frames,
        baseline_descriptor=artifacts.add_simulation(report.baseline),
        reform_descriptor=artifacts.add_simulation(report.reform),
    )
    assert aggregate["result"]["spm_provenance"] is None

    aggregate_reference = artifacts.write_aggregate(
        prefix=_context().artifact_prefix, payload=aggregate
    )

    stored_aggregate = artifacts.read(aggregate_reference.uri)
    assert aggregate_reference.content_sha256 == sha256(stored_aggregate).hexdigest()
    assert json.loads(stored_aggregate)["result"]["spm_provenance"] is None


def test_single_worker_rejects_combined_baseline_and_reform_input() -> None:
    payload = _planned_simulation(SimulationRole.BASELINE).model_dump(mode="json")
    payload["baseline"] = {}
    payload["reform"] = {}

    with pytest.raises(ValueError, match="cannot contain baseline or reform"):
        run_single_simulation(
            payload,
            _context().model_dump(mode="json"),
            required_country="us",
            store=FakeStore(),
            artifacts=FakeArtifacts(),
            calculator=lambda _: _frames(),
        )


def test_simulation_input_digest_excludes_temporary_record_identifiers() -> None:
    original = _simulation(SimulationRole.BASELINE)
    recreated = original.model_copy(
        update={
            "evaluation_id": uuid4(),
            "simulation_execution_id": uuid4(),
        }
    )

    assert simulation_input_sha256(original) == simulation_input_sha256(recreated)


class FutureCall:
    def __init__(self, object_id, future):
        self.object_id = object_id
        self.future = future

    def get(self, *, timeout=None):
        return self.future.result(timeout=timeout)


class ImmediateCall:
    def __init__(self, object_id, result, observed_timeouts):
        self.object_id = object_id
        self.result = result
        self.observed_timeouts = observed_timeouts

    def get(self, *, timeout=None):
        self.observed_timeouts.append(timeout)
        return self.result


class ConcurrentInvoker:
    def __init__(
        self,
        artifacts,
        *,
        fail_role=None,
        incompatible=False,
        wrong_plan_digest=False,
        missing_column=False,
        production_result=None,
    ):
        self.artifacts = artifacts
        self.fail_role = fail_role
        self.incompatible = incompatible
        self.wrong_plan_digest = wrong_plan_digest
        self.missing_column = missing_column
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.intervals = {}
        self.events = []
        self.environments = []
        self.production_result = production_result
        self.production_wait_timeouts = []
        self.output_plans = []

    def spawn(self, *, simulation, environment, **_):
        parsed = PlannedSimulationExecutionInput.model_validate(simulation)
        role = parsed.role.value
        self.events.append(f"spawn:{role}")
        self.environments.append(environment)
        self.output_plans.append(parsed.output_plan)

        return self._submit(parsed, role)

    def restore(self, invocation_id):
        if invocation_id == "production-job-1":
            self.events.append("restore:production")
            return ImmediateCall(
                invocation_id,
                self.production_result,
                self.production_wait_timeouts,
            )
        role = invocation_id.removeprefix("call-")
        parsed = _planned_simulation(SimulationRole(role))
        self.events.append(f"restore:{role}")
        return self._submit(parsed, role)

    def _submit(self, parsed, role):

        def run():
            started = time.monotonic()
            time.sleep(0.05)
            if role == self.fail_role:
                msg = "child calculation failed"
                raise RuntimeError(msg)
            frames = _frames(100.0 if role == "baseline" else 120.0)
            if self.missing_column and role == "reform":
                frames["household"] = frames["household"].drop(
                    columns=["household_net_income"]
                )
            descriptor = self.artifacts.add_simulation(parsed, frames)
            if self.incompatible and role == "reform":
                descriptor = descriptor.model_copy(
                    update={
                        "row_identity": RowIdentity(
                            identifier_columns=("household.household_id",),
                            row_count=1,
                            identity_sha256="f" * 64,
                        )
                    }
                )
            if self.wrong_plan_digest and role == "reform":
                descriptor = descriptor.model_copy(
                    update={"output_plan_sha256": "f" * 64}
                )
            self.intervals[role] = (started, time.monotonic())
            return descriptor.model_dump(mode="json")

        return FutureCall(f"call-{role}", self.executor.submit(run))


def test_coordinator_starts_both_children_before_waiting_and_aggregates() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)
    observed = {}

    def aggregate(**values):
        observed.update(values)
        return _aggregate_payload()

    context = _context().model_copy(
        update={
            "environment": "production",
            "modal_environment": "main",
            "artifact_prefix": (
                "stage-12-runs/production/2026/09/00000000-0000-0000-0000-000000000001"
            ),
        }
    )
    parent = _parent().model_copy(update={"environment": "production"})
    resolver_calls = []

    def resolver(report):
        resolver_calls.append(report.evaluation_id)
        return _output_plan()

    result = coordinate_report_impl(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=aggregate,
        output_plan_resolver=resolver,
    )

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert invoker.environments == ["main", "main"]
    assert resolver_calls == [EVALUATION_ID]
    assert invoker.output_plans == [_output_plan(), _output_plan()]
    latest_start = max(interval[0] for interval in invoker.intervals.values())
    earliest_end = min(interval[1] for interval in invoker.intervals.values())
    assert latest_start < earliest_end
    assert result["artifact"]["uri"] == (
        f"gs://private/{context.artifact_prefix}/reports/aggregate.json"
    )
    assert observed["baseline_frames"]["household"][
        "household_net_income"
    ].tolist() == [100.0]
    assert observed["reform_frames"]["household"]["household_net_income"].tolist() == [
        120.0
    ]
    assert observed["uk_local_authority_metadata"] is None
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.aggregation_status is ComparisonRunAggregationStatus.SUCCEEDED
    assert store.parent.coordinator_invocation_id == "coordinator-1"


def test_duplicate_coordinator_submission_does_not_start_duplicate_children() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    context = _context()
    parent = _parent()
    first_invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=first_invoker,
        aggregator=lambda **_: _aggregate_payload(),
    )
    duplicate_invoker = ConcurrentInvoker(artifacts)
    duplicate = coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-2",
        store=store,
        artifacts=artifacts,
        invoker=duplicate_invoker,
        aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
    )

    assert duplicate == {
        "deduplicated": True,
        "evaluation_id": str(EVALUATION_ID),
        "status": "succeeded",
    }
    assert duplicate_invoker.events == []


def test_coordinator_measures_output_and_child_input_planning() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    runtime = RecordingRuntime()

    coordinate_report(
        _report().model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        application_name=_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=ConcurrentInvoker(artifacts),
        aggregator=lambda **_: _aggregate_payload(),
        runtime=runtime,
    )

    planning_operations = [
        operation
        for operation in runtime.operations
        if operation[0]
        in {
            "stage12_coordinator_preparation",
            "stage12_output_planning",
            "stage12_child_input_planning",
        }
    ]
    assert planning_operations == [
        ("stage12_coordinator_preparation", {}),
        ("stage12_output_planning", {"country": "us"}),
        ("stage12_child_input_planning", {"simulation_role": "baseline"}),
        ("stage12_child_input_planning", {"simulation_role": "reform"}),
    ]


def test_coordinator_planning_operations_preserve_observability_id() -> None:
    output = io.StringIO()
    runtime = configure(
        ObservabilityConfig(
            service=ServiceIdentity(
                name="stage12-test",
                namespace="policyengine.api-v1",
                version="test",
                role="coordinator",
            ),
            deployment=DeploymentIdentity(
                environment="test",
                platform="local",
                region="us-central1",
                instance_id="test-process",
            ),
            logging=LoggingConfig(
                destinations=(StdoutLogDestination(),),
            ),
            otel=OTelConfig(enabled=False),
            dispatch_attribute_keys=frozenset({"observability_id"}),
        )
    )
    runtime._delivery._stdout = output
    observability_id = "00000000-0000-4000-8000-000000000004"
    artifacts = FakeArtifacts()

    try:
        with runtime.operation(
            "stage12_report",
            remote_context={
                "captured_at": NOW.isoformat(),
                "observability_id": observability_id,
            },
        ):
            coordinate_report(
                _report().model_dump(mode="json"),
                _context().model_dump(mode="json"),
                _parent().model_dump(mode="json"),
                application_name=_context().modal_application,
                coordinator_invocation_id="coordinator-1",
                store=FakeStore(),
                artifacts=artifacts,
                invoker=ConcurrentInvoker(artifacts),
                aggregator=lambda **_: _aggregate_payload(),
                runtime=runtime,
            )
    finally:
        runtime.shutdown()

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    planning_names = {
        "stage12_coordinator_preparation",
        "stage12_output_planning",
        "stage12_child_input_planning",
    }
    planning_records = [
        record for record in records if record.get("operation.name") in planning_names
    ]
    assert len(planning_records) == 4
    assert all(
        record["attributes"]["observability_id"] == observability_id
        for record in planning_records
    )


def test_coordinator_compares_automatic_run_after_successful_aggregation() -> None:
    store = FakeStore()
    parent = _automatic_parent()
    artifacts = FakeArtifacts()
    production_result = {"budget": {"total": 100.0, "count": 2}}
    stage12_result = {"budget": {"total": 101.0, "count": 2}}
    invoker = ConcurrentInvoker(
        artifacts,
        production_result=production_result,
    )
    context = _context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: _aggregate_payload(stage12_result),
    )

    assert invoker.events[-1] == "restore:production"
    assert invoker.production_wait_timeouts == [900]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.comparison_status is ResultComparisonStatus.DIFFERENT
    assert store.parent.comparison_output_uri == "gs://private/comparison.json"
    receipt = artifacts.comparison_writes[0]
    assert receipt["difference_count"] == 1
    assert receipt["differences"][0]["path"] == "/budget/total"
    assert receipt["differences"][0]["production_value"] == 100.0
    assert receipt["differences"][0]["stage12_value"] == 101.0
    assert "production_result" not in receipt
    assert "stage12_result" not in receipt


def test_comparison_failure_does_not_change_successful_stage12_report() -> None:
    store = FakeStore()
    parent = _automatic_parent()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts, production_result="not-an-object")
    context = _context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: _aggregate_payload({"budget": 100}),
    )

    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.aggregation_status is ComparisonRunAggregationStatus.SUCCEEDED
    assert store.parent.comparison_status is ResultComparisonStatus.FAILED
    assert store.parent.comparison_error_code == "result_comparison_failed"
    assert artifacts.comparison_writes == []


class UnavailableProductionCall:
    object_id = "production-job-1"

    def get(self, *, timeout=None):
        msg = (
            "production result unavailable at "
            "https://storage.example.com/result.json?X-Goog-Signature=signed-value"
        )
        raise ConnectionError(msg)


class UnavailableProductionInvoker(ConcurrentInvoker):
    def restore(self, invocation_id):
        if invocation_id == "production-job-1":
            return UnavailableProductionCall()
        return super().restore(invocation_id)


def test_comparison_failure_stores_a_redacted_cause() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    context = _context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        _automatic_parent().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=UnavailableProductionInvoker(artifacts),
        aggregator=lambda **_: _aggregate_payload({"budget": 100}),
    )

    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.comparison_status is ResultComparisonStatus.FAILED
    assert store.parent.comparison_error_summary == (
        "ConnectionError: production result unavailable at "
        "https://storage.example.com/result.json?[REDACTED]"
    )


def test_repeated_submission_does_not_retry_failed_comparison() -> None:
    store = FakeStore()
    parent = _automatic_parent()
    artifacts = FakeArtifacts()
    context = _context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    first_invoker = ConcurrentInvoker(artifacts, production_result="not-an-object")
    coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=first_invoker,
        aggregator=lambda **_: _aggregate_payload({"budget": {"total": 100.0}}),
    )
    failed_at = store.parent.comparison_completed_at
    failed_summary = store.parent.comparison_error_summary

    second_invoker = ConcurrentInvoker(
        artifacts,
        production_result={"budget": {"total": 100.0}},
    )
    result = coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-2",
        store=store,
        artifacts=artifacts,
        invoker=second_invoker,
        aggregator=lambda **_: _aggregate_payload({"budget": {"total": 100.0}}),
    )

    assert result == {
        "deduplicated": True,
        "evaluation_id": str(EVALUATION_ID),
        "status": "succeeded",
    }
    assert second_invoker.events == []
    assert store.parent.comparison_status is ResultComparisonStatus.FAILED
    assert store.parent.comparison_completed_at == failed_at
    assert store.parent.comparison_error_code == "result_comparison_failed"
    assert store.parent.comparison_error_summary == failed_summary
    assert artifacts.comparison_writes == []


def test_coordinator_never_writes_partial_aggregate_when_a_child_fails() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts, fail_role="reform")

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        )

    assert "child calculation failed" not in str(error.value)

    expected_summary = (
        "RuntimeError in stage12_child_wait (reform): child calculation failed"
    )
    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == expected_summary
    failed_child = store.children[REFORM_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_invocation_failed"
    assert failed_child.error_summary == expected_summary


@pytest.mark.parametrize(
    "invoker",
    [
        lambda artifacts: ConcurrentInvoker(artifacts, wrong_plan_digest=True),
        lambda artifacts: ConcurrentInvoker(artifacts, missing_column=True),
    ],
)
def test_coordinator_rejects_artifacts_that_do_not_satisfy_the_shared_plan(
    invoker,
) -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker(artifacts),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        )

    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED


def test_coordinator_records_failure_when_child_persistence_fails() -> None:
    class FailingChildStore(FakeStore):
        def create_or_resolve_simulation(self, record):
            msg = "database rejected the child record"
            raise RuntimeError(msg)

    store = FailingChildStore()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=ConcurrentInvoker(FakeArtifacts()),
        )

    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "report_coordination_failed"
    assert store.parent.error_summary == (
        "RuntimeError in stage12_child_state_create: database rejected the child record"
    )


class RefusingDispatchInvoker:
    def __init__(self, dispatch_error):
        self.dispatch_error = dispatch_error
        self.events = []

    def spawn(self, *, simulation, **_):
        role = PlannedSimulationExecutionInput.model_validate(simulation).role.value
        self.events.append(f"spawn:{role}")
        raise self.dispatch_error

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


def test_coordinator_records_a_redacted_dispatch_failure_cause() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = RefusingDispatchInvoker(
        ConnectionError("Modal rejected the call with token=opaque-token")
    )
    runtime = RecordingRuntime()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            runtime=runtime,
        )

    expected_summary = (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal rejected the call with token=[REDACTED]"
    )
    assert invoker.events == ["spawn:baseline"]
    assert artifacts.aggregate_writes == []
    failed_child = store.children[BASELINE_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_dispatch_failed"
    assert failed_child.error_summary == expected_summary
    assert store.parent.error_code == "report_coordination_failed"
    assert store.parent.error_summary == expected_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == expected_summary


class WorkerRecordedFailureCall:
    """A child whose worker persisted its cause before Modal raised."""

    def __init__(self, object_id, store, simulation_execution_id, worker_summary):
        self.object_id = object_id
        self.store = store
        self.simulation_execution_id = simulation_execution_id
        self.worker_summary = worker_summary

    def get(self, *, timeout=None):
        child = self.store.children[self.simulation_execution_id]
        self.store.children[self.simulation_execution_id] = child.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.FAILED,
                "error_code": "simulation_execution_failed",
                "error_summary": self.worker_summary,
                "completed_at": NOW,
            }
        )
        msg = "Stage 12 simulation execution failed"
        raise RuntimeError(msg)


class UnreachedCall:
    def __init__(self, object_id):
        self.object_id = object_id

    def get(self, *, timeout=None):
        msg = "the coordinator waits for baseline first"
        raise AssertionError(msg)


class WorkerRecordedFailureInvoker:
    def __init__(self, store, worker_summary):
        self.store = store
        self.worker_summary = worker_summary

    def spawn(self, *, simulation, **_):
        planned = PlannedSimulationExecutionInput.model_validate(simulation)
        if planned.role is SimulationRole.BASELINE:
            return WorkerRecordedFailureCall(
                "call-baseline",
                self.store,
                planned.simulation_execution_id,
                self.worker_summary,
            )
        return UnreachedCall("call-reform")

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


def test_coordinator_keeps_the_cause_recorded_by_a_failed_worker() -> None:
    store = FakeStore()
    worker_summary = (
        "ValueError in stage12_calculation (baseline): "
        "Variable 'country' not found in household data"
    )
    runtime = RecordingRuntime()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=WorkerRecordedFailureInvoker(store, worker_summary),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            runtime=runtime,
        )

    failed_child = store.children[BASELINE_ID]
    assert failed_child.error_code == "simulation_execution_failed"
    assert failed_child.error_summary == worker_summary
    assert store.parent.error_code == "report_coordination_failed"
    assert store.parent.error_summary == worker_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == worker_summary


def test_coordinator_names_the_stage_that_rejected_the_aggregate() -> None:
    class RejectingAggregateArtifacts(FakeArtifacts):
        def write_aggregate(self, *, prefix, payload):
            SPMComparisonProvenance.model_validate(payload["result"]["spm_provenance"])

    store = FakeStore()
    artifacts = RejectingAggregateArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts),
            aggregator=lambda **_: {"result": {"spm_provenance": None}},
        )

    assert store.parent.error_summary == (
        "ValidationError in stage12_aggregate_artifact_write: "
        "1 validation error for SPMComparisonProvenance: "
        "Input should be a valid dictionary or instance "
        "of SPMComparisonProvenance [model_type]"
    )


def test_coordinator_builds_its_store_with_the_coordinator_runtime(
    monkeypatch,
) -> None:
    store = FakeStore()
    store_runtimes = []

    def record_store_runtime(runtime):
        store_runtimes.append(runtime)
        return store

    monkeypatch.setattr(coordination_module, "runtime_store", record_store_runtime)
    artifacts = FakeArtifacts()
    runtime = RecordingRuntime()

    coordinate_report(
        _report().model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        application_name=_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        artifacts=artifacts,
        invoker=ConcurrentInvoker(artifacts),
        aggregator=lambda **_: _aggregate_payload(),
        runtime=runtime,
    )

    # The runtime store reports each connection retry through this runtime.
    assert store_runtimes == [runtime]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


def test_coordinator_does_not_deduplicate_against_its_own_claim_after_a_lost_commit() -> (  # noqa: E501  # the test name spells out the lost-commit scenario
    None
):
    # SQLAlchemy raises a failed COMMIT without a statement, like a refused
    # connection, although the claim may already be durable.
    lost_commit_reply = OperationalError(
        None,
        None,
        Exception("server closed the connection unexpectedly"),
    )

    class LostCommitReplyStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.claim_attempts = 0

        def create_or_resolve_report(self, record):
            self.claim_attempts += 1
            claim = super().create_or_resolve_report(record)
            if self.claim_attempts == 1:
                raise lost_commit_reply
            return claim

    store = LostCommitReplyStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(OperationalError) as error:
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
        )

    # A retry would find this coordinator's own running claim and return a
    # deduplicated success without dispatching either simulation.
    assert error.value is lost_commit_reply
    assert store.claim_attempts == 1
    assert invoker.events == []


class ScriptedResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        return self.row


class ScriptedConnection:
    """Answer each statement with the next scripted row."""

    def __init__(self, rows, *, commit_error=None):
        self.rows = list(rows)
        self.statement_count = 0
        self.commit_error = commit_error

    def __enter__(self):
        return self

    def __exit__(self, *_exception_details):
        return None

    @contextmanager
    def begin(self):
        yield
        if self.commit_error is not None:
            raise self.commit_error

    def execute(self, _statement):
        self.statement_count += 1
        return ScriptedResult(self.rows.pop(0))


class BusyPoolerEngine:
    """Refuse the first connections, as a session pooler at its limit does."""

    def __init__(self, rows, *, refused_connections=0, commit_error=None):
        self.connection = ScriptedConnection(rows, commit_error=commit_error)
        self.refused_connections = refused_connections
        self.connection_attempts = 0

    def connect(self):
        self.connection_attempts += 1
        if self.connection_attempts <= self.refused_connections:
            raise _connection_limit_error()
        return self.connection


def _runtime_store_over(engine, monkeypatch, runtime):
    monkeypatch.setattr(database_retry, "INITIAL_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        persistence_store_module,
        "create_stage12_engine",
        lambda _database_url: engine,
    )
    return runtime_store(runtime)


def test_runtime_store_retries_a_refused_connection_for_child_state_creation(
    monkeypatch,
) -> None:
    ordered_events: list[tuple[str, object]] = []
    child = _child(_planned_simulation(SimulationRole.BASELINE))
    engine = BusyPoolerEngine(
        [_parent().model_dump(mode="python"), child.model_dump(mode="python")],
        refused_connections=1,
    )
    store = _runtime_store_over(
        engine, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    result = store.create_or_resolve_simulation(child)

    assert result.created is True
    assert result.record == child
    assert engine.connection_attempts == 2
    assert engine.connection.statement_count == 2
    [(event_kind, (message, attributes, error))] = ordered_events
    assert event_kind == "telemetry"
    assert message == "Stage 12 database connection unavailable; retrying"
    assert attributes["stage12_database_operation"] == "create_or_resolve_simulation"
    assert attributes["stage12_database_failed_attempt"] == 1
    assert isinstance(error, OperationalError)


def test_runtime_store_does_not_retry_a_failed_commit(monkeypatch) -> None:
    ordered_events: list[tuple[str, object]] = []
    lost_commit_reply = OperationalError(
        None,
        None,
        Exception("server closed the connection unexpectedly"),
    )
    report = _parent()
    engine = BusyPoolerEngine(
        [report.model_dump(mode="python")],
        commit_error=lost_commit_reply,
    )
    store = _runtime_store_over(
        engine, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    with pytest.raises(OperationalError) as failure:
        store.create_or_resolve_report(report)

    assert failure.value is lost_commit_reply
    assert engine.connection_attempts == 1
    assert engine.connection.statement_count == 1
    assert ordered_events == []


def test_coordinator_reports_failure_before_an_unwritable_failure_record() -> None:
    ordered_events: list[tuple[str, object]] = []
    aggregation_error = ValueError("aggregate failed validation")

    class UnwritableFailureStore(FakeStore):
        def replace_report(self, record):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_summary))
                raise _connection_limit_error()
            return super().replace_report(record)

    def fail_aggregation(**_):
        raise aggregation_error

    store = UnwritableFailureStore()
    artifacts = FakeArtifacts()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts),
            aggregator=fail_aggregation,
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    assert error.value.__cause__ is None
    original_report, failure_write, persistence_report = ordered_events
    assert original_report[1][1] == {"stage12_failure_scope": "report_coordination"}
    assert original_report[1][2] is aggregation_error
    assert failure_write == (
        "failure_write",
        "ValueError in stage12_aggregation: aggregate failed validation",
    )
    assert isinstance(persistence_report[1][2], OperationalError)
    assert store.parent.status is ComparisonRunLifecycleStatus.RUNNING


def test_coordinator_reports_a_child_failure_once_when_its_record_is_unwritable() -> (
    None
):
    ordered_events: list[tuple[str, object]] = []

    class UnreadableChildStore(FakeStore):
        def get_simulation(self, simulation_execution_id):
            ordered_events.append(("child_read", str(simulation_execution_id)))
            raise _connection_limit_error()

    store = UnreadableChildStore()
    artifacts = FakeArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts, fail_role="baseline"),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    failure_reports = [
        event[1]
        for event in ordered_events
        if event[0] == "telemetry" and event[1][0] == "Stage 12 execution failed"
    ]
    assert [report[1] for report in failure_reports] == [
        {"stage12_failure_scope": "child_wait", "simulation_role": "baseline"}
    ]
    assert ordered_events[0][0] == "telemetry"
    assert ordered_events[1] == ("child_read", str(BASELINE_ID))
    # The report record is still written after the child record could not be.
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == (
        "RuntimeError in stage12_child_wait (baseline): child calculation failed"
    )


def test_coordinator_reports_a_child_dispatch_failure_once() -> None:
    ordered_events: list[tuple[str, object]] = []
    dispatch_error = ConnectionError("Modal refused the dispatch")
    store = FakeStore()
    invoker = RefusingDispatchInvoker(dispatch_error)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    failure_reports = [
        event[1]
        for event in ordered_events
        if event[0] == "telemetry" and event[1][0] == "Stage 12 execution failed"
    ]
    assert [(attributes, error) for _message, attributes, error in failure_reports] == [
        (
            {"stage12_failure_scope": "child_dispatch", "simulation_role": "baseline"},
            dispatch_error,
        )
    ]
    assert invoker.events == ["spawn:baseline"]
    baseline_child = store.children[BASELINE_ID]
    assert baseline_child.status is ComparisonRunLifecycleStatus.FAILED
    assert baseline_child.error_code == "simulation_dispatch_failed"
    expected_summary = (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal refused the dispatch"
    )
    assert baseline_child.error_summary == expected_summary
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == expected_summary


def test_coordinator_rejects_incompatible_row_identity() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts, incompatible=True)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        )
    assert artifacts.aggregate_writes == []


def test_coordinator_reuses_matching_successful_child() -> None:
    report = _report()
    store = FakeStore()
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    baseline = _child(report.baseline).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": baseline_descriptor.artifact.uri,
            "output_sha256": baseline_descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": baseline_descriptor.row_identity.identifier_columns,
            "row_count": baseline_descriptor.row_identity.row_count,
            "row_identity_sha256": baseline_descriptor.row_identity.identity_sha256,
            "completed_at": NOW,
        }
    )
    store.children[report.baseline.simulation_execution_id] = baseline
    invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        report.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        application_name=_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: _aggregate_payload(),
    )

    assert invoker.events == ["spawn:reform"]


def test_coordinator_resumes_a_matching_running_child_invocation() -> None:
    report = _report()
    store = FakeStore()
    artifacts = FakeArtifacts()
    store.children[report.baseline.simulation_execution_id] = _child(
        report.baseline
    ).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "modal_invocation_id": "call-baseline",
            "started_at": NOW,
        }
    )
    invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        report.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        application_name=_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: _aggregate_payload(),
    )

    assert invoker.events == ["restore:baseline", "spawn:reform"]


class TimeoutCall:
    def __init__(self, object_id):
        self.object_id = object_id

    def get(self, *, timeout=None):
        msg = "child result was not ready"
        raise TimeoutError(msg)


class TimeoutInvoker:
    def __init__(self):
        self.events = []

    def spawn(self, *, simulation, **_):
        role = PlannedSimulationExecutionInput.model_validate(simulation).role.value
        self.events.append(f"spawn:{role}")
        return TimeoutCall(f"call-{role}")

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


def test_coordinator_records_bounded_timeout_without_an_aggregate() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = TimeoutInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        )

    assert "child result was not ready" not in str(error.value)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert artifacts.aggregate_writes == []
    expected_summary = (
        "TimeoutError in stage12_child_wait (baseline): child result was not ready"
    )
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == expected_summary
    timed_out_child = store.children[BASELINE_ID]
    assert timed_out_child.status is ComparisonRunLifecycleStatus.FAILED
    assert timed_out_child.error_summary == expected_summary
