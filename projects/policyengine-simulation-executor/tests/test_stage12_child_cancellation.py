"""Tests for cancelling a failed Stage 12 report's unfinished child simulations."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from stage12_child_call_support import (
    AttachFailingStore,
    CancellationStateFailingStore,
    PlaceholderWriteFailingStore,
    ScriptedChildInvoker,
    assert_cancelled,
    assert_log_identifies_child,
    assert_original_coordinator_failure,
    coordinate_with,
    coordination_log_record,
    report_with_segmentation,
    role_simulation_id,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import BASELINE_ID, NOW, REFORM_ID, child_record


def test_baseline_failure_cancels_and_records_the_reform_child() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline worker failed")}
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_with_segmentation(segmented=False),
            store,
            invoker,
            artifacts=artifacts,
        )

    reform_call = invoker.calls["reform"]
    assert reform_call.cancel_count == 1
    assert reform_call.probed_at == []
    assert artifacts.aggregate_writes == []
    assert store.children[BASELINE_ID].error_code == "simulation_invocation_failed"
    assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert store.parent.error_code == "simulation_invocation_failed"


def test_reform_failure_leaves_the_finished_baseline_alone() -> None:
    report = report_with_segmentation(segmented=False)
    store = FakeStore()
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    invoker = ScriptedChildInvoker(
        results={"baseline": baseline_descriptor.model_dump(mode="json")},
        wait_errors={"reform": LookupError("reform worker failed")},
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(report, store, invoker, artifacts=artifacts)

    assert invoker.calls["baseline"].cancel_count == 0
    assert store.children[BASELINE_ID].status is ComparisonRunLifecycleStatus.RUNNING
    assert store.children[BASELINE_ID].error_code is None
    assert store.children[REFORM_ID].error_code == "simulation_invocation_failed"


def test_baseline_failure_cancels_a_reform_call_restored_on_retry() -> None:
    report = report_with_segmentation(segmented=False)
    store = FakeStore()
    # An earlier coordinator attempt started the reform child before stopping.
    store.children[REFORM_ID] = child_record(report.reform).model_copy(
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
        coordinate_with(report, store, invoker)

    assert invoker.events == ["spawn:baseline", "restore:reform"]
    assert invoker.calls["reform"].cancel_count == 1
    assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
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
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

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
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert invoker.calls["reform"].cancel_count == 1
    assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert store.parent.error_code == "simulation_invocation_failed"
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.WARNING),
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
        coordinate_with(report_with_segmentation(segmented=segmented), store, invoker)

    # A segmented worker cannot read a cancellation that was never written, so
    # it is cancelled through Modal as well.
    assert invoker.calls["reform"].cancel_count == 1
    assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.RUNNING
    assert store.parent.error_code == "simulation_invocation_failed"
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.ERROR),
        role="reform",
        error_type="ConnectionError",
    )


def test_wait_timeout_cancels_and_records_every_unfinished_child() -> None:
    store = FakeStore()
    invoker = ScriptedChildInvoker()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_with_segmentation(segmented=False),
            store,
            invoker,
            wait_timeout_seconds=0,
        )

    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    for role in ("baseline", "reform"):
        assert_cancelled(
            store.children[role_simulation_id(role)],
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
        coordinate_with(
            report_with_segmentation(segmented=False),
            store,
            invoker,
            wait_timeout_seconds=0,
        )

    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    assert store.parent.error_code == "simulation_wait_timeout"
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.WARNING),
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
        coordinate_with(report_with_segmentation(segmented=segmented), store, invoker)

    assert_original_coordinator_failure(error, store)
    assert invoker.events == ["spawn:baseline"]
    assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
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
    store = AttachFailingStore(role_simulation_id(failing_role))
    invoker = ScriptedChildInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert_original_coordinator_failure(error, store)
    assert invoker.events == [f"spawn:{role}" for role in started_roles]
    # The child whose invocation could not be attached is stopped as well.
    for role in started_roles:
        assert_cancelled(
            store.children[role_simulation_id(role)], after="the coordinator failed"
        )
        assert invoker.calls[role].cancel_count == 1
    if failing_role == "baseline":
        assert store.children[REFORM_ID].status is ComparisonRunLifecycleStatus.PENDING


def test_startup_failure_stops_a_call_restored_from_an_earlier_attempt() -> None:
    report = report_with_segmentation(segmented=False)
    store = PlaceholderWriteFailingStore(REFORM_ID)
    store.children[BASELINE_ID] = child_record(report.baseline).model_copy(
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
        coordinate_with(report, store, invoker)

    assert_original_coordinator_failure(error, store)
    assert invoker.events == ["restore:baseline"]
    assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
    assert invoker.calls["baseline"].cancel_count == 1


def test_startup_failure_leaves_a_finished_child_unchanged() -> None:
    report = report_with_segmentation(segmented=False)
    store = AttachFailingStore(REFORM_ID)
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    finished_baseline = child_record(report.baseline).model_copy(
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
        coordinate_with(report, store, invoker, artifacts=artifacts)

    assert_original_coordinator_failure(error, store)
    assert invoker.events == ["spawn:reform"]
    assert store.children[BASELINE_ID] == finished_baseline
    assert_cancelled(store.children[REFORM_ID], after="the coordinator failed")
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
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert_original_coordinator_failure(error, store)
    assert invoker.calls["baseline"].cancel_count == 1
    assert_cancelled(store.children[BASELINE_ID], after="the coordinator failed")
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.WARNING),
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
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert_original_coordinator_failure(error, store)
    assert invoker.calls["baseline"].cancel_count == 1
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.ERROR),
        role="baseline",
        error_type="ConnectionError",
    )
