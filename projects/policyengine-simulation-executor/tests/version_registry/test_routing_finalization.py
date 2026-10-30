"""Tests for finalizing a release's routing publication from its journal."""

from __future__ import annotations

import json

import pytest
from registry_test_support import (
    FakeDict,
    install_routing_store,
    publication_plan,
    published_routing_state,
)
from src.modal.routing_publisher import publication as routing_publication
from src.modal.utils import update_version_registry as registry


@pytest.mark.parametrize(
    "release_case",
    [
        ("failed_release", True, False, "restored"),
        ("successful_release", True, True, "completed"),
        ("upload_failure_before_apply", False, False, "restored"),
        ("failed_jobs_rerun", True, True, "completed"),
        ("failed_jobs_rerun_gate_failure", True, False, "restored"),
        ("failure_inside_deployment_ready", True, True, "completed"),
        ("passing_gates_but_apply_never_ran", False, True, "restored"),
    ],
    ids=lambda release_case: "-".join(str(part) for part in release_case),
)
@pytest.mark.parametrize("first_publication", [False, True])
def test_finalization_finishes_each_release_and_allows_the_next_one(
    monkeypatch, patched_modal, release_case, first_publication
):
    """Upload loss and attempt 2 use the full journal, with no artifact reads.

    ``release_case`` is the scenario, whether its plan was applied, whether
    its gates passed and the finalization outcome.
    """
    scenario, applied, gates_passed, expected = release_case
    before = (
        None
        if first_publication
        else published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    )
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(
        monkeypatch,
        before,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    if applied:
        registry.apply_routing_plan(environment="staging", plan=pending)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2" if "rerun" in scenario else "1")
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == expected
    )
    expected_live = pending["after"] if expected == "completed" else before
    assert store.get("active") == expected_live
    assert store.get("publication") == {"status": expected, "plan": pending}
    # deployment_ready is a reporter after finalization. Losing that reporter
    # cannot strand the journal or block the next run.
    if scenario == "failure_inside_deployment_ready":
        assert store.get("publication")["status"] == "completed"
    later = publication_plan(
        monkeypatch,
        expected_live,
        ("6.2.0", "1.2.0", "2.3.0"),
        publication_id="74402-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=later)
    registry.apply_routing_plan(environment="staging", plan=later)
    assert store.get("active") == later["after"]


def test_lost_finalizer_is_recovered_by_operator_without_an_artifact(
    monkeypatch, patched_modal
):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(
        monkeypatch,
        before,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    later = publication_plan(monkeypatch, pending["after"], ("6.2.0", "1.2.0", "2.3.0"))
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=later)
    assert store.get("publication") == {"status": "pending", "plan": pending}
    assert (
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=False
        )
        == "restored"
    )
    assert store.get("active") == before
    assert store.get("publication") == {"status": "restored", "plan": pending}
    recovered_later = publication_plan(monkeypatch, before, ("6.2.0", "1.2.0", "2.3.0"))
    registry.record_routing_plan(environment="staging", plan=recovered_later)
    registry.apply_routing_plan(environment="staging", plan=recovered_later)
    assert store.get("active") == recovered_later["after"]


@pytest.mark.parametrize("gates_passed", [False, True])
@pytest.mark.parametrize(
    "publication_id",
    [
        "74400-1-beta",
        "744010-1-beta",
        "74401-1-prod",
        "74401-1-beta-other",
        "74401-invalid-beta",
        "invalid-1-beta",
    ],
)
def test_finalizer_leaves_a_foreign_pending_publication_untouched(
    monkeypatch, patched_modal, gates_passed, publication_id
):
    store = install_routing_store(patched_modal, None)
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id=publication_id,
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == "foreign-publication"
    )
    assert store.snapshot() == saved


@pytest.mark.parametrize(
    ("publication_run", "gates_passed"),
    [("74401", False), ("74401", True), (None, False)],
)
def test_finalizer_conflict_is_loud_and_keeps_the_journal_pending(
    monkeypatch, patched_modal, publication_run, gates_passed
):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(
        monkeypatch,
        before,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    store["active"] = published_routing_state(
        monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0"
    )
    saved = json.loads(json.dumps(store.snapshot()))
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run=publication_run,
            gates_passed=gates_passed,
        )
    assert store.snapshot() == saved


@pytest.mark.parametrize("gates_passed", [False, True])
@pytest.mark.parametrize("journal", ["missing_store", "absent", "foreign_completed"])
def test_finalizer_without_its_pending_record_changes_nothing(
    monkeypatch, patched_modal, gates_passed, journal
):
    if journal != "missing_store":
        before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
        store = install_routing_store(patched_modal, before)
        if journal == "foreign_completed":
            plan = publication_plan(
                monkeypatch,
                before,
                ("6.1.0", "1.1.0", "2.2.0"),
                publication_id="74400-1-beta",
            )
            registry.record_routing_plan(environment="staging", plan=plan)
            registry.apply_routing_plan(environment="staging", plan=plan)
            registry.complete_routing_plan(environment="staging", plan=plan)
        saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == "no-pending"
    )
    if journal == "missing_store":
        assert patched_modal == {}
    else:
        assert store.snapshot() == saved


@pytest.mark.parametrize("gates_passed", [False, True])
def test_retry_after_finalization_returns_the_prior_outcome_without_writing(
    monkeypatch, patched_modal, gates_passed
):
    store = install_routing_store(patched_modal, None)
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    args = {
        "environment": "staging",
        "release_environment": "beta",
        "publication_run": "74401",
        "gates_passed": gates_passed,
    }
    expected = "completed" if gates_passed else "restored"
    assert registry.finalize_routing_publication(**args) == expected
    saved = json.loads(json.dumps(store.snapshot()))

    def forbidden_write(*_args, **_kwargs):
        pytest.fail("an already finalized publication was written again")

    monkeypatch.setattr(
        routing_publication, "write_publication_record", forbidden_write
    )
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert registry.finalize_routing_publication(**args) == expected
    assert store.snapshot() == saved


def test_operator_cannot_complete_pending_publications(patched_modal):
    store = install_routing_store(patched_modal, None)
    with pytest.raises(
        ValueError, match="operator recovery requires gates_passed=false"
    ):
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=True
        )
    assert store.snapshot() == {}


def test_completed_finalizer_retry_cannot_report_a_release_that_is_no_longer_live(
    monkeypatch, patched_modal
):
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    registry.complete_routing_plan(environment="staging", plan=pending)
    store = patched_modal["staging/simulation-api-routing-state"]
    store["active"] = published_routing_state(
        monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0"
    )
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=True,
        )
        == "no-pending"
    )
    assert store.snapshot() == saved


def test_finalizer_owns_prod_publications_in_the_main_modal_environment(
    monkeypatch, patched_modal
):
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-prod",
    )
    registry.record_routing_plan(environment="main", plan=pending)
    registry.apply_routing_plan(environment="main", plan=pending)
    assert (
        registry.finalize_routing_publication(
            environment="main",
            release_environment="prod",
            publication_run="74401",
            gates_passed=True,
        )
        == "completed"
    )
    store = patched_modal["main/simulation-api-routing-state"]
    assert store.get("publication") == {"status": "completed", "plan": pending}


def test_finalizer_rejects_invalid_release_environments(patched_modal):
    with pytest.raises(ValueError, match="release_environment must be beta or prod"):
        registry.finalize_routing_publication(
            environment="staging", release_environment="staging", gates_passed=False
        )
    assert patched_modal == {}


@pytest.mark.parametrize(
    "publication_id",
    [
        "invalid-1-beta",
        "74401-invalid-beta",
        "74401-1-prod",
        "74401-1-beta-extra",
        "b88e8c1b01384bc88885657dcf1b8254",
    ],
)
def test_operator_restores_any_valid_pending_plan_in_the_selected_store(
    monkeypatch, patched_modal, publication_id
):
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id=publication_id,
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    store = patched_modal["staging/simulation-api-routing-state"]
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=False
        )
        == "restored"
    )
    assert saved["publication"]["status"] == "pending"
    assert store.get("active") is None
    assert store.get("publication") == {"status": "restored", "plan": pending}


@pytest.mark.parametrize("status", ["pending", "completed", "restored"])
def test_publication_records_are_read_back(monkeypatch, patched_modal, status):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))

    class DroppedJournalWrite(FakeDict):
        def __setitem__(self, key, value):
            if key != "publication":
                super().__setitem__(key, value)

    initial = {"active": before if status == "pending" else plan["after"]}
    if status != "pending":
        initial["publication"] = {"status": "pending", "plan": plan}
    store = DroppedJournalWrite(initial)
    patched_modal["staging/simulation-api-routing-state"] = store
    operation = {
        "pending": registry.record_routing_plan,
        "completed": registry.complete_routing_plan,
        "restored": registry.restore_routing_plan,
    }[status]
    with pytest.raises(
        registry.RoutingConflictError, match="publication record reads back differently"
    ):
        operation(environment="staging", plan=plan)
    assert store.get("active") == (plan["after"] if status == "completed" else before)
    assert store.get("publication") == initial.get("publication")


def test_recording_checks_the_live_starting_state(monkeypatch, patched_modal):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    later = published_routing_state(monkeypatch, before, "6.2.0", "1.2.0", "2.3.0")
    store = install_routing_store(patched_modal, later)
    with pytest.raises(registry.RoutingConflictError, match="changed before recording"):
        registry.record_routing_plan(environment="staging", plan=plan)
    assert store.snapshot() == {"active": later}


@pytest.mark.parametrize(
    "record", ["garbled", {"status": "unknown"}, {"status": "completed", "plan": {}}]
)
def test_malformed_publication_record_refuses_writes(
    monkeypatch, patched_modal, record
):
    store = install_routing_store(patched_modal, None)
    store["publication"] = record
    plan = publication_plan(monkeypatch, None, ("6.1.0", "1.1.0", "2.2.0"))
    with pytest.raises((registry.RoutingConflictError, ValueError)):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.snapshot() == {"publication": record}


@pytest.mark.parametrize(
    ("publication_id", "publication_run", "owned"),
    [
        ("74401-1-beta", "74401", True),
        ("74401-2-beta", "74401", True),
        ("74401-1-prod", "74401", False),
        ("74400-1-beta", "74401", False),
        ("74401-x-beta", "74401", False),
        ("74401-1-beta-extra", "74401", False),
        ("74401-beta", "74401", False),
        ("anything", None, True),
    ],
)
def test_publication_ownership_reads_run_attempt_and_environment(
    publication_id, publication_run, owned
):
    assert (
        routing_publication._publication_owned_by_release(
            {"publication_id": publication_id},
            publication_run=publication_run,
            release_environment="beta",
        )
        is owned
    )
