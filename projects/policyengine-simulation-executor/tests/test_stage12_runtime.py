"""Tests for independent Stage 12 simulations and report coordination."""

from __future__ import annotations

import importlib
import io
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from types import SimpleNamespace
from typing import ClassVar, override
from uuid import UUID, uuid4

import pandas as pd
import pytest
from fixtures.fake_modal import install_fake_modal
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
    SPMInputError,
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
    CoordinatorDependencies,
    CoordinatorInvocation,
    RetainedSimulationOutput,
    SimulationCalculation,
    Stage12SegmentRunner,
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
from policyengine_simulation_executor.stage12_runtime.comparison import (
    PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS,
)
from policyengine_simulation_executor.stage12_runtime.dependencies import (
    runtime_store,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
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


def coordinate_report(*payloads, application_name, coordinator_invocation_id, **seams):
    seams.setdefault("output_plan_resolver", lambda _: _output_plan())
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

    def replace_simulation_if_status(self, record, *, expected_status):
        with self.lock:
            current = self.children[record.simulation_execution_id]
            if current.status is not expected_status:
                return current, False
            self.children[record.simulation_execution_id] = record
        return record, True

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
        extra_variables: ClassVar[dict[str, list[str]]] = {}
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
        nullcontext,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_region",
        lambda **_kwargs: region,
    )

    def resolve_dataset(params, *, region_resolution):
        received["params"] = params
        received["region"] = region_resolution
        return selection

    def load_dataset(_params, *, selection, country_module):
        received["load_selection"] = selection
        received["load_country_module"] = country_module
        return dataset

    def build_simulation(_params, *, dataset, dataset_selection, **_kwargs):
        received["build_dataset"] = dataset
        received["build_selection"] = dataset_selection
        return Model()

    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        resolve_dataset,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(worker, "build_stage12_simulation", build_simulation)

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
    monkeypatch, region_code: str | None, *, routed: bool, accepted: bool
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


def test_single_worker_routes_eligible_us_calculation_to_segment_runner() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    received = []

    def segmented_calculator(value, cancellation_requested):
        received.append(value)
        assert cancellation_requested() is False
        return SimulationCalculation(frames=_frames())

    run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        store=store,
        artifacts=FakeArtifacts(),
        calculator=lambda _: pytest.fail("monolithic calculator was selected"),
        segmented_calculator=segmented_calculator,
    )

    assert received == [simulation]


def test_single_worker_honors_explicit_segmentation_opt_out() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={"options": {"segmented": False}}
    )
    store.children[simulation.simulation_execution_id] = _child(simulation)
    segmented_calls = []

    def unexpected_segmented(value, cancellation_requested):
        segmented_calls.append(value)
        return SimulationCalculation(frames=_frames())

    run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        store=store,
        artifacts=FakeArtifacts(),
        calculator=lambda _: _frames(),
        segmented_calculator=unexpected_segmented,
    )

    assert segmented_calls == []


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
    assert _failure_cause(child.error_summary) == (
        "RuntimeError in stage12_calculation (baseline): "
        "country package calculation failed"
    )


def test_single_worker_persists_and_records_a_redacted_cause() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
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
    assert _failure_cause(child.error_summary) == (
        "RuntimeError in stage12_calculation (baseline): "
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

    assert _failure_cause(
        store.children[simulation.simulation_execution_id].error_summary
    ) == (
        "OSError in stage12_simulation_artifact_write (baseline): bucket write rejected"
    )


def test_single_worker_preserves_typed_failure_values() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)

    def fail(_simulation):
        msg = "SPM_YEAR_UNAVAILABLE"
        raise SPMInputError(msg, "No SPM values for 2026")

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=fail,
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_code == "SPM_YEAR_UNAVAILABLE"
    assert child.error_summary == "No SPM values for 2026"


def test_single_worker_rejects_bare_year_policy_period_before_calculation() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={
            "policy": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026": 3_000}}
        }
    )
    store.children[simulation.simulation_execution_id] = _child(simulation)

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=lambda _: pytest.fail("calculation must not start"),
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_code == "invalid_policy_period"
    assert child.error_summary == (
        "Policy parameter 'gov.irs.credits.ctc.amount.base[0].amount' has invalid "
        "effective period '2026'; expected YYYY-MM-DD or "
        "YYYY-MM-DD.YYYY-MM-DD."
    )


def _connection_limit_error() -> OperationalError:
    # SQLAlchemy wraps a failure to open a connection without a statement.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


CORRELATION_SUFFIX = re.compile(r" \(correlation_id=(?P<correlation_id>[0-9a-f]+)\)\Z")


def _correlation_id(error_summary: str) -> str:
    """Return the correlation ID that ends a redacted failure summary."""
    match = CORRELATION_SUFFIX.search(error_summary)
    assert match is not None, error_summary
    return match["correlation_id"]


def _failure_cause(error_summary: str) -> str:
    """Return the redacted cause before a failure summary's correlation ID."""
    _correlation_id(error_summary)
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
        def replace_simulation_if_status(self, record, *, expected_status):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_summary))
                raise _connection_limit_error()
            return super().replace_simulation_if_status(
                record, expected_status=expected_status
            )

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
        "evaluation_id": str(EVALUATION_ID),
        "simulation_execution_id": str(BASELINE_ID),
        "correlation_id": _correlation_id(failure_write[1]),
    }
    assert failure_write[0] == "failure_write"
    assert _failure_cause(failure_write[1]) == (
        "ValueError in stage12_calculation (baseline): "
        "country column missing from household data"
    )
    assert persistence_report[0] == "telemetry"
    assert persistence_report[1][0] == (
        "Stage 12 failure record could not be persisted"
    )
    assert isinstance(persistence_report[1][2], OperationalError)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.RUNNING


def test_late_simulation_success_does_not_overwrite_parent_cancellation() -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)

    def segmented_calculator(value, cancellation_requested):
        current = store.children[value.simulation_execution_id]
        cancelled_at = datetime.now(UTC)
        store.replace_simulation(
            current.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "error_code": "simulation_cancelled",
                    "error_summary": "Cancelled after the reform simulation failed.",
                    "updated_at": cancelled_at,
                    "completed_at": cancelled_at,
                }
            )
        )
        assert cancellation_requested() is True
        return SimulationCalculation(frames=_frames())

    with pytest.raises(RuntimeError, match="Stage 12 simulation was cancelled"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=lambda _: pytest.fail("monolithic calculator was selected"),
            segmented_calculator=segmented_calculator,
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_cancelled"


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
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
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
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
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
        baseline=RetainedSimulationOutput(
            uk_frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            uk_frames, artifacts.add_simulation(report.reform)
        ),
    )
    assert aggregate["result"]["spm_provenance"] is None

    aggregate_reference = artifacts.write_aggregate(
        prefix=_context().artifact_prefix, payload=aggregate
    )

    stored_aggregate = artifacts.read(aggregate_reference.uri)
    assert aggregate_reference.content_sha256 == sha256(stored_aggregate).hexdigest()
    assert json.loads(stored_aggregate)["result"]["spm_provenance"] is None


def test_aggregate_report_wraps_builder_output_with_report_provenance(
    monkeypatch,
) -> None:
    from policyengine_simulation_executor import simulation_output_builder

    report = _report()
    report = report.model_copy(
        update={"reform": report.reform.model_copy(update={"policy": {}})}
    )
    entities = ("person", "marital_unit", "family", "spm_unit", "tax_unit")

    def frames(income):
        return {
            **{entity: pd.DataFrame({f"{entity}_id": [1]}) for entity in entities},
            "household": pd.DataFrame(
                {"household_id": [1], "household_net_income": [income]}
            ),
        }

    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"budget": {"budgetary_impact": 1.0}}

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", CapturingBuilder
    )
    artifacts = FakeArtifacts()

    result = build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames(100.0), artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames(120.0), artifacts.add_simulation(report.reform)
        ),
        uk_local_authority_metadata=None,
    )

    assert result == {
        "contract_version": 1,
        "aggregate_schema_version": 1,
        "evaluation_id": str(EVALUATION_ID),
        "requested_aggregates": [item.value for item in report.requested_aggregates],
        "bundle": report.baseline.bundle.model_dump(mode="json"),
        "result": {"budget": {"budgetary_impact": 1.0}},
    }
    assert list(observed) == [
        "country",
        "simulation_params",
        "country_module",
        "dataset",
        "baseline",
        "reform",
        "resolved_data_version",
        "resolved_region_code",
        "uk_local_authority_metadata",
    ]
    assert observed["country"] == "us"
    assert observed["simulation_params"] == {
        "country": "us",
        "scope": "macro",
        "data": report.baseline.bundle.dataset.uri,
        "data_version": report.baseline.bundle.dataset.artifact_revision,
        "time_period": str(report.baseline.year),
        "region": report.baseline.geography.region,
    }
    assert observed["dataset"] is observed["baseline"].output_dataset
    for role, income in (("baseline", 100.0), ("reform", 120.0)):
        household = observed[role].output_dataset.data.household
        assert household["household_net_income"].tolist() == [income]
    assert observed["resolved_data_version"] == (
        report.baseline.bundle.dataset.artifact_revision
    )
    assert observed["resolved_region_code"] == report.baseline.geography.region
    assert observed["uk_local_authority_metadata"] is None


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

    def cancel(self):
        self.future.cancel()


class ImmediateCall:
    def __init__(self, object_id, result, observed_timeouts):
        self.object_id = object_id
        self.result = result
        self.observed_timeouts = observed_timeouts
        self.cancel_count = 0

    def get(self, *, timeout=None):
        self.observed_timeouts.append(timeout)
        return self.result

    def cancel(self):
        self.cancel_count += 1


@dataclass(frozen=True)
class InjectedChildFaults:
    """Faults a ConcurrentInvoker injects into the child simulations it runs."""

    fail_role: str | None = None
    incompatible: bool = False
    wrong_plan_digest: bool = False
    missing_column: bool = False


class ConcurrentInvoker:
    def __init__(
        self,
        artifacts,
        *,
        faults: InjectedChildFaults | None = None,
        production_result=None,
    ):
        faults = faults or InjectedChildFaults()
        self.artifacts = artifacts
        self.fail_role = faults.fail_role
        self.incompatible = faults.incompatible
        self.wrong_plan_digest = faults.wrong_plan_digest
        self.missing_column = faults.missing_column
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
        invocation=CoordinatorInvocation(
            application_name=context.modal_application,
            invocation_id="coordinator-1",
        ),
        dependencies=CoordinatorDependencies(
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=aggregate,
            output_plan_resolver=resolver,
        ),
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
    assert observed["baseline"].frames["household"][
        "household_net_income"
    ].tolist() == [100.0]
    assert observed["reform"].frames["household"]["household_net_income"].tolist() == [
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
    invoker = ConcurrentInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

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

    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert _failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_wait (reform): child calculation failed"
    )
    failed_child = store.children[REFORM_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_invocation_failed"
    assert failed_child.error_summary == store.parent.error_summary


@pytest.mark.parametrize(
    ("failed_role", "failed_id", "pending_role", "pending_id"),
    [
        ("reform", REFORM_ID, "baseline", BASELINE_ID),
        ("baseline", BASELINE_ID, "reform", REFORM_ID),
    ],
)
def test_coordinator_observes_one_role_failure_while_the_peer_is_pending(
    failed_role,
    failed_id,
    pending_role,
    pending_id,
) -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    class PendingCall:
        object_id = f"call-{pending_role}"

        def __init__(self):
            self.timeouts = []
            self.cancel_count = 0

        def get(self, *, timeout=None):
            self.timeouts.append(timeout)
            raise TimeoutError

        def cancel(self):
            self.cancel_count += 1

    class FailedCall:
        object_id = f"call-{failed_role}"

        def __init__(self):
            self.timeouts = []

        def get(self, *, timeout=None):
            self.timeouts.append(timeout)
            child = store.children[failed_id]
            failed_at = datetime.now(UTC)
            store.replace_simulation(
                child.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "error_code": "invalid_policy_period",
                        "error_summary": "The reform period is invalid.",
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                )
            )
            msg = "remote wrapper"
            raise RuntimeError(msg)

        def cancel(self):
            return None

    baseline_call = PendingCall()
    failed_call = FailedCall()

    class ConcurrentFailureInvoker:
        def spawn(self, *, simulation, **_):
            role = PlannedSimulationExecutionInput.model_validate(simulation).role
            return failed_call if role.value == failed_role else baseline_call

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentFailureInvoker(),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            sleep=lambda _: pytest.fail("failure should be observed before sleeping"),
        )

    assert failed_call.timeouts == [0]
    assert baseline_call.timeouts == ([0] if pending_role == "baseline" else [])
    # A segmented peer reads its cancelled record and stops its own
    # region-group calls, so it is not cancelled through Modal.
    assert baseline_call.cancel_count == 0
    pending = store.children[pending_id]
    assert pending.status is ComparisonRunLifecycleStatus.FAILED
    assert pending.error_code == "simulation_cancelled"
    assert pending.error_summary == (
        f"Cancelled after the {failed_role} simulation failed."
    )
    assert store.parent.error_code == "invalid_policy_period"
    assert store.parent.error_summary == "The reform period is invalid."
    assert artifacts.aggregate_writes == []


def test_dispatch_failure_cancels_an_already_started_peer() -> None:
    store = FakeStore()
    baseline_call = TimeoutCall("call-baseline")

    class DispatchFailureInvoker:
        def spawn(self, *, simulation, **_):
            role = PlannedSimulationExecutionInput.model_validate(simulation).role
            if role is SimulationRole.REFORM:
                msg = "dispatch contains sensitive details"
                raise ConnectionError(msg)
            return baseline_call

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=DispatchFailureInvoker(),
        )

    baseline = store.children[BASELINE_ID]
    reform = store.children[REFORM_ID]
    assert baseline_call.cancelled == 0
    assert baseline.status is ComparisonRunLifecycleStatus.FAILED
    assert baseline.error_code == "simulation_cancelled"
    assert baseline.error_summary == "Cancelled after the reform simulation failed."
    assert reform.status is ComparisonRunLifecycleStatus.FAILED
    assert reform.error_code == "simulation_dispatch_failed"
    assert store.parent.error_code == "simulation_dispatch_failed"


def test_coordinator_propagates_an_already_persisted_child_failure() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    class PersistedFailureCall:
        object_id = "call-reform"

        def get(self, *, timeout=None):
            child = store.children[REFORM_ID]
            failed_at = datetime.now(UTC)
            store.replace_simulation(
                child.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "error_code": "invalid_policy_period",
                        "error_summary": "The reform period is invalid.",
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                )
            )
            msg = "remote wrapper must not replace the child failure"
            raise RuntimeError(msg)

    class PersistedFailureInvoker:
        def spawn(self, *, simulation, **_):
            parsed = PlannedSimulationExecutionInput.model_validate(simulation)
            if parsed.role is SimulationRole.REFORM:
                return PersistedFailureCall()
            descriptor = artifacts.add_simulation(parsed, _frames(100.0))
            return ImmediateCall(
                "call-baseline", descriptor.model_dump(mode="json"), []
            )

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=PersistedFailureInvoker(),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        )

    assert store.children[REFORM_ID].error_code == "invalid_policy_period"
    assert store.children[REFORM_ID].error_summary == "The reform period is invalid."
    assert store.parent.error_code == "invalid_policy_period"
    assert store.parent.error_summary == "The reform period is invalid."


@pytest.mark.parametrize(
    "invoker",
    [
        lambda artifacts: ConcurrentInvoker(
            artifacts, faults=InjectedChildFaults(wrong_plan_digest=True)
        ),
        lambda artifacts: ConcurrentInvoker(
            artifacts, faults=InjectedChildFaults(missing_column=True)
        ),
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
        @override
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
    assert _failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_state_create: database rejected the child record"
    )


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
        msg = "the coordinator observes baseline first"
        raise AssertionError(msg)

    def cancel(self):
        return None


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
        "Variable 'country' not found in household data "
        "(correlation_id=0123456789abcdef0123456789abcdef)"
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
    assert store.parent.error_code == "simulation_execution_failed"
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

    assert _failure_cause(store.parent.error_summary) == (
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
    assert original_report[1][1] == {
        "stage12_failure_scope": "report_coordination",
        "evaluation_id": str(EVALUATION_ID),
        "correlation_id": _correlation_id(failure_write[1]),
    }
    assert original_report[1][2] is aggregation_error
    assert failure_write[0] == "failure_write"
    assert _failure_cause(failure_write[1]) == (
        "ValueError in stage12_aggregation: aggregate failed validation"
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
            invoker=ConcurrentInvoker(
                artifacts, faults=InjectedChildFaults(fail_role="baseline")
            ),
            aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    failure_reports = [
        event[1]
        for event in ordered_events
        if event[0] == "telemetry" and event[1][0] == "Stage 12 execution failed"
    ]
    assert [report[1] for report in failure_reports] == [
        {
            "stage12_failure_scope": "child_wait",
            "simulation_role": "baseline",
            "evaluation_id": str(EVALUATION_ID),
            "simulation_execution_id": str(BASELINE_ID),
            "correlation_id": _correlation_id(store.parent.error_summary),
        }
    ]
    assert ordered_events[0][0] == "telemetry"
    assert ordered_events[1] == ("child_read", str(BASELINE_ID))
    # The report record is still written after the child record could not be.
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "simulation_invocation_failed"
    assert _failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_wait (baseline): child calculation failed"
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
    baseline_child = store.children[BASELINE_ID]
    assert [(attributes, error) for _message, attributes, error in failure_reports] == [
        (
            {
                "stage12_failure_scope": "child_dispatch",
                "simulation_role": "baseline",
                "evaluation_id": str(EVALUATION_ID),
                "simulation_execution_id": str(BASELINE_ID),
                "correlation_id": _correlation_id(baseline_child.error_summary),
            },
            dispatch_error,
        )
    ]
    assert invoker.events == ["spawn:baseline"]
    assert baseline_child.status is ComparisonRunLifecycleStatus.FAILED
    assert baseline_child.error_code == "simulation_dispatch_failed"
    assert _failure_cause(baseline_child.error_summary) == (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal refused the dispatch"
    )
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == baseline_child.error_summary


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

    assert invoker.events == ["spawn:baseline"]
    assert artifacts.aggregate_writes == []
    failed_child = store.children[BASELINE_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_dispatch_failed"
    assert _failure_cause(failed_child.error_summary) == (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal rejected the call with token=[REDACTED]"
    )
    assert store.parent.error_code == "simulation_dispatch_failed"
    assert store.parent.error_summary == failed_child.error_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == failed_child.error_summary


def test_coordinator_rejects_incompatible_row_identity() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(
        artifacts, faults=InjectedChildFaults(incompatible=True)
    )

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
        self.cancelled = 0

    def get(self, *, timeout=None):
        msg = "contains sensitive timeout details"
        raise TimeoutError(msg)

    def cancel(self):
        self.cancelled += 1


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
            wait_timeout_seconds=0,
        )

    assert "sensitive" not in str(error.value)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "simulation_wait_timeout"
    assert store.parent.error_summary == (
        "Stage 12 simulations did not finish within 0 seconds of the report "
        "coordinator starting."
    )
    assert all(
        child.status is ComparisonRunLifecycleStatus.FAILED
        for child in store.children.values()
    )
    assert all(
        child.error_code == "simulation_cancelled" for child in store.children.values()
    )


def test_closed_wait_window_is_reported_before_its_failure_records() -> None:
    ordered_events: list[tuple[str, object]] = []

    class UnwritableFailureStore(FakeStore):
        def replace_simulation_if_status(self, record, *, expected_status):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_code))
                raise _connection_limit_error()
            return super().replace_simulation_if_status(
                record, expected_status=expected_status
            )

        def replace_report(self, record):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_code))
                raise _connection_limit_error()
            return super().replace_report(record)

    store = UnwritableFailureStore()
    invoker = TimeoutInvoker()

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
            wait_timeout_seconds=0,
        )

    original_report, *failure_writes, persistence_report = ordered_events
    event_kind, (message, attributes, reported_error) = original_report
    assert event_kind == "telemetry"
    assert message == "Stage 12 execution failed"
    assert attributes == {"stage12_failure_scope": "child_wait_timeout"}
    assert reported_error.detail.error_code == "simulation_wait_timeout"
    # Both cancellation records and the report record were attempted after
    # the closed window had already been reported.
    assert failure_writes == [
        ("failure_write", "simulation_cancelled"),
        ("failure_write", "simulation_cancelled"),
        ("failure_write", "simulation_wait_timeout"),
    ]
    assert persistence_report[1][0] == (
        "Stage 12 failure record could not be persisted"
    )
    assert store.parent.status is ComparisonRunLifecycleStatus.RUNNING


class StartupWriteError(Exception):
    """A coordinator persistence failure after a child may have started."""


class ScriptedChildCall:
    """A started child that keeps running unless the test scripts an outcome."""

    def __init__(
        self,
        object_id,
        *,
        clock=None,
        result=None,
        wait_error=None,
        cancel_error=None,
        during_wait=None,
    ):
        self.object_id = object_id
        self.clock = clock
        self.result = result
        self.wait_error = wait_error
        self.cancel_error = cancel_error
        self.during_wait = during_wait
        self.probed_at = []
        self.cancel_count = 0

    def get(self, *, timeout=None):
        self.probed_at.append(
            self.clock.monotonic() if self.clock is not None else None
        )
        if self.during_wait is not None:
            self.during_wait()
        if self.wait_error is not None:
            raise self.wait_error
        if self.result is not None:
            return self.result
        msg = "still running"
        raise TimeoutError(msg)

    def cancel(self):
        self.cancel_count += 1
        if self.cancel_error is not None:
            raise self.cancel_error


class ScriptedChildInvoker:
    def __init__(
        self,
        *,
        clock=None,
        results=None,
        wait_errors=None,
        cancel_errors=None,
        during_wait=None,
    ):
        self.clock = clock
        self.results = results or {}
        self.wait_errors = wait_errors or {}
        self.cancel_errors = cancel_errors or {}
        self.during_wait = during_wait or {}
        self.events = []
        self.calls = {}

    def spawn(self, *, simulation, **_):
        role = PlannedSimulationExecutionInput.model_validate(simulation).role.value
        self.events.append(f"spawn:{role}")
        return self._call(role)

    def restore(self, invocation_id):
        role = invocation_id.removeprefix("call-")
        self.events.append(f"restore:{role}")
        return self._call(role)

    def _call(self, role):
        call = ScriptedChildCall(
            f"call-{role}",
            clock=self.clock,
            result=self.results.get(role),
            wait_error=self.wait_errors.get(role),
            cancel_error=self.cancel_errors.get(role),
            during_wait=self.during_wait.get(role),
        )
        self.calls[role] = call
        return call


class PlaceholderWriteFailingStore(FakeStore):
    def __init__(self, failing_simulation_id):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id

    def replace_simulation(self, record):
        # The coordinator's only plain replacement is the dispatch placeholder.
        if record.simulation_execution_id == self.failing_simulation_id:
            msg = "placeholder write contains sensitive details"
            raise StartupWriteError(msg)
        return super().replace_simulation(record)


class AttachFailingStore(FakeStore):
    def __init__(self, failing_simulation_id):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id

    def attach_simulation_invocation(self, simulation_execution_id, **values):
        if simulation_execution_id == self.failing_simulation_id:
            msg = "attach contains sensitive details"
            raise StartupWriteError(msg)
        return super().attach_simulation_invocation(simulation_execution_id, **values)


class CancellationStateFailingStore(FakeStore):
    def replace_simulation_if_status(self, record, *, expected_status):
        if record.error_code == "simulation_cancelled":
            msg = "database contains sensitive details"
            raise ConnectionError(msg)
        return super().replace_simulation_if_status(
            record, expected_status=expected_status
        )


def _simulation_id(role: str) -> UUID:
    return BASELINE_ID if role == "baseline" else REFORM_ID


def _report_with_segmentation(*, segmented: bool) -> ReportExecutionInput:
    if segmented:
        return _report()

    def unsegmented(role: SimulationRole) -> SimulationExecutionInput:
        return _simulation(role).model_copy(update={"options": {"segmented": False}})

    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=unsegmented(SimulationRole.BASELINE),
        reform=unsegmented(SimulationRole.REFORM),
        requested_aggregates=tuple(ReportAggregate),
    )


def _coordinate_with(report, store, invoker, **coordinator_options):
    coordinator_options.setdefault("artifacts", FakeArtifacts())
    return coordinate_report(
        report.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        application_name=_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        invoker=invoker,
        aggregator=lambda **_: _aggregate_payload({"must": "not run"}),
        **coordinator_options,
    )


def _assert_cancelled(child, *, after: str) -> None:
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_cancelled"
    assert child.error_summary == f"Cancelled after {after}."
    assert child.completed_at is not None


def _assert_original_coordinator_failure(error, store) -> None:
    assert isinstance(error.value.__context__, StartupWriteError)
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "report_coordination_failed"
    original_error = error.value.__context__
    assert _failure_cause(store.parent.error_summary).startswith(
        f"{type(original_error).__name__} "
    )
    assert _failure_cause(store.parent.error_summary).endswith(f": {original_error}")


def _coordination_log(caplog, level):
    (record,) = [
        record
        for record in caplog.records
        if record.name == coordination_module.__name__ and record.levelno == level
    ]
    return record


def _assert_log_identifies_child(record, *, role: str, error_type: str) -> None:
    simulation_execution_id = str(_simulation_id(role))
    assert record.evaluation_id == str(EVALUATION_ID)
    assert record.simulation_execution_id == simulation_execution_id
    assert record.simulation_role == role
    assert record.error_type == error_type
    message = record.getMessage()
    assert str(EVALUATION_ID) in message
    assert simulation_execution_id in message
    assert "sensitive" not in message
    assert record.exc_info is None


def test_baseline_failure_cancels_and_records_the_reform_child() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline worker failed")}
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report_with_segmentation(segmented=False),
            store,
            invoker,
            artifacts=artifacts,
        )

    reform_call = invoker.calls["reform"]
    assert reform_call.cancel_count == 1
    assert reform_call.probed_at == []
    assert artifacts.aggregate_writes == []
    assert store.children[BASELINE_ID].error_code == "simulation_invocation_failed"
    _assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert store.parent.error_code == "simulation_invocation_failed"


def test_reform_failure_leaves_the_finished_baseline_alone() -> None:
    report = _report_with_segmentation(segmented=False)
    store = FakeStore()
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    invoker = ScriptedChildInvoker(
        results={"baseline": baseline_descriptor.model_dump(mode="json")},
        wait_errors={"reform": LookupError("reform worker failed")},
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(report, store, invoker, artifacts=artifacts)

    assert invoker.calls["baseline"].cancel_count == 0
    assert store.children[BASELINE_ID].status is ComparisonRunLifecycleStatus.RUNNING
    assert store.children[BASELINE_ID].error_code is None
    assert store.children[REFORM_ID].error_code == "simulation_invocation_failed"


def test_baseline_failure_cancels_a_reform_call_restored_on_retry() -> None:
    report = _report_with_segmentation(segmented=False)
    store = FakeStore()
    # An earlier coordinator attempt started the reform child before stopping.
    store.children[REFORM_ID] = _child(report.reform).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "modal_invocation_id": "call-reform",
            "started_at": NOW,
        }
    )
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline worker failed")}
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(report, store, invoker)

    assert invoker.events == ["spawn:baseline", "restore:reform"]
    assert invoker.calls["reform"].cancel_count == 1
    _assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert store.children[BASELINE_ID].error_code == "simulation_invocation_failed"


@pytest.mark.parametrize(
    ("worker_status", "worker_error_code"),
    [
        (ComparisonRunLifecycleStatus.SUCCEEDED, None),
        (ComparisonRunLifecycleStatus.FAILED, "simulation_execution_failed"),
    ],
)
def test_cancellation_leaves_a_sibling_that_already_finished_unchanged(
    worker_status,
    worker_error_code,
) -> None:
    store = FakeStore()
    worker_records = []

    def reform_worker_records_its_outcome():
        # The reform worker finishes while the coordinator probes baseline.
        finished_at = NOW + timedelta(minutes=5)
        worker_records.append(
            store.replace_simulation(
                store.get_simulation(REFORM_ID).model_copy(
                    update={
                        "status": worker_status,
                        "error_code": worker_error_code,
                        "updated_at": finished_at,
                        "completed_at": finished_at,
                    }
                )
            )
        )

    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline worker failed")},
        during_wait={"baseline": reform_worker_records_its_outcome},
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    # Cancelling a finished Modal call is harmless; its record is not touched.
    assert invoker.calls["reform"].cancel_count == 1
    assert store.children[REFORM_ID] == worker_records[0]
    assert store.children[BASELINE_ID].error_code == "simulation_invocation_failed"


def test_peer_cancellation_failure_does_not_replace_the_child_failure(
    caplog,
) -> None:
    store = FakeStore()
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline failure is sensitive")},
        cancel_errors={
            "reform": ConnectionError("cancel request contains sensitive details")
        },
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    assert invoker.calls["reform"].cancel_count == 1
    _assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert store.parent.error_code == "simulation_invocation_failed"
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.WARNING),
        role="reform",
        error_type="ConnectionError",
    )


@pytest.mark.parametrize("segmented", [True, False])
def test_failed_cancellation_state_write_still_cancels_through_modal(
    caplog,
    segmented,
) -> None:
    store = CancellationStateFailingStore()
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline failure is sensitive")},
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        _coordinate_with(_report_with_segmentation(segmented=segmented), store, invoker)

    # A segmented worker cannot read a cancellation that was never written, so
    # it is cancelled through Modal as well.
    assert invoker.calls["reform"].cancel_count == 1
    assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.RUNNING
    assert store.parent.error_code == "simulation_invocation_failed"
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.ERROR),
        role="reform",
        error_type="ConnectionError",
    )


def test_wait_timeout_cancels_and_records_every_unfinished_child() -> None:
    store = FakeStore()
    invoker = ScriptedChildInvoker()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report_with_segmentation(segmented=False),
            store,
            invoker,
            wait_timeout_seconds=0,
        )

    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    for role in ("baseline", "reform"):
        _assert_cancelled(
            store.children[_simulation_id(role)],
            after="the 0-second wait window closed",
        )
    assert store.parent.error_code == "simulation_wait_timeout"


def test_timeout_cancellation_failure_log_identifies_the_report_and_child(
    caplog,
) -> None:
    store = FakeStore()
    invoker = ScriptedChildInvoker(
        cancel_errors={
            "reform": ConnectionError("cancel request contains sensitive details")
        },
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        _coordinate_with(
            _report_with_segmentation(segmented=False),
            store,
            invoker,
            wait_timeout_seconds=0,
        )

    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    assert store.parent.error_code == "simulation_wait_timeout"
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.WARNING),
        role="reform",
        error_type="ConnectionError",
    )


@pytest.mark.parametrize("segmented", [True, False])
def test_failed_placeholder_write_stops_the_started_baseline(segmented) -> None:
    store = PlaceholderWriteFailingStore(REFORM_ID)
    invoker = ScriptedChildInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        _coordinate_with(_report_with_segmentation(segmented=segmented), store, invoker)

    _assert_original_coordinator_failure(error, store)
    assert invoker.events == ["spawn:baseline"]
    _assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
    # A segmented worker stops itself, and its region-group calls, once it
    # reads the cancelled record; Modal does not cancel spawned calls
    # recursively.
    assert invoker.calls["baseline"].cancel_count == (0 if segmented else 1)
    assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.PENDING


@pytest.mark.parametrize(
    ("failing_role", "started_roles"),
    [
        ("baseline", ["baseline"]),
        ("reform", ["baseline", "reform"]),
    ],
)
def test_failed_invocation_attach_stops_every_started_child(
    failing_role,
    started_roles,
) -> None:
    store = AttachFailingStore(_simulation_id(failing_role))
    invoker = ScriptedChildInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    _assert_original_coordinator_failure(error, store)
    assert invoker.events == [f"spawn:{role}" for role in started_roles]
    # The child whose invocation could not be attached is stopped as well.
    for role in started_roles:
        _assert_cancelled(
            store.children[_simulation_id(role)], after="the coordinator failed"
        )
        assert invoker.calls[role].cancel_count == 1
    if failing_role == "baseline":
        assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.PENDING


def test_startup_failure_stops_a_call_restored_from_an_earlier_attempt() -> None:
    report = _report_with_segmentation(segmented=False)
    store = PlaceholderWriteFailingStore(REFORM_ID)
    store.children[BASELINE_ID] = _child(report.baseline).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "modal_invocation_id": "call-baseline",
            "started_at": NOW,
        }
    )
    invoker = ScriptedChildInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        _coordinate_with(report, store, invoker)

    _assert_original_coordinator_failure(error, store)
    assert invoker.events == ["restore:baseline"]
    _assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
    assert invoker.calls["baseline"].cancel_count == 1


def test_startup_failure_leaves_a_finished_child_unchanged() -> None:
    report = _report_with_segmentation(segmented=False)
    store = AttachFailingStore(REFORM_ID)
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    finished_baseline = _child(report.baseline).model_copy(
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
    store.children[BASELINE_ID] = finished_baseline
    invoker = ScriptedChildInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        _coordinate_with(report, store, invoker, artifacts=artifacts)

    _assert_original_coordinator_failure(error, store)
    assert invoker.events == ["spawn:reform"]
    assert store.children[BASELINE_ID] == finished_baseline
    _assert_cancelled(store.children[REFORM_ID], after="the coordinator failed")
    assert invoker.calls["reform"].cancel_count == 1


def test_startup_cancellation_error_does_not_replace_the_original_failure(
    caplog,
) -> None:
    store = PlaceholderWriteFailingStore(REFORM_ID)
    invoker = ScriptedChildInvoker(
        cancel_errors={
            "baseline": ConnectionError("cancel request contains sensitive details")
        }
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(
            RuntimeError, match="Stage 12 report coordination failed"
        ) as error,
    ):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    _assert_original_coordinator_failure(error, store)
    assert invoker.calls["baseline"].cancel_count == 1
    _assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.WARNING),
        role="baseline",
        error_type="ConnectionError",
    )


def test_startup_cancellation_state_error_does_not_replace_the_original_failure(
    caplog,
) -> None:
    class FailingStore(PlaceholderWriteFailingStore, CancellationStateFailingStore):
        pass

    store = FailingStore(REFORM_ID)
    invoker = ScriptedChildInvoker()

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(
            RuntimeError, match="Stage 12 report coordination failed"
        ) as error,
    ):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    _assert_original_coordinator_failure(error, store)
    assert invoker.calls["baseline"].cancel_count == 1
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.ERROR),
        role="baseline",
        error_type="ConnectionError",
    )


class ChildStateError(Exception):
    """A database failure while reading or writing a child's own record."""


class OwnChildStateFailingStore(FakeStore):
    """Fail one read, or one failed-status write, of one child's record once.

    Cancellation writes are left alone, so only the failed child's own state
    update can fail.
    """

    def __init__(self, *, failing_simulation_id, failing_operation):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id
        self.failing_operation = failing_operation
        self.remaining_failures = 1

    def _fail_once(self, operation, simulation_execution_id):
        if (
            operation == self.failing_operation
            and simulation_execution_id == self.failing_simulation_id
            and self.remaining_failures > 0
        ):
            self.remaining_failures -= 1
            msg = "database contains sensitive details"
            raise ChildStateError(msg)

    def get_simulation(self, simulation_execution_id):
        self._fail_once("read", simulation_execution_id)
        return super().get_simulation(simulation_execution_id)

    def replace_simulation_if_status(self, record, *, expected_status):
        if record.error_code != "simulation_cancelled":
            self._fail_once("write", record.simulation_execution_id)
        return super().replace_simulation_if_status(
            record, expected_status=expected_status
        )


@pytest.mark.parametrize(
    ("failing_operation", "baseline_status", "baseline_error_code"),
    [
        # The failure is still recorded: the read before the conditional
        # write succeeds.
        (
            "read",
            ComparisonRunLifecycleStatus.FAILED,
            "simulation_invocation_failed",
        ),
        ("write", ComparisonRunLifecycleStatus.RUNNING, None),
    ],
)
def test_failed_child_state_error_does_not_replace_the_child_failure(
    caplog,
    failing_operation,
    baseline_status,
    baseline_error_code,
) -> None:
    store = OwnChildStateFailingStore(
        failing_simulation_id=BASELINE_ID,
        failing_operation=failing_operation,
    )
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline failure is sensitive")},
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    assert store.parent.error_code == "simulation_invocation_failed"
    _correlation_id(store.parent.error_summary)
    baseline = store.children[BASELINE_ID]
    assert baseline.status is baseline_status
    assert baseline.error_code == baseline_error_code
    if baseline_error_code is not None:
        assert baseline.error_summary == store.parent.error_summary
    # The failed child is neither cancelled nor recorded as cancelled.
    assert invoker.calls["baseline"].cancel_count == 0
    _assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert invoker.calls["reform"].cancel_count == 1
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.ERROR),
        role="baseline",
        error_type="ChildStateError",
    )


class ReformDispatchFailureInvoker(ScriptedChildInvoker):
    def spawn(self, *, simulation, **values):
        role = PlannedSimulationExecutionInput.model_validate(simulation).role
        if role is SimulationRole.REFORM:
            self.events.append("spawn:reform")
            msg = "dispatch contains sensitive details"
            raise ConnectionError(msg)
        return super().spawn(simulation=simulation, **values)


@pytest.mark.parametrize("failing_operation", ["read", "write"])
def test_dispatch_failure_state_error_does_not_replace_the_child_failure(
    caplog,
    failing_operation,
) -> None:
    store = OwnChildStateFailingStore(
        failing_simulation_id=REFORM_ID,
        failing_operation=failing_operation,
    )
    invoker = ReformDispatchFailureInvoker()

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        _coordinate_with(_report_with_segmentation(segmented=False), store, invoker)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert store.parent.error_code == "simulation_dispatch_failed"
    _correlation_id(store.parent.error_summary)
    # The reform record could not be updated, so it keeps its dispatch
    # placeholder rather than a cancellation.
    reform = store.children[REFORM_ID]
    assert reform.status is ComparisonRunLifecycleStatus.RUNNING
    assert reform.error_code is None
    _assert_cancelled(store.children[BASELINE_ID], after="the reform simulation failed")
    assert invoker.calls["baseline"].cancel_count == 1
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.ERROR),
        role="reform",
        error_type="ChildStateError",
    )


class ExceptionRecordingRuntime(RecordingRuntime):
    """Record every telemetry call that can report a failure."""

    def __init__(self) -> None:
        super().__init__()
        self.reports: list[tuple[str, object, dict, object]] = []

    def log(self, message, *, severity="INFO", attributes=None, error=None):
        self.reports.append(("log", message, dict(attributes or {}), error))

    def record_exception(self, error, *, handled, status_code=None):
        self.reports.append(("exception", None, {}, error))

    def event(self, name, *, attributes=None, **_options):
        self.reports.append(("event", name, dict(attributes or {}), None))

    def set_context(self, **_attributes):
        return None


def _fail_simulation_calculation(runtime):
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    calculation_error = ValueError("calculation contains sensitive details")

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
            runtime=runtime,
        )
    return calculation_error, store.children[BASELINE_ID].error_summary


def _fail_child_call(runtime):
    store = FakeStore()
    child_error = LookupError("baseline failure contains sensitive details")
    invoker = ScriptedChildInvoker(wait_errors={"baseline": child_error})

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report_with_segmentation(segmented=False),
            store,
            invoker,
            runtime=runtime,
        )
    return child_error, store.parent.error_summary


def _fail_child_dispatch(runtime):
    store = FakeStore()
    dispatch_error = ConnectionError("dispatch contains sensitive details")

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report(),
            store,
            RefusingDispatchInvoker(dispatch_error),
            runtime=runtime,
        )
    return dispatch_error, store.parent.error_summary


def _fail_report_aggregation(runtime):
    store = FakeStore()
    artifacts = FakeArtifacts()
    aggregation_error = ValueError("aggregation contains sensitive details")

    def fail_aggregation(**_values):
        raise aggregation_error

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
            aggregator=fail_aggregation,
            runtime=runtime,
        )
    return aggregation_error, store.parent.error_summary


@pytest.mark.parametrize(
    "fail",
    [
        _fail_simulation_calculation,
        _fail_child_call,
        _fail_child_dispatch,
        _fail_report_aggregation,
    ],
)
@pytest.mark.parametrize("with_runtime", [True, False], ids=["runtime", "no-runtime"])
def test_an_unexpected_failure_is_reported_exactly_once(
    caplog,
    fail,
    with_runtime,
) -> None:
    runtime = ExceptionRecordingRuntime() if with_runtime else None

    with caplog.at_level(logging.DEBUG):
        original_error, error_summary = fail(runtime)

    correlation_id = _correlation_id(error_summary)
    logged_failures = [record for record in caplog.records if record.exc_info]
    if runtime is not None:
        [(kind, message, attributes, reported_error)] = [
            report for report in runtime.reports if report[0] == "log"
        ]
        assert (kind, message) == ("log", "Stage 12 execution failed")
        assert reported_error is original_error
        assert attributes["correlation_id"] == correlation_id
        # The redacted cause, never the original error, is also attached to
        # the failing operation, once.
        [recorded_cause] = [
            report[3] for report in runtime.reports if report[0] == "exception"
        ]
        assert str(recorded_cause) == error_summary
        assert sorted(report[0] for report in runtime.reports) == ["exception", "log"]
        assert logged_failures == []
    else:
        [logged_failure] = logged_failures
        assert logged_failure.exc_info[1] is original_error
        assert f"correlation_id={correlation_id}" in logged_failure.getMessage()


def test_a_segment_failure_is_reported_exactly_once(caplog) -> None:
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    runtime = ExceptionRecordingRuntime()
    segment_error = RuntimeError("segment contains sensitive details")

    class SegmentFunction:
        def spawn(self, payload, *, observability_context):
            segment_index = payload["segment_index"]
            # The first segment fails on every probe; the others keep running.
            return ScriptedChildCall(
                f"segment-{segment_index}",
                wait_error=segment_error if segment_index == 0 else None,
            )

    class SegmentFunctionLookup:
        @staticmethod
        def from_name(_app_name, _function_name):
            return SegmentFunction()

    def segmented_calculator(value, cancellation_requested):
        return Stage12SegmentRunner(
            value,
            app_name="stage12-app",
            runtime=runtime,
            modal_module=SimpleNamespace(Function=SegmentFunctionLookup),
            groups=US_REGION_GROUPS,
            sleep=lambda _seconds: None,
            cancellation_requested=cancellation_requested,
        ).run()

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"),
    ):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            _context().model_dump(mode="json"),
            required_country="us",
            store=store,
            artifacts=FakeArtifacts(),
            calculator=lambda _: pytest.fail("monolithic calculator was selected"),
            segmented_calculator=segmented_calculator,
            runtime=runtime,
        )

    child = store.children[BASELINE_ID]
    assert child.error_code == "segment_execution_failed"
    [(kind, message, attributes, reported_error)] = [
        report for report in runtime.reports if report[0] == "log"
    ]
    [recorded_cause] = [
        report[3] for report in runtime.reports if report[0] == "exception"
    ]
    assert str(recorded_cause) == child.error_summary
    assert (kind, message) == ("log", "Stage 12 execution failed")
    assert reported_error is segment_error
    assert attributes == {
        "stage12_failure_scope": "segment_call",
        "simulation_role": "baseline",
        "evaluation_id": str(EVALUATION_ID),
        "simulation_execution_id": str(BASELINE_ID),
        "segment_index": 0,
        "correlation_id": _correlation_id(
            child.error_summary.removeprefix("Segment 0: ")
        ),
    }
    assert [record for record in caplog.records if record.exc_info] == []


class SteppedClock:
    def __init__(self, start_seconds=10_000.0):
        self.current_seconds = start_seconds

    def monotonic(self):
        return self.current_seconds

    def advance(self, elapsed_seconds):
        self.current_seconds += elapsed_seconds


def _declared_modal_timeouts(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_app", None)
    modal_app = importlib.import_module("src.modal.v2_app")
    return {
        name: options.get("timeout") for name, options in modal_app.app.function_calls
    }


# After the child window closes and the production-result wait ends, the
# coordinator still has to read both simulation artifacts, aggregate, write its
# artifacts, and record the outcome before Modal stops it.
MINIMUM_FINISHING_SECONDS = 600


def test_declared_modal_timeouts_fit_the_shared_wait_window(monkeypatch) -> None:
    declared_timeouts = _declared_modal_timeouts(monkeypatch)
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    child_worker_timeouts = {
        name: timeout
        for name, timeout in declared_timeouts.items()
        if name.startswith("run_single_simulation")
    }

    assert set(child_worker_timeouts) == {
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
    }
    # The window covers every worker's own Modal timeout.
    assert all(window_seconds >= timeout for timeout in child_worker_timeouts.values())
    assert (
        declared_timeouts["coordinate_report"]
        >= window_seconds
        + PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
        + MINIMUM_FINISHING_SECONDS
    )


@pytest.mark.parametrize("setup_seconds", [0.0, 400.0, 2_000.0])
def test_child_wait_window_opens_when_the_coordinator_starts(
    monkeypatch,
    setup_seconds,
) -> None:
    declared_timeouts = _declared_modal_timeouts(monkeypatch)
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    clock = SteppedClock()
    coordinator_started_at = clock.monotonic()

    def slow_output_planning(_report):
        # Loading the country model is coordinator setup, not child time.
        clock.advance(setup_seconds)
        return _output_plan()

    store = FakeStore()
    invoker = ScriptedChildInvoker(clock=clock)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report(),
            store,
            invoker,
            output_plan_resolver=slow_output_planning,
            monotonic=clock.monotonic,
            sleep=clock.advance,
        )

    probed_at = invoker.calls["baseline"].probed_at
    child_waits_began_after = probed_at[0] - coordinator_started_at
    window_closed_after = probed_at[-1] - coordinator_started_at
    assert child_waits_began_after == setup_seconds
    # Setup shortens the children's window rather than the time after it.
    assert window_closed_after == pytest.approx(window_seconds)
    assert probed_at[-1] - probed_at[0] == pytest.approx(window_seconds - setup_seconds)
    assert (
        declared_timeouts["coordinate_report"]
        - window_closed_after
        - PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
        >= MINIMUM_FINISHING_SECONDS
    )
    assert store.parent.error_code == "simulation_wait_timeout"
    for role in ("baseline", "reform"):
        _assert_cancelled(
            store.children[_simulation_id(role)],
            after=f"the {window_seconds:g}-second wait window closed",
        )


def test_exhausted_wait_window_gives_the_children_no_extra_time() -> None:
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    clock = SteppedClock()

    def setup_that_uses_the_whole_window(_report):
        clock.advance(window_seconds + 60.0)
        return _output_plan()

    store = FakeStore()
    invoker = ScriptedChildInvoker(clock=clock)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        _coordinate_with(
            _report_with_segmentation(segmented=False),
            store,
            invoker,
            output_plan_resolver=setup_that_uses_the_whole_window,
            monotonic=clock.monotonic,
            sleep=lambda _: pytest.fail("an exhausted window must not wait"),
        )

    assert [len(call.probed_at) for call in invoker.calls.values()] == [1, 1]
    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    assert store.parent.error_code == "simulation_wait_timeout"
