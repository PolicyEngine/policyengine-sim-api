"""Characterization tests for Stage 12 report coordination state and order."""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ResultComparisonStatus,
)
from policyengine_simulation_executor.stage12_runtime import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    coordination,
)
from test_stage12_runtime import (
    BASELINE_ID,
    EVALUATION_ID,
    NOW,
    REFORM_ID,
    ConcurrentInvoker,
    FakeArtifacts,
    FakeStore,
    _aggregate_payload,
    _child,
    _context,
    _output_plan,
    _parent,
    _report,
    coordinate_report,
)

CLAIMED_AT = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
PLACEHOLDER_ID = UUID("00000000-0000-4000-8000-0000000000ff")
# The redacted causes the report and its children record for these failures.
DISPATCH_FAILURE_SUMMARY = (
    "ConnectionError in stage12_child_dispatch (baseline): "
    "dispatch rejected with sensitive detail"
)
RETAINED_DIGEST_FAILURE_SUMMARY = (
    "ValueError after stage12_child_state_create: "
    "retained child artifact digest mismatch"
)
# report_failure ends every recorded summary with the failure's correlation ID.
CORRELATION_SUFFIX = r" \(correlation_id=[0-9a-f]{32}\)"


def _assert_failure_summary(recorded: str | None, expected: str) -> None:
    assert recorded is not None
    assert re.fullmatch(re.escape(expected) + CORRELATION_SUFFIX, recorded), recorded


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        assert tz is UTC
        return CLAIMED_AT


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(coordination, "datetime", FixedClock)
    monkeypatch.setattr(coordination, "uuid4", lambda: PLACEHOLDER_ID)


class TracingRuntime:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def operation(self, name, *, attributes=None, **_kwargs):
        self.events.append(("operation", name, dict(attributes or {})))
        yield

    def span(self, name, *, attributes=None):
        self.events.append(("span", name, dict(attributes or {})))
        return nullcontext()

    def capture_context(self):
        self.events.append(("capture_context", "", {}))
        return {"traceparent": "trace"}


class FailingSpawnInvoker:
    def __init__(self, store=None, succeeded_child=None):
        self.store = store
        self.succeeded_child = succeeded_child
        self.spawns = []

    def spawn(self, **call):
        self.spawns.append(call)
        if self.succeeded_child is not None:
            self.store.children[self.succeeded_child.simulation_execution_id] = (
                self.succeeded_child
            )
        msg = "dispatch rejected with sensitive detail"
        raise ConnectionError(msg)

    def restore(self, invocation_id):
        msg = f"unexpected restore {invocation_id}"
        raise AssertionError(msg)


def _coordinate(context=None, parent=None, **dependencies):
    context = context or _context()
    dependencies.setdefault("application_name", context.modal_application)
    dependencies.setdefault("coordinator_invocation_id", "coordinator-1")
    dependencies.setdefault("aggregator", lambda **_: _aggregate_payload())
    dependencies.setdefault("artifacts", FakeArtifacts())
    dependencies.setdefault("invoker", ConcurrentInvoker(dependencies["artifacts"]))
    return coordinate_report(
        _report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        (parent or _parent()).model_dump(mode="json"),
        **dependencies,
    )


def _succeeded_child(simulation, artifacts):
    descriptor = artifacts.add_simulation(simulation)
    return _child(simulation).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": descriptor.artifact.uri,
            "output_sha256": descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": descriptor.row_identity.identifier_columns,
            "row_count": descriptor.row_identity.row_count,
            "row_identity_sha256": descriptor.row_identity.identity_sha256,
            "completed_at": NOW,
        }
    )


@pytest.mark.parametrize(
    ("context_update", "application_name", "message"),
    [
        (
            {},
            "another-application",
            "report coordinator application differs from dispatch context",
        ),
        (
            {"worker_version": "9.9.9"},
            None,
            "report worker version differs from dispatch context",
        ),
        (
            {"bundle_manifest_sha256": "c" * 64},
            None,
            "report bundle digest differs from dispatch context",
        ),
    ],
)
def test_dispatch_context_must_match_the_report(
    context_update, application_name, message
):
    store = FakeStore()
    context = _context().model_copy(update=context_update)

    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _coordinate(
            context,
            store=store,
            application_name=application_name or context.modal_application,
        )

    assert store.parent is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evaluation_id", UUID("00000000-0000-0000-0000-0000000000aa")),
        ("environment", "production"),
        ("originating_request_id", "request-2"),
        ("worker_version", "5.3.0"),
        ("modal_application", "another-application"),
        ("version_manifest_sha256", "c" * 64),
        ("policyengine_version", "5.3.0"),
        ("country_package_name", "policyengine-uk"),
        ("country_package_version", "9.9.9"),
        ("country", "uk"),
        ("dataset_identity", "another-dataset"),
        ("dataset_uri", "hf://policyengine/data/another.h5@revision"),
        ("data_package_name", "another-data"),
        ("data_package_version", "9.9.9"),
        ("data_artifact_revision", "another-revision"),
        ("created_at", NOW + timedelta(seconds=1)),
        ("retention_expires_at", NOW + timedelta(days=29)),
        ("status", ComparisonRunLifecycleStatus.RUNNING),
        ("aggregation_status", ComparisonRunAggregationStatus.RUNNING),
        ("coordinator_invocation_id", "coordinator-0"),
        ("production_identity", "direct:another"),
        ("incumbent_execution_id", "production-job-1"),
        ("comparison_status", ResultComparisonStatus.PENDING),
    ],
)
def test_submitted_direct_parent_must_describe_the_invocation(field, value):
    store = FakeStore()
    messages = {
        "status": "submitted parent must be pending",
        "aggregation_status": "submitted parent aggregation must not be started",
        "coordinator_invocation_id": (
            "submitted parent must not contain an invocation identifier"
        ),
        "production_identity": "direct parent identity does not match its report",
        "incumbent_execution_id": (
            "direct parent must not name a production execution"
        ),
        "comparison_status": "direct parent must not request result comparison",
    }

    message = messages.get(field, f"submitted parent {field} does not match invocation")

    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _coordinate(parent=_parent().model_copy(update={field: value}), store=store)

    assert store.parent is None


@pytest.mark.parametrize(
    "update",
    [
        {"production_identity": "production-job-2"},
        {"incumbent_execution_id": "production-job-2"},
        {"incumbent_execution_id": None},
    ],
)
def test_submitted_automatic_parent_must_name_the_production_job(update):
    store = FakeStore()
    context = _context().model_copy(
        update={"production_function_call_id": "production-job-1"}
    )
    parent = _parent().model_copy(
        update={
            "production_identity": "production-job-1",
            "incumbent_execution_id": "production-job-1",
            "comparison_status": ResultComparisonStatus.PENDING,
            **update,
        }
    )

    with pytest.raises(
        ValueError,
        match=r"^automatic parent does not match the production execution$",
    ):
        _coordinate(context, parent, store=store)

    assert store.parent is None


def test_existing_parent_with_another_identifier_is_rejected_unwrapped():
    store = FakeStore()
    store.parent = _parent().model_copy(
        update={"evaluation_id": UUID("00000000-0000-0000-0000-0000000000aa")}
    )

    with pytest.raises(
        ValueError,
        match=r"^existing comparison identity has a different report identifier$",
    ):
        _coordinate(store=store)


@pytest.mark.usefixtures("fixed_clock")
@pytest.mark.parametrize("previous_start", [None, NOW + timedelta(minutes=5)])
def test_failed_parent_is_reclaimed_keeping_its_first_start(previous_start):
    store = FakeStore()
    artifacts = FakeArtifacts()
    store.parent = _parent().model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.FAILED,
            "aggregation_status": ComparisonRunAggregationStatus.FAILED,
            "coordinator_invocation_id": "coordinator-0",
            "error_code": "report_coordination_failed",
            "error_summary": "TimeoutError",
            "started_at": previous_start,
            "completed_at": NOW + timedelta(minutes=10),
        }
    )
    replaced = []
    replace_report = store.replace_report

    def record_replace(record):
        replaced.append(record)
        return replace_report(record)

    store.replace_report = record_replace

    _coordinate(store=store, artifacts=artifacts, invoker=ConcurrentInvoker(artifacts))

    claimed = replaced[0]
    assert claimed.status is ComparisonRunLifecycleStatus.RUNNING
    assert claimed.aggregation_status is ComparisonRunAggregationStatus.RUNNING
    assert claimed.coordinator_invocation_id == "coordinator-1"
    assert claimed.error_code is None
    assert claimed.error_summary is None
    assert claimed.started_at == (previous_start or CLAIMED_AT)
    assert claimed.updated_at == CLAIMED_AT
    assert claimed.completed_at is None
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status",
    [
        ComparisonRunLifecycleStatus.RUNNING,
        ComparisonRunLifecycleStatus.SUCCEEDED,
        ComparisonRunLifecycleStatus.SKIPPED,
    ],
)
def test_claimed_or_finished_parent_is_deduplicated(status):
    store = FakeStore()
    existing = _parent().model_copy(update={"status": status, "error_code": "skipped"})
    if status is ComparisonRunLifecycleStatus.SUCCEEDED:
        existing = existing.model_copy(
            update={
                "aggregation_status": ComparisonRunAggregationStatus.SUCCEEDED,
                "aggregate_output_uri": "gs://private/aggregate.json",
                "aggregate_output_sha256": "d" * 64,
                "aggregate_schema_version": 1,
                "completed_at": NOW,
            }
        )
    store.parent = existing
    invoker = ConcurrentInvoker(FakeArtifacts())

    result = _coordinate(store=store, invoker=invoker)

    assert result == {
        "deduplicated": True,
        "evaluation_id": str(EVALUATION_ID),
        "status": status.value,
    }
    assert invoker.events == []
    assert store.parent is existing


def test_retained_child_with_a_changed_artifact_fails_the_report():
    report = _report()
    store = FakeStore()
    artifacts = FakeArtifacts()
    succeeded = _succeeded_child(report.baseline, artifacts)
    store.children[BASELINE_ID] = succeeded
    artifacts.payloads[succeeded.output_uri] = b"changed"
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match=r"^Stage 12 report coordination failed$"):
        _coordinate(store=store, artifacts=artifacts, invoker=invoker)

    assert invoker.events == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "report_coordination_failed"
    _assert_failure_summary(store.parent.error_summary, RETAINED_DIGEST_FAILURE_SUMMARY)
    assert store.children[BASELINE_ID] == succeeded


@pytest.mark.usefixtures("fixed_clock")
def test_dispatch_failure_marks_the_child_and_report_failed():
    store = FakeStore()
    invoker = FailingSpawnInvoker()

    with pytest.raises(RuntimeError) as error:
        _coordinate(store=store, artifacts=FakeArtifacts(), invoker=invoker)

    assert str(error.value) == "Stage 12 report coordination failed"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert len(invoker.spawns) == 1
    failed_child = store.children[BASELINE_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.modal_invocation_id == f"dispatch-pending-{PLACEHOLDER_ID}"
    assert failed_child.error_code == "simulation_dispatch_failed"
    _assert_failure_summary(failed_child.error_summary, DISPATCH_FAILURE_SUMMARY)
    assert failed_child.started_at == CLAIMED_AT
    assert failed_child.completed_at == CLAIMED_AT
    assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.PENDING
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.aggregation_status is ComparisonRunAggregationStatus.FAILED
    _assert_failure_summary(store.parent.error_summary, DISPATCH_FAILURE_SUMMARY)
    assert store.parent.completed_at == CLAIMED_AT


def test_dispatch_failure_keeps_a_child_that_succeeded_meanwhile():
    report = _report()
    store = FakeStore()
    artifacts = FakeArtifacts()
    succeeded = _succeeded_child(report.baseline, artifacts)
    invoker = FailingSpawnInvoker(store, succeeded)

    with pytest.raises(RuntimeError, match=r"^Stage 12 report coordination failed$"):
        _coordinate(store=store, artifacts=artifacts, invoker=invoker)

    assert store.children[BASELINE_ID] == succeeded
    _assert_failure_summary(store.parent.error_summary, DISPATCH_FAILURE_SUMMARY)


def test_default_output_plan_resolver_measures_its_own_stages(monkeypatch):
    from test_stage12_runtime import coordinate_report_impl

    store = FakeStore()
    artifacts = FakeArtifacts()
    runtime = TracingRuntime()
    resolved = []

    def resolve(report, *, stage_scope):
        with stage_scope(coordination.Stage.STAGE12_OUTPUT_PLANNING, {"probe": 1}):
            resolved.append(report.evaluation_id)
        return _output_plan()

    monkeypatch.setattr(coordination, "resolve_report_output_plan", resolve)

    coordinate_report_impl(
        _report().model_dump(mode="json"),
        _context().model_dump(mode="json"),
        _parent().model_dump(mode="json"),
        invocation=CoordinatorInvocation(
            application_name=_context().modal_application,
            invocation_id="coordinator-1",
        ),
        dependencies=CoordinatorDependencies(
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts),
            aggregator=lambda **_: _aggregate_payload(),
            runtime=runtime,
        ),
    )

    assert resolved == [EVALUATION_ID]
    assert ("operation", "stage12_output_planning", {"probe": 1}) in runtime.events


@pytest.mark.usefixtures("fixed_clock")
def test_successful_coordination_records_every_stage_and_state():
    store = FakeStore()
    artifacts = FakeArtifacts()
    runtime = TracingRuntime()
    aggregated = []

    def aggregate(**values):
        aggregated.append(sorted(values))
        return _aggregate_payload({"budget": 1})

    result = _coordinate(
        store=store,
        artifacts=artifacts,
        invoker=ConcurrentInvoker(artifacts),
        aggregator=aggregate,
        runtime=runtime,
    )

    assert aggregated == [
        ["baseline", "reform", "report", "uk_local_authority_metadata"]
    ]
    assert runtime.events == [
        ("span", "stage12_coordinator_claim", {}),
        ("operation", "stage12_coordinator_preparation", {}),
        ("operation", "stage12_output_planning", {"country": "us"}),
        ("operation", "stage12_child_input_planning", {"simulation_role": "baseline"}),
        ("operation", "stage12_child_input_planning", {"simulation_role": "reform"}),
        ("span", "stage12_child_state_create", {}),
        ("span", "stage12_child_dispatch", {"simulation_role": "baseline"}),
        ("capture_context", "", {}),
        ("span", "stage12_child_dispatch", {"simulation_role": "reform"}),
        ("capture_context", "", {}),
        ("span", "stage12_child_wait", {}),
        ("span", "stage12_artifact_read", {}),
        ("span", "stage12_aggregation", {}),
        ("span", "stage12_aggregate_artifact_write", {}),
    ]
    aggregate_artifact = result["artifact"]
    assert result == {
        "contract_version": 1,
        "evaluation_id": str(EVALUATION_ID),
        "aggregate_schema_version": 1,
        "artifact": aggregate_artifact,
        "baseline_artifact_sha256": result["baseline_artifact_sha256"],
        "reform_artifact_sha256": result["reform_artifact_sha256"],
        "bundle": _report().baseline.bundle.model_dump(mode="json"),
    }
    aggregate_uri = f"gs://private/{_context().artifact_prefix}/reports/aggregate.json"
    assert aggregate_artifact["uri"] == aggregate_uri
    parent = store.parent.model_dump(mode="json")
    expected_parent = _parent().model_dump(mode="json")
    expected_parent.update(
        {
            "status": "succeeded",
            "aggregation_status": "succeeded",
            "coordinator_invocation_id": "coordinator-1",
            "aggregate_output_uri": aggregate_uri,
            "aggregate_output_sha256": aggregate_artifact["content_sha256"],
            "aggregate_schema_version": 1,
            "started_at": "2026-09-15T01:00:00Z",
            "updated_at": "2026-09-15T01:00:00Z",
            "completed_at": "2026-09-15T01:00:00Z",
        }
    )
    assert parent == expected_parent
    for role, simulation_id in (("baseline", BASELINE_ID), ("reform", REFORM_ID)):
        child = store.children[simulation_id]
        assert child.status is ComparisonRunLifecycleStatus.RUNNING
        assert child.modal_invocation_id == f"call-{role}"
        assert child.started_at == CLAIMED_AT
        assert child.updated_at == CLAIMED_AT
        assert child.simulation_callable == "run_single_simulation_us"


def test_modal_child_invoker_spawns_and_restores_deployed_functions(monkeypatch):
    from types import SimpleNamespace

    from policyengine_simulation_executor.stage12_runtime import dependencies

    calls = []

    def from_name(*args, **kwargs):
        calls.append(("from_name", args, kwargs))

        def spawn(*spawn_args, **spawn_kwargs):
            calls.append(("spawn", spawn_args, spawn_kwargs))
            return "spawned-call"

        return SimpleNamespace(spawn=spawn)

    def from_id(invocation_id):
        calls.append(("from_id", (invocation_id,), {}))
        return "restored-call"

    fake_modal = SimpleNamespace(
        Function=SimpleNamespace(from_name=from_name),
        FunctionCall=SimpleNamespace(from_id=from_id),
    )

    def import_modal(module_name):
        assert module_name == "modal"
        return fake_modal

    monkeypatch.setattr(dependencies, "import_module", import_modal)
    invoker = dependencies.ModalChildInvoker()

    spawned = invoker.spawn(
        application_name="application",
        function_name="run_single_simulation_us",
        environment="staging",
        simulation={"role": "baseline"},
        context={"request_id": "request-1"},
        observability_context={"traceparent": "trace"},
    )
    restored = invoker.restore("call-1")

    assert (spawned, restored) == ("spawned-call", "restored-call")
    assert calls == [
        (
            "from_name",
            ("application", "run_single_simulation_us"),
            {"environment_name": "staging"},
        ),
        (
            "spawn",
            ({"role": "baseline"}, {"request_id": "request-1"}),
            {"observability_context": {"traceparent": "trace"}},
        ),
        ("from_id", ("call-1",), {}),
    ]
