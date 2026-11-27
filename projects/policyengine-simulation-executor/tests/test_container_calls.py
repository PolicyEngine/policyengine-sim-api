"""Tests for ``src.modal.container_calls``, which both Modal apps' workers use.

Each worker call's operation says how the container serving it started
(``container_start``) and which call it is in that container
(``container_call_number``). The module reads both from Modal's environment
variables, as the snapshot hook and each call see them; these tests set those
variables as Modal 1.6.4 does.
"""

from __future__ import annotations

import pytest
from conftest import NoOpObservabilityRuntime
from src.modal.container_calls import ContainerCalls, ContainerStart

# The Modal task IDs of the container that takes the snapshot, and of a
# container restored from it.
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
RESTORED_TASK_ID = "ta-01RESTORED"
CONTAINER_START_KEYS = {"container_start", "container_call_number"}


class _AttributeRecordingRuntime(NoOpObservabilityRuntime):
    """Keeps every attribute set on its operation."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_context(self, **attributes: object) -> None:
        self.attributes.update(attributes)


class _AttributeRefusingRuntime(NoOpObservabilityRuntime):
    """Fails every attempt to set an attribute, as a broken exporter might."""

    def set_context(self, **_attributes: object) -> None:
        msg = "attribute export failed"
        raise RuntimeError(msg)


@pytest.fixture(autouse=True)
def _container_without_modal_variables(monkeypatch):
    monkeypatch.delenv("MODAL_TASK_ID", raising=False)
    monkeypatch.delenv("MODAL_ENABLE_SNAP_RESTORE", raising=False)


def _calls_after_snapshot_hook(
    monkeypatch, *, snapshot_flag: str | None = "1"
) -> ContainerCalls:
    """Run the snapshot hook's record step, as Modal does in a new container.

    Modal's worker sets ``MODAL_ENABLE_SNAP_RESTORE`` to ``1`` in a container
    it takes a snapshot from, and leaves it unset in a container of the same
    class that starts without snapshots.
    """
    monkeypatch.setenv("MODAL_TASK_ID", SNAPSHOT_TASK_ID)
    if snapshot_flag is not None:
        monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", snapshot_flag)
    container_calls = ContainerCalls()
    container_calls.record_snapshot_hook()
    return container_calls


def _restore_into_another_container(monkeypatch) -> None:
    """Act as a restore: Modal 1.6.4 sets MODAL_TASK_ID to the new container's ID."""
    monkeypatch.setenv("MODAL_TASK_ID", RESTORED_TASK_ID)


def test_a_container_whose_snapshot_hook_never_ran_reports_no_snapshot(monkeypatch):
    # A plain Modal function has no snapshot hook.
    monkeypatch.setenv("MODAL_TASK_ID", SNAPSHOT_TASK_ID)

    assert ContainerCalls().container_start() is ContainerStart.NO_SNAPSHOT


@pytest.mark.parametrize("snapshot_flag", [None, "0", ""])
def test_a_container_started_without_snapshots_reports_no_snapshot(
    monkeypatch, snapshot_flag
):
    container_calls = _calls_after_snapshot_hook(
        monkeypatch, snapshot_flag=snapshot_flag
    )

    assert container_calls.container_start() is ContainerStart.NO_SNAPSHOT


def test_the_container_that_ran_the_snapshot_hook_reports_took_snapshot(
    monkeypatch,
):
    container_calls = _calls_after_snapshot_hook(monkeypatch)

    assert container_calls.container_start() is ContainerStart.TOOK_SNAPSHOT


def test_a_restored_container_reports_restored_from_snapshot(monkeypatch):
    container_calls = _calls_after_snapshot_hook(monkeypatch)
    _restore_into_another_container(monkeypatch)

    assert container_calls.container_start() is ContainerStart.RESTORED_FROM_SNAPSHOT


def test_the_attribute_values_are_the_ones_both_apps_report():
    assert {start.value for start in ContainerStart} == {
        "restored_from_snapshot",
        "took_snapshot",
        "no_snapshot",
    }


def test_each_call_takes_the_next_call_number(monkeypatch):
    container_calls = _calls_after_snapshot_hook(monkeypatch)
    _restore_into_another_container(monkeypatch)

    attributes_by_call = [container_calls.next_call_attributes() for _call in range(3)]

    assert attributes_by_call == [
        {"container_start": "restored_from_snapshot", "container_call_number": 1},
        {"container_start": "restored_from_snapshot", "container_call_number": 2},
        {"container_start": "restored_from_snapshot", "container_call_number": 3},
    ]


def test_set_call_attributes_sets_both_attributes_on_the_operation(monkeypatch):
    container_calls = _calls_after_snapshot_hook(monkeypatch)
    _restore_into_another_container(monkeypatch)
    first_call_runtime = _AttributeRecordingRuntime()
    second_call_runtime = _AttributeRecordingRuntime()

    container_calls.set_call_attributes(first_call_runtime)
    container_calls.set_call_attributes(second_call_runtime)

    assert first_call_runtime.attributes == {
        "container_start": "restored_from_snapshot",
        "container_call_number": 1,
    }
    assert second_call_runtime.attributes == {
        "container_start": "restored_from_snapshot",
        "container_call_number": 2,
    }


def test_a_runtime_refusing_the_attributes_raises_nothing_and_the_call_counts(
    monkeypatch,
):
    container_calls = _calls_after_snapshot_hook(monkeypatch)

    container_calls.set_call_attributes(_AttributeRefusingRuntime())

    next_call_runtime = _AttributeRecordingRuntime()
    container_calls.set_call_attributes(next_call_runtime)
    assert next_call_runtime.attributes["container_call_number"] == 2


def test_an_unreadable_container_start_raises_nothing_and_the_call_counts(
    monkeypatch,
):
    container_calls = _calls_after_snapshot_hook(monkeypatch)
    read_container_start = container_calls.container_start

    def fail_to_read_the_container_start() -> ContainerStart:
        msg = "container start unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        container_calls, "container_start", fail_to_read_the_container_start
    )
    failed_call_runtime = _AttributeRecordingRuntime()
    container_calls.set_call_attributes(failed_call_runtime)
    monkeypatch.setattr(container_calls, "container_start", read_container_start)
    next_call_runtime = _AttributeRecordingRuntime()
    container_calls.set_call_attributes(next_call_runtime)

    assert not CONTAINER_START_KEYS & set(failed_call_runtime.attributes)
    assert next_call_runtime.attributes == {
        "container_start": "took_snapshot",
        "container_call_number": 2,
    }


def test_separate_containers_number_their_calls_separately(monkeypatch):
    first_container_calls = _calls_after_snapshot_hook(monkeypatch)
    second_container_calls = _calls_after_snapshot_hook(monkeypatch)

    first_container_calls.next_call_attributes()

    assert second_container_calls.next_call_attributes()["container_call_number"] == 1
