"""Tests for independent Stage 12 simulations and report coordination."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from uuid import UUID, uuid4

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_execution import (
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
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    RequestedSimulationOutput,
    ResultComparisonStatus,
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
)

from policyengine_simulation_executor.stage12_artifacts import (
    canonical_json_bytes,
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    _build_spm_result,
    coordinate_report,
    run_single_simulation,
    simulation_input_sha256,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000002")
REFORM_ID = UUID("00000000-0000-0000-0000-000000000003")


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
        requested_output=RequestedSimulationOutput(variables=("*",)),
        bundle=_bundle(),
    )


def _report() -> ReportExecutionInput:
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=_simulation(SimulationRole.BASELINE),
        reform=_simulation(SimulationRole.REFORM),
        requested_aggregates=(ReportAggregate.BUDGET,),
    )


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
    ):
        descriptor = self.add_simulation(simulation, frames)
        return descriptor.model_copy(
            update={"calculation_provenance": calculation_provenance}
        )

    def read(self, uri):
        return self.payloads[uri]

    def write_aggregate(self, *, prefix, payload):
        encoded = canonical_json_bytes(payload)
        digest = sha256(encoded).hexdigest()
        uri = "gs://private/aggregate.json"
        self.payloads[uri] = encoded
        self.aggregate_writes.append(payload)
        return ArtifactReference(
            uri=uri,
            media_type=ArtifactMediaType.JSON,
            content_sha256=digest,
            size_bytes=len(encoded),
        )

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


def test_single_worker_accepts_one_policy_and_persists_one_artifact() -> None:
    store = FakeStore()
    simulation = _simulation(SimulationRole.BASELINE)
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
    simulation = _simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    artifacts = FakeArtifacts()
    provenance = {"receipt": {"version": 1}}

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

    assert result["calculation_provenance"] == provenance


def test_single_worker_exposes_only_a_bounded_failure() -> None:
    store = FakeStore()
    simulation = _simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)

    def fail(_simulation):
        raise RuntimeError("contains sensitive calculation values")

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

    assert "sensitive" not in str(error.value)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_execution_failed"
    assert child.error_summary == "RuntimeError"


def test_aggregate_combines_detached_spm_receipts() -> None:
    selection = {
        "forecast_content_sha256": "f" * 64,
        "scenario": "official",
        "geography_kind": "national",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
    }

    def receipt():
        return {
            "forecast_id": "forecast-1",
            "forecast_sha256": "f" * 64,
            "scenario": "official",
            "geography_kind": "national",
            "runtime_versions": {"spm-calculator": "0.3.1"},
            "years": {"2026": {}},
            "geographies": [],
            "composition_method": "direct",
            "storage_method": "detached",
        }

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
        .model_copy(
            update={
                "calculation_provenance": {
                    "spm_config": selection,
                    "spm_provenance": receipt(),
                }
            }
        )
    )
    reform = (
        FakeArtifacts()
        .add_simulation(report.reform)
        .model_copy(
            update={
                "calculation_provenance": {
                    "spm_config": selection,
                    "spm_provenance": receipt(),
                }
            }
        )
    )

    result = _build_spm_result(
        report=report,
        baseline_descriptor=baseline,
        reform_descriptor=reform,
    )

    assert result["spm_config"] == selection
    assert len(result["spm_provenance"]["baseline"]) == 1
    assert len(result["spm_provenance"]["reform"]) == 1


def test_single_worker_rejects_combined_baseline_and_reform_input() -> None:
    payload = _simulation(SimulationRole.BASELINE).model_dump(mode="json")
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
    def __init__(self, object_id, result):
        self.object_id = object_id
        self.result = result

    def get(self, *, timeout=None):
        return self.result


class ConcurrentInvoker:
    def __init__(
        self,
        artifacts,
        *,
        fail_role=None,
        incompatible=False,
        production_result=None,
    ):
        self.artifacts = artifacts
        self.fail_role = fail_role
        self.incompatible = incompatible
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.intervals = {}
        self.events = []
        self.environments = []
        self.production_result = production_result

    def spawn(self, *, simulation, environment, **_):
        parsed = SimulationExecutionInput.model_validate(simulation)
        role = parsed.role.value
        self.events.append(f"spawn:{role}")
        self.environments.append(environment)

        return self._submit(parsed, role)

    def restore(self, invocation_id):
        if invocation_id == "production-job-1":
            self.events.append("restore:production")
            return ImmediateCall(invocation_id, self.production_result)
        role = invocation_id.removeprefix("call-")
        parsed = _simulation(SimulationRole(role))
        self.events.append(f"restore:{role}")
        return self._submit(parsed, role)

    def _submit(self, parsed, role):

        def run():
            started = time.monotonic()
            time.sleep(0.05)
            if role == self.fail_role:
                raise RuntimeError("child failed with sensitive values")
            frames = _frames(100.0 if role == "baseline" else 120.0)
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
        return {"result": "complete"}

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
    result = coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=aggregate,
    )

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert invoker.environments == ["main", "main"]
    latest_start = max(interval[0] for interval in invoker.intervals.values())
    earliest_end = min(interval[1] for interval in invoker.intervals.values())
    assert latest_start < earliest_end
    assert result["artifact"]["uri"] == "gs://private/aggregate.json"
    assert observed["baseline_frames"]["household"][
        "household_net_income"
    ].tolist() == [100.0]
    assert observed["reform_frames"]["household"]["household_net_income"].tolist() == [
        120.0
    ]
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
        aggregator=lambda **_: {"result": "complete"},
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
        aggregator=lambda **_: {"must": "not run"},
    )

    assert duplicate == {
        "deduplicated": True,
        "evaluation_id": str(EVALUATION_ID),
        "status": "succeeded",
    }
    assert duplicate_invoker.events == []


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
        aggregator=lambda **_: {"result": stage12_result},
    )

    assert invoker.events[-1] == "restore:production"
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
        aggregator=lambda **_: {"result": {"budget": 100}},
    )

    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.aggregation_status is ComparisonRunAggregationStatus.SUCCEEDED
    assert store.parent.comparison_status is ResultComparisonStatus.FAILED
    assert store.parent.comparison_error_code == "result_comparison_failed"
    assert artifacts.comparison_writes == []


def test_comparison_retry_clears_prior_failure_metadata() -> None:
    store = FakeStore()
    parent = _automatic_parent().model_copy(
        update={
            "comparison_status": ResultComparisonStatus.FAILED,
            "comparison_completed_at": NOW,
            "comparison_error_code": "result_comparison_failed",
            "comparison_error_summary": "TimeoutError",
        }
    )
    artifacts = FakeArtifacts()
    result = {"budget": {"total": 100.0}}
    invoker = ConcurrentInvoker(artifacts, production_result=result)
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
        aggregator=lambda **_: {"result": result},
    )

    assert store.parent.comparison_status is ResultComparisonStatus.MATCHED
    assert store.parent.comparison_error_code is None
    assert store.parent.comparison_error_summary is None
    assert store.parent.comparison_output_uri == "gs://private/comparison.json"


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
            aggregator=lambda **_: {"must": "not run"},
        )

    assert "sensitive" not in str(error.value)

    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == "RuntimeError"
    assert "sensitive" not in store.parent.error_summary
    failed_child = store.children[REFORM_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_invocation_failed"
    assert failed_child.error_summary == "RuntimeError"


def test_coordinator_records_failure_when_child_persistence_fails() -> None:
    class FailingChildStore(FakeStore):
        def create_or_resolve_simulation(self, record):
            raise RuntimeError("sensitive database detail")

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
    assert store.parent.error_summary == "RuntimeError"


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
            aggregator=lambda **_: {"must": "not run"},
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
        aggregator=lambda **_: {"result": "complete"},
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
        aggregator=lambda **_: {"result": "complete"},
    )

    assert invoker.events == ["restore:baseline", "spawn:reform"]


class TimeoutCall:
    def __init__(self, object_id):
        self.object_id = object_id

    def get(self, *, timeout=None):
        raise TimeoutError("contains sensitive timeout details")


class TimeoutInvoker:
    def __init__(self):
        self.events = []

    def spawn(self, *, simulation, **_):
        role = SimulationExecutionInput.model_validate(simulation).role.value
        self.events.append(f"spawn:{role}")
        return TimeoutCall(f"call-{role}")

    def restore(self, invocation_id):
        raise AssertionError(f"unexpected restored invocation {invocation_id}")


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
            aggregator=lambda **_: {"must": "not run"},
        )

    assert "sensitive" not in str(error.value)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == "TimeoutError"
    timed_out_child = store.children[BASELINE_ID]
    assert timed_out_child.status is ComparisonRunLifecycleStatus.FAILED
    assert timed_out_child.error_summary == "TimeoutError"
