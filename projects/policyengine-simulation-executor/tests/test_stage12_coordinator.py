"""Tests for Stage 12 report coordination: starting, aggregating and comparing."""

from __future__ import annotations

import io
import json

from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    ServiceIdentity,
    StdoutLogDestination,
    configure,
)
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ResultComparisonStatus,
)
from policyengine_simulation_executor.stage12_runtime import (
    CoordinatorDependencies,
    CoordinatorInvocation,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordinate_report as coordinate_report_impl,
)
from stage12_child_call_support import ConcurrentInvoker, ContextRecordingInvoker
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    EVALUATION_ID,
    NOW,
    RecordingRuntime,
    aggregate_payload,
    automatic_parent_record,
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
    shared_output_plan,
)


def test_coordinator_starts_both_children_before_waiting_and_aggregates() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)
    observed = {}

    def aggregate(**values):
        observed.update(values)
        return aggregate_payload()

    context = invocation_context().model_copy(
        update={
            "environment": "production",
            "modal_environment": "main",
            "artifact_prefix": (
                "stage-12-runs/production/2026/09/00000000-0000-0000-0000-000000000001"
            ),
        }
    )
    parent = parent_record().model_copy(update={"environment": "production"})
    resolver_calls = []

    def resolver(report):
        resolver_calls.append(report.evaluation_id)
        return shared_output_plan()

    result = coordinate_report_impl(
        report_execution_input().model_dump(mode="json"),
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
    assert invoker.output_plans == [shared_output_plan(), shared_output_plan()]
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
    context = invocation_context()
    parent = parent_record()
    first_invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=first_invoker,
        aggregator=lambda **_: aggregate_payload(),
    )
    duplicate_invoker = ConcurrentInvoker(artifacts)
    duplicate = coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-2",
        store=store,
        artifacts=artifacts,
        invoker=duplicate_invoker,
        aggregator=lambda **_: aggregate_payload({"must": "not run"}),
    )

    assert duplicate == {
        "deduplicated": True,
        "evaluation_id": str(EVALUATION_ID),
        "status": "succeeded",
    }
    assert duplicate_invoker.events == []


def test_stage12_children_receive_the_context_without_a_delivery_field() -> None:
    artifacts = FakeArtifacts()
    invoker = ContextRecordingInvoker(artifacts)

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=FakeStore(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload(),
    )

    # The Stage 12 worker payload keeps exactly the fields it had before the
    # delivery field existed.
    fields_before_the_delivery_field = {
        "contract_version",
        "request_id",
        "environment",
        "modal_environment",
        "worker_version",
        "modal_application",
        "simulation_callable",
        "version_manifest_sha256",
        "bundle_manifest_sha256",
        "artifact_prefix",
        "production_function_call_id",
        "created_at",
        "retention_expires_at",
    }
    assert [set(context) for context in invoker.child_contexts] == [
        fields_before_the_delivery_field,
        fields_before_the_delivery_field,
    ]


def test_coordinator_measures_output_and_child_input_planning() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    runtime = RecordingRuntime()

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=ConcurrentInvoker(artifacts),
        aggregator=lambda **_: aggregate_payload(),
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
                report_execution_input().model_dump(mode="json"),
                invocation_context().model_dump(mode="json"),
                parent_record().model_dump(mode="json"),
                application_name=invocation_context().modal_application,
                coordinator_invocation_id="coordinator-1",
                store=FakeStore(),
                artifacts=artifacts,
                invoker=ConcurrentInvoker(artifacts),
                aggregator=lambda **_: aggregate_payload(),
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
    parent = automatic_parent_record()
    artifacts = FakeArtifacts()
    production_result = {"budget": {"total": 100.0, "count": 2}}
    stage12_result = {"budget": {"total": 101.0, "count": 2}}
    invoker = ConcurrentInvoker(
        artifacts,
        production_result=production_result,
    )
    context = invocation_context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload(stage12_result),
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
    parent = automatic_parent_record()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts, production_result="not-an-object")
    context = invocation_context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload({"budget": 100}),
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
    context = invocation_context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        automatic_parent_record().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=UnavailableProductionInvoker(artifacts),
        aggregator=lambda **_: aggregate_payload({"budget": 100}),
    )

    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert store.parent.comparison_status is ResultComparisonStatus.FAILED
    assert store.parent.comparison_error_summary == (
        "ConnectionError: production result unavailable at "
        "https://storage.example.com/result.json?[REDACTED]"
    )


def test_repeated_submission_does_not_retry_failed_comparison() -> None:
    store = FakeStore()
    parent = automatic_parent_record()
    artifacts = FakeArtifacts()
    context = invocation_context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )

    first_invoker = ConcurrentInvoker(artifacts, production_result="not-an-object")
    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=first_invoker,
        aggregator=lambda **_: aggregate_payload({"budget": {"total": 100.0}}),
    )
    failed_at = store.parent.comparison_completed_at
    failed_summary = store.parent.comparison_error_summary

    second_invoker = ConcurrentInvoker(
        artifacts,
        production_result={"budget": {"total": 100.0}},
    )
    result = coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id="coordinator-2",
        store=store,
        artifacts=artifacts,
        invoker=second_invoker,
        aggregator=lambda **_: aggregate_payload({"budget": {"total": 100.0}}),
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
