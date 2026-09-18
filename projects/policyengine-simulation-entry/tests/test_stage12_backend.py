"""Tests for direct, durable Stage 12 comparison dispatch."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import make_settings
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
)
from stage12_fixtures import eligible_payload, worker

from policyengine_simulation_entry import stage12_backend as backend_module
from policyengine_simulation_entry.stage12_backend import (
    Stage12ComparisonBackend,
    TemporaryStage12UnsupportedRequest,
)


class FakeLoader:
    def __init__(self):
        self.worker = worker()
        self.resolved_versions = []

    def resolve(self, version=None):
        assert version in {None, "5.2.0"}
        self.resolved_versions.append(version)
        return "5.2.0", self.worker, "c" * 64


class FakeStore:
    def __init__(self, *, existing=None, conflict=None):
        self.existing = existing
        self.conflict = conflict
        self.current = existing or conflict
        self.created = []
        self.replaced = []
        self.events = []

    def get_report_for_production(self, **identity):
        self.events.append(("lookup", identity))
        return self.existing

    def get_report(self, evaluation_id):
        self.events.append(("get_report", evaluation_id))
        if self.current is None or self.current.evaluation_id != evaluation_id:
            raise LookupError("missing report")
        return self.current

    def list_simulations(self, evaluation_id):
        self.events.append(("list_simulations", evaluation_id))
        return ()

    def create_or_resolve_report(self, record):
        self.events.append(("create", record.evaluation_id))
        self.created.append(record)
        self.current = self.conflict or record
        return ComparisonReportPersistenceResult(
            record=self.conflict or record,
            created=self.conflict is None,
        )

    def replace_report(self, record):
        self.events.append(("replace", record.status))
        self.replaced.append(record)
        self.current = record
        return record

    def replace_report_result_comparison(self, record):
        self.events.append(("replace_comparison", record.comparison_status))
        self.current = record
        return record

    def attach_report_invocation(
        self,
        evaluation_id,
        *,
        expected_placeholder,
        modal_invocation_id,
        updated_at,
    ):
        self.events.append(("attach", modal_invocation_id))
        assert self.current.evaluation_id == evaluation_id
        assert self.current.coordinator_invocation_id == expected_placeholder
        self.current = self.current.model_copy(
            update={
                "coordinator_invocation_id": modal_invocation_id,
                "updated_at": updated_at,
            }
        )
        return self.current


class FakeInvoker:
    def __init__(self, *, error=None, events=None):
        self.error = error
        self.calls = []
        self.events = events

    async def spawn(self, **call):
        if self.events is not None:
            self.events.append(("spawn", call["worker"].application_name))
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return "modal-call-1"


def _backend(store, invoker):
    return Stage12ComparisonBackend(
        make_settings(
            environment="staging",
            stage12_v2_manifest_environment="staging",
        ),
        manifest_loader=FakeLoader(),
        store=store,
        invoker=invoker,
    )


def _response(job_id="job-1") -> bytes:
    return json.dumps({"status": "submitted", "job_id": job_id}).encode()


def test_parent_is_durable_before_direct_modal_dispatch() -> None:
    store = FakeStore()
    invoker = FakeInvoker(events=store.events)

    asyncio.run(
        _backend(store, invoker).dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
        )
    )

    operations = [event[0] for event in store.events]
    assert operations == ["lookup", "create", "replace", "spawn", "attach"]
    assert store.created[0].status is ComparisonRunLifecycleStatus.PENDING
    assert store.replaced[0].status is ComparisonRunLifecycleStatus.RUNNING
    assert store.replaced[0].coordinator_invocation_id.startswith("dispatch-pending-")
    assert store.current.status is ComparisonRunLifecycleStatus.RUNNING
    assert store.current.coordinator_invocation_id == "modal-call-1"
    assert invoker.calls[0]["context_payload"]["request_id"] == "request-1"
    assert invoker.calls[0]["context_payload"]["production_function_call_id"] == (
        "job-1"
    )
    assert (
        invoker.calls[0]["context_payload"]["simulation_callable"]
        == "run_single_simulation_us"
    )
    report = invoker.calls[0]["report_payload"]
    assert report["baseline"]["role"] == "baseline"
    assert report["reform"]["role"] == "reform"


def test_repeated_submission_for_existing_production_job_does_not_dispatch() -> None:
    existing_store = FakeStore()
    invoker = FakeInvoker()
    backend = _backend(existing_store, invoker)
    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
        )
    )
    existing_store.existing = existing_store.created[0]
    existing_store.created.clear()

    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-2",
        )
    )

    assert existing_store.created == []
    assert len(invoker.calls) == 1


def test_failed_dispatch_reuses_parent_version_and_deterministic_children() -> None:
    store = FakeStore()
    invoker = FakeInvoker()
    loader = FakeLoader()
    backend = Stage12ComparisonBackend(
        make_settings(environment="staging"),
        manifest_loader=loader,
        store=store,
        invoker=invoker,
    )
    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
        )
    )
    first_report = invoker.calls[0]["report_payload"]
    failed = store.replaced[-1].model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.FAILED,
            "aggregation_status": ComparisonRunAggregationStatus.FAILED,
            "error_code": "report_coordination_failed",
            "error_summary": "RuntimeError",
        }
    )
    store.existing = failed

    asyncio.run(
        backend.dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-2",
        )
    )

    retried_report = invoker.calls[1]["report_payload"]
    assert loader.resolved_versions == [None, "5.2.0"]
    assert retried_report["evaluation_id"] == first_report["evaluation_id"]
    assert (
        retried_report["baseline"]["simulation_execution_id"]
        == first_report["baseline"]["simulation_execution_id"]
    )
    assert (
        retried_report["reform"]["simulation_execution_id"]
        == first_report["reform"]["simulation_execution_id"]
    )
    assert invoker.calls[1]["context_payload"]["version_manifest_sha256"] == "c" * 64
    assert len(store.created) == 1


def test_concurrent_create_resolution_does_not_dispatch_a_second_coordinator() -> None:
    first_store = FakeStore()
    first_invoker = FakeInvoker()
    asyncio.run(
        _backend(first_store, first_invoker).dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-1",
        )
    )
    concurrent = first_store.created[0]
    store = FakeStore(conflict=concurrent)
    invoker = FakeInvoker()

    asyncio.run(
        _backend(store, invoker).dispatch_after_production(
            request_payload=eligible_payload(),
            production_response=_response(),
            request_id="request-2",
        )
    )

    assert len(store.created) == 1
    assert store.replaced == []
    assert invoker.calls == []


def test_unsupported_input_records_one_skip_without_dispatch() -> None:
    store = FakeStore()
    invoker = FakeInvoker()
    payload = {**eligible_payload(), "include_cliffs": True}

    asyncio.run(
        _backend(store, invoker).dispatch_after_production(
            request_payload=payload,
            production_response=_response(),
            request_id="request-1",
        )
    )

    assert store.created[0].status is ComparisonRunLifecycleStatus.SKIPPED
    assert store.created[0].error_code == "unsupported_cliff_calculation"
    assert invoker.calls == []


def test_dispatch_failure_is_sanitized_and_persisted() -> None:
    store = FakeStore()
    invoker = FakeInvoker(error=RuntimeError("contains sensitive input"))

    with pytest.raises(
        RuntimeError, match="Stage 12 comparison-run dispatch failed"
    ) as error:
        asyncio.run(
            _backend(store, invoker).dispatch_after_production(
                request_payload=eligible_payload(),
                production_response=_response(),
                request_id="request-1",
            )
        )

    assert "sensitive" not in str(error.value)

    failed = store.replaced[-1]
    assert failed.status is ComparisonRunLifecycleStatus.FAILED
    assert failed.error_code == "comparison_dispatch_failed"
    assert failed.error_summary == "RuntimeError"
    assert "sensitive" not in failed.error_summary


def test_temporary_submission_spawns_without_waiting_for_modal_result() -> None:
    store = FakeStore()
    invoker = FakeInvoker(events=store.events)
    backend = Stage12ComparisonBackend(
        make_settings(
            environment="staging",
            stage12_v2_manifest_environment="staging",
        ),
        manifest_loader=FakeLoader(),
        store=store,
        invoker=invoker,
    )

    report = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="manual-request-1",
        )
    )

    assert [event[0] for event in store.events] == [
        "create",
        "replace",
        "spawn",
        "attach",
    ]
    assert report.status is ComparisonRunLifecycleStatus.RUNNING
    assert report.production_identity == f"direct:{report.evaluation_id}"
    assert report.incumbent_execution_id is None
    assert invoker.calls[0]["context_payload"]["request_id"] == "manual-request-1"
    assert invoker.calls[0]["context_payload"]["environment"] == "staging"
    assert invoker.calls[0]["context_payload"]["modal_environment"] == "staging"
    assert invoker.calls[0]["context_payload"]["production_function_call_id"] is None
    assert not hasattr(invoker, "get")


def test_production_context_keeps_logical_and_modal_environments_distinct() -> None:
    store = FakeStore()
    invoker = FakeInvoker()
    backend = Stage12ComparisonBackend(
        make_settings(
            environment="production",
            stage12_v2_manifest_environment="main",
        ),
        manifest_loader=FakeLoader(),
        store=store,
        invoker=invoker,
    )

    report = asyncio.run(
        backend.submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="production-manual-request",
        )
    )

    assert report.environment == "production"
    context = invoker.calls[0]["context_payload"]
    assert context["environment"] == "production"
    assert context["modal_environment"] == "main"


def test_each_temporary_submission_creates_a_distinct_report() -> None:
    first_store = FakeStore()
    second_store = FakeStore()

    first = asyncio.run(
        _backend(first_store, FakeInvoker()).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
        )
    )
    second = asyncio.run(
        _backend(second_store, FakeInvoker()).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-2",
        )
    )

    assert first.evaluation_id != second.evaluation_id
    assert first.production_identity != second.production_identity


def test_temporary_submission_rejects_unsupported_input_without_persistence() -> None:
    store = FakeStore()

    with pytest.raises(TemporaryStage12UnsupportedRequest) as error:
        asyncio.run(
            _backend(store, FakeInvoker()).submit_temporary_report(
                request_payload={**eligible_payload(), "include_cliffs": True},
                request_id="request-1",
            )
        )

    assert error.value.reason == "unsupported_cliff_calculation"
    assert store.events == []


def test_temporary_status_reads_postgres_state_without_modal_access() -> None:
    store = FakeStore()
    invoker = FakeInvoker()
    report = asyncio.run(
        _backend(store, invoker).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
        )
    )
    store.events.clear()

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
    store = FakeStore()
    report = asyncio.run(
        _backend(store, FakeInvoker()).submit_temporary_report(
            request_payload=eligible_payload(),
            request_id="request-1",
        )
    )
    production_backend = Stage12ComparisonBackend(
        make_settings(environment="production"),
        manifest_loader=FakeLoader(),
        store=store,
        invoker=FakeInvoker(),
    )

    with pytest.raises(LookupError, match="does not exist"):
        production_backend._get_temporary_report(report.evaluation_id)


@pytest.mark.asyncio
async def test_temporary_submission_offloads_synchronous_dispatch(monkeypatch) -> None:
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
    )

    assert calls[0] == (
        original,
        (),
        {
            "request_payload": eligible_payload(),
            "request_id": "request-1",
        },
    )
    assert len(calls) == 3
    assert report.status is ComparisonRunLifecycleStatus.RUNNING
