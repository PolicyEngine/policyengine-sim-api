"""Tests for acknowledgement-only Stage 12 dispatch."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid5

import pytest
from conftest import make_settings
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
)
from policyengine_simulation_entry import stage12_backend as backend_module
from policyengine_simulation_entry.stage12_adapter import adapt_annual_comparison
from policyengine_simulation_entry.stage12_backend import (
    ModalReportInvoker,
    Stage12ComparisonBackend,
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequestError,
)
from stage12_fixtures import eligible_payload, worker

OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000012"


class FakeLoader:
    def __init__(self, *, household_only: tuple[str, ...] = ()):
        self.worker = worker(household_only=household_only)
        self.resolved_versions = []

    def resolve(self, version=None):
        assert version in {None, "5.2.0"}
        self.resolved_versions.append(version)
        return "5.2.0", self.worker, "c" * 64


class FakeStore:
    def __init__(self, *, current: ComparisonReportRecord | None = None):
        self.current = current
        self.events = []

    def get_report(self, evaluation_id):
        self.events.append(("get_report", evaluation_id))
        if self.current is None or self.current.evaluation_id != evaluation_id:
            msg = "missing report"
            raise LookupError(msg)
        return self.current

    def list_simulations(self, evaluation_id):
        self.events.append(("list_simulations", evaluation_id))
        return ()


class FakeInvoker:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def spawn(self, **call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return "modal-call-1"


def _backend(
    store,
    invoker,
    *,
    environment="staging",
    modal_environment="staging",
    household_only=(),
):
    runtime = MagicMock()
    runtime.capture_context.return_value = {
        "observability_id": "00000000-0000-4000-8000-000000000099",
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
    }
    return Stage12ComparisonBackend(
        make_settings(
            environment=environment,
            stage12_v2_manifest_environment=modal_environment,
        ),
        manifest_loader=FakeLoader(household_only=household_only),
        store=store,
        invoker=invoker,
        runtime=runtime,
    )


def _response(job_id="job-1") -> bytes:
    return json.dumps({"status": "submitted", "job_id": job_id}).encode()


@pytest.mark.asyncio
async def test_modal_invoker_submits_report_context_and_pending_parent(
    monkeypatch,
) -> None:
    calls = []

    async def spawn_aio(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(object_id="modal-call-1")

    def from_name(*args, **kwargs):
        assert args == (
            "policyengine-simulation-v2-py5-2-0",
            "coordinate_report",
        )
        assert kwargs == {"environment_name": "staging"}
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn_aio))

    monkeypatch.setattr(backend_module.modal.Function, "from_name", from_name)

    invocation_id = await ModalReportInvoker("staging").spawn(
        worker=worker(),
        report_payload={"report": "payload"},
        context_payload={"context": "payload"},
        parent_payload={"parent": "payload"},
        observability_context={"traceparent": "trace"},
    )

    assert invocation_id == "modal-call-1"
    assert calls == [
        (
            (
                {"report": "payload"},
                {"context": "payload"},
                {"parent": "payload"},
            ),
            {"observability_context": {"traceparent": "trace"}},
        )
    ]


def test_automatic_submission_only_awaits_modal_acknowledgement() -> None:
    store = FakeStore()
    invoker = FakeInvoker()

    asyncio.run(
        _backend(store, invoker).dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert store.events == []
    assert len(invoker.calls) == 1
    call = invoker.calls[0]
    parent = ComparisonReportRecord.model_validate(call["parent_payload"])
    assert parent.status is ComparisonRunLifecycleStatus.PENDING
    assert parent.production_identity == "job-1"
    assert parent.coordinator_invocation_id is None
    assert call["context_payload"]["request_id"] == "request-1"
    assert call["context_payload"]["production_function_call_id"] == "job-1"
    assert call["context_payload"]["simulation_callable"] == (
        "run_single_simulation_us"
    )
    assert call["report_payload"]["baseline"]["role"] == "baseline"
    assert call["report_payload"]["reform"]["role"] == "reform"


def test_repeated_automatic_submission_uses_one_deterministic_report_identity() -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker)

    for request_id in ("request-1", "request-2"):
        asyncio.run(
            backend.dispatch_after_production(
                request_payload=eligible_payload(),
                production_response=_response(),
                request_id=request_id,
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert len(invoker.calls) == 2
    assert (
        invoker.calls[0]["report_payload"]["evaluation_id"]
        == invoker.calls[1]["report_payload"]["evaluation_id"]
    )
    assert (
        invoker.calls[0]["report_payload"]["baseline"]["simulation_execution_id"]
        == invoker.calls[1]["report_payload"]["baseline"]["simulation_execution_id"]
    )


def test_cliff_automatic_input_dispatches_without_entrypoint_writes() -> None:
    store = FakeStore()
    invoker = FakeInvoker()

    asyncio.run(
        _backend(store, invoker).dispatch_after_production(
            request_payload={**eligible_payload(), "include_cliffs": True},
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert store.events == []
    assert len(invoker.calls) == 1
    report = invoker.calls[0]["report_payload"]
    assert report["baseline"]["options"] == {"include_cliffs": True}
    assert report["reform"]["options"] == {"include_cliffs": True}


def test_dispatch_failure_is_sanitized_without_database_cleanup() -> None:
    store = FakeStore()
    invoker = FakeInvoker(error=RuntimeError("contains sensitive input"))

    with pytest.raises(
        TemporaryStage12DispatchFailed,
        match="Stage 12 comparison-run dispatch failed",
    ) as error:
        asyncio.run(
            _backend(store, invoker).dispatch_after_production(
                request_payload=eligible_payload(),
                production_response=_response(),
                request_id="request-1",
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert "sensitive" not in str(error.value)
    assert error.value.report.status is ComparisonRunLifecycleStatus.FAILED
    assert error.value.report.error_code == "comparison_dispatch_failed"
    assert error.value.report.error_summary == "RuntimeError"
    assert store.events == []


@pytest.mark.asyncio
async def test_dispatch_cancellation_does_not_wait_for_database_cleanup() -> None:
    class BlockingInvoker(FakeInvoker):
        async def spawn(self, **call):
            self.calls.append(call)
            await asyncio.Event().wait()

    store = FakeStore()
    backend = _backend(store, BlockingInvoker())
    task = asyncio.create_task(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.events == []


def test_temporary_submission_spawns_without_database_writes() -> None:
    store = FakeStore()
    invoker = FakeInvoker()

    report = asyncio.run(
        _backend(store, invoker).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="manual-request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert store.events == []
    assert report.status is ComparisonRunLifecycleStatus.RUNNING
    assert report.observability_id == OBSERVABILITY_ID
    assert report.production_identity == f"direct:{report.evaluation_id}"
    assert report.incumbent_execution_id is None
    assert report.coordinator_invocation_id == "modal-call-1"
    call = invoker.calls[0]
    assert call["context_payload"]["request_id"] == "manual-request-1"
    assert call["context_payload"]["environment"] == "staging"
    assert call["context_payload"]["modal_environment"] == "staging"
    assert call["context_payload"]["production_function_call_id"] is None
    submitted_parent = ComparisonReportRecord.model_validate(call["parent_payload"])
    assert submitted_parent.status is ComparisonRunLifecycleStatus.PENDING
    assert submitted_parent.observability_id == report.observability_id
    assert submitted_parent.coordinator_invocation_id is None
    assert call["observability_context"]["observability_id"] == report.observability_id


def test_production_context_keeps_logical_and_modal_environments_distinct() -> None:
    invoker = FakeInvoker()
    backend = _backend(
        FakeStore(),
        invoker,
        environment="production",
        modal_environment="main",
    )

    report = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="production-manual-request",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert report.environment == "production"
    context = invoker.calls[0]["context_payload"]
    assert context["environment"] == "production"
    assert context["modal_environment"] == "main"


def test_each_temporary_submission_creates_a_distinct_report_identity() -> None:
    backend = _backend(FakeStore(), FakeInvoker())

    first = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )
    second = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-2",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert first.evaluation_id != second.evaluation_id
    assert first.production_identity != second.production_identity


def test_temporary_submission_rejects_unsupported_input_without_side_effects() -> None:
    store = FakeStore()
    invoker = FakeInvoker()

    with pytest.raises(TemporaryStage12UnsupportedRequestError) as error:
        asyncio.run(
            _backend(store, invoker).submit_temporary_report(
                request_payload={**eligible_payload(), "include_cliffs": "true"},
                request_id="request-1",
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert error.value.reason == "unsupported_request_shape"
    assert store.events == []
    assert invoker.calls == []


def test_temporary_submission_rejects_a_country_without_economy_reports() -> None:
    store = FakeStore()
    invoker = FakeInvoker()

    with pytest.raises(TemporaryStage12UnsupportedRequestError) as error:
        asyncio.run(
            _backend(store, invoker, household_only=("us",)).submit_temporary_report(
                request_payload=eligible_payload(),
                request_id="request-1",
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert error.value.reason == "unsupported_country"
    assert store.events == []
    assert invoker.calls == []


def test_automatic_dispatch_skips_a_country_without_economy_reports() -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker, household_only=("us",))

    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert invoker.calls == []
    backend._runtime.event.assert_called_once_with(
        "stage12_comparison_run_skipped",
        attributes={
            "request_id": "request-1",
            "production_identity": "job-1",
            "reason": "unsupported_country",
        },
    )


def test_temporary_status_reads_coordinator_owned_postgres_state() -> None:
    store = FakeStore()
    invoker = FakeInvoker()
    report = asyncio.run(
        _backend(store, invoker).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )
    store.current = report

    resolved, simulations = _backend(store, invoker)._get_temporary_report(
        report.evaluation_id
    )

    assert resolved == report
    assert simulations == ()
    assert [event[0] for event in store.events] == [
        "get_report",
        "list_simulations",
    ]


def test_temporary_status_rejects_a_record_from_another_environment() -> None:
    staging_backend = _backend(FakeStore(), FakeInvoker())
    report = asyncio.run(
        staging_backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )
    store = FakeStore(current=report)
    production_backend = _backend(
        store,
        FakeInvoker(),
        environment="production",
        modal_environment="main",
    )

    with pytest.raises(LookupError, match="does not exist"):
        production_backend._get_temporary_report(report.evaluation_id)


@pytest.mark.asyncio
async def test_temporary_submission_offloads_only_pure_preparation(monkeypatch) -> None:
    backend = _backend(FakeStore(), FakeInvoker())
    calls = []
    original = backend._prepare_temporary_report

    async def fake_to_thread(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        return function(*args, **kwargs)

    monkeypatch.setattr(backend_module.asyncio, "to_thread", fake_to_thread)

    report = await backend.submit_temporary_report(
        request_payload=eligible_payload(),
        request_id="request-1",
        observability_id=OBSERVABILITY_ID,
    )

    assert calls == [
        (
            original,
            (),
            {
                "request_payload": eligible_payload(),
                "request_id": "request-1",
            },
        )
    ]
    assert report.status is ComparisonRunLifecycleStatus.RUNNING


FIXED_NOW = datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
TEMPORARY_EVALUATION_ID = UUID("00000000-0000-4000-8000-0000000000aa")


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        assert tz is UTC
        return FIXED_NOW


def _expected_parent(evaluation_id, **overrides) -> dict:
    return {
        "contract_version": 1,
        "evaluation_id": str(evaluation_id),
        "status": "pending",
        "aggregation_status": "not_started",
        "environment": "staging",
        "calculation_flow": "economy",
        "originating_request_id": "request-1",
        "observability_id": OBSERVABILITY_ID,
        "production_identity": "job-1",
        "incumbent_execution_id": "job-1",
        "worker_version": "5.2.0",
        "modal_application": "policyengine-simulation-v2-py5-2-0",
        "report_coordinator_callable": "coordinate_report",
        "version_manifest_sha256": "c" * 64,
        "policyengine_version": "5.2.0",
        "country_package_name": "policyengine-us",
        "country_package_version": "1.764.6",
        "country": "us",
        "dataset_identity": "populace_us_2024",
        "dataset_uri": (
            "hf://policyengine/data/populace_us_2024.h5@populace_us_2024-revision"
        ),
        "data_package_name": "populace-data",
        "data_package_version": "0.1.0",
        "data_artifact_revision": "populace_us_2024-revision",
        "coordinator_invocation_id": None,
        "error_code": None,
        "error_summary": None,
        "aggregate_output_uri": None,
        "aggregate_output_sha256": None,
        "aggregate_schema_version": None,
        "comparison_status": "pending",
        "comparison_output_uri": None,
        "comparison_output_sha256": None,
        "comparison_schema_version": None,
        "comparison_completed_at": None,
        "comparison_error_code": None,
        "comparison_error_summary": None,
        "created_at": "2026-09-22T12:30:00Z",
        "updated_at": "2026-09-22T12:30:00Z",
        "started_at": None,
        "completed_at": None,
        "retention_expires_at": "2026-10-22T12:30:00Z",
        **overrides,
    }


def _expected_context(evaluation_id, **overrides) -> dict:
    return {
        "request_id": "request-1",
        "environment": "staging",
        "modal_environment": "staging",
        "worker_version": "5.2.0",
        "modal_application": "policyengine-simulation-v2-py5-2-0",
        "simulation_callable": "run_single_simulation_us",
        "version_manifest_sha256": "c" * 64,
        "bundle_manifest_sha256": "b" * 64,
        "artifact_prefix": f"stage-12-runs/staging/2026/09/{evaluation_id}",
        "production_function_call_id": "job-1",
        "created_at": "2026-09-22T12:30:00+00:00",
        "retention_expires_at": "2026-10-22T12:30:00+00:00",
        **overrides,
    }


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(backend_module, "datetime", FixedClock)
    monkeypatch.setattr(backend_module, "uuid4", lambda: TEMPORARY_EVALUATION_ID)


@pytest.mark.usefixtures("fixed_clock")
def test_automatic_dispatch_sends_the_complete_coordinator_call() -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker)

    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    [call] = invoker.calls
    evaluation_id = call["report_payload"]["evaluation_id"]
    assert evaluation_id == str(
        uuid5(
            backend_module.AUTOMATIC_REPORT_NAMESPACE,
            f"staging|economy|job-1|5.2.0|{'c' * 64}",
        )
    )
    assert call["worker"] == backend._manifest_loader.worker
    assert call["report_payload"] == (
        adapt_annual_comparison(
            eligible_payload(),
            evaluation_id=UUID(evaluation_id),
            worker=worker(),
        ).report.model_dump(mode="json")
    )
    assert call["parent_payload"] == _expected_parent(evaluation_id)
    assert call["context_payload"] == _expected_context(evaluation_id)
    assert call["observability_context"] == {
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        "observability_id": OBSERVABILITY_ID,
    }
    assert backend._manifest_loader.resolved_versions == [None]
    backend._runtime.span.assert_called_once_with(
        "stage12_entry_dispatch",
        attributes={
            "evaluation_id": evaluation_id,
            "execution_mode": "shadow",
            "runner_name": "stage12",
        },
    )
    backend._runtime.event.assert_called_once_with(
        "stage12_comparison_run_dispatched",
        attributes={
            "request_id": "request-1",
            "evaluation_id": evaluation_id,
            "production_identity": "job-1",
            "worker_version": "5.2.0",
            "modal_application": "policyengine-simulation-v2-py5-2-0",
            "coordinator_invocation_id": "modal-call-1",
        },
    )


@pytest.mark.usefixtures("fixed_clock")
def test_temporary_submission_returns_the_complete_acknowledged_parent() -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker)

    report = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    direct_identity = f"direct:{TEMPORARY_EVALUATION_ID}"
    [call] = invoker.calls
    assert call["parent_payload"] == _expected_parent(
        TEMPORARY_EVALUATION_ID,
        production_identity=direct_identity,
        incumbent_execution_id=None,
        comparison_status="not_requested",
    )
    assert call["context_payload"] == _expected_context(
        TEMPORARY_EVALUATION_ID, production_function_call_id=None
    )
    assert report.model_dump(mode="json") == _expected_parent(
        TEMPORARY_EVALUATION_ID,
        status="running",
        production_identity=direct_identity,
        incumbent_execution_id=None,
        comparison_status="not_requested",
        coordinator_invocation_id="modal-call-1",
        started_at="2026-09-22T12:30:00Z",
    )
    backend._runtime.span.assert_called_once_with(
        "stage12_entry_dispatch",
        attributes={
            "evaluation_id": str(TEMPORARY_EVALUATION_ID),
            "execution_mode": "authoritative",
            "runner_name": "stage12",
        },
    )


def test_automatic_dispatch_skips_unsupported_requests_with_an_event() -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker)

    asyncio.run(
        backend.dispatch_after_production(
            request_payload={**eligible_payload(), "scope": "household"},
            production_response=_response(),
            request_id="request-1",
            observability_id=OBSERVABILITY_ID,
        )
    )

    assert invoker.calls == []
    backend._runtime.event.assert_called_once_with(
        "stage12_comparison_run_skipped",
        attributes={
            "request_id": "request-1",
            "production_identity": "job-1",
            "reason": "unsupported_scope",
        },
    )


@pytest.mark.parametrize("response", [b"[]", b'{"job_id": ""}', b'{"job_id": 7}'])
def test_automatic_dispatch_requires_a_production_job_identifier(response) -> None:
    invoker = FakeInvoker()
    backend = _backend(FakeStore(), invoker)

    with pytest.raises(
        ValueError, match=r"^accepted production response has no job identifier$"
    ):
        asyncio.run(
            backend.dispatch_after_production(
                request_payload=eligible_payload(),
                production_response=response,
                request_id="request-1",
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert invoker.calls == []
    assert backend._manifest_loader.resolved_versions == []
