"""Tests for acknowledgement-only Stage 12 dispatch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import make_settings
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
)
from stage12_fixtures import eligible_payload, worker

from policyengine_simulation_entry import stage12_backend as backend_module
from policyengine_simulation_entry.stage12_backend import (
    ModalReportInvoker,
    Stage12ComparisonBackend,
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequest,
)

OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000012"


class FakeLoader:
    def __init__(self):
        self.worker = worker()
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
            raise LookupError("missing report")
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


def _backend(store, invoker, *, environment="staging", modal_environment="staging"):
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
        manifest_loader=FakeLoader(),
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

    async def spawn_aio(*args):
        calls.append(args)
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
            {"report": "payload"},
            {"context": "payload"},
            {"parent": "payload"},
            {"traceparent": "trace"},
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


def test_unsupported_automatic_input_does_not_dispatch_or_write() -> None:
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
    assert invoker.calls == []


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

    with pytest.raises(TemporaryStage12UnsupportedRequest) as error:
        asyncio.run(
            _backend(store, invoker).submit_temporary_report(
                request_payload={**eligible_payload(), "include_cliffs": True},
                request_id="request-1",
                observability_id=OBSERVABILITY_ID,
            )
        )

    assert error.value.reason == "unsupported_cliff_calculation"
    assert store.events == []
    assert invoker.calls == []


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
