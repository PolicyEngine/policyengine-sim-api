"""Tests for recording, applying and restoring routing plans in Modal."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from registry_test_support import (
    FakeDict,
    install_routing_store,
    publication_plan,
    published_routing_state,
    versioned_app_name,
)
from src.modal.routing_publisher import stores as routing_stores
from src.modal.utils import update_version_registry as registry


def test_applying_a_plan_verifies_the_written_state(monkeypatch, patched_modal):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))

    class DroppedWrite(FakeDict):
        def __setitem__(self, key, value):
            pass

    store = DroppedWrite({"active": state})
    patched_modal["staging/simulation-api-routing-state"] = store
    with pytest.raises(
        registry.RoutingConflictError,
        match="published routing state reads back differently",
    ):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == state


@pytest.mark.parametrize("first_publication", [False, True])
def test_restoring_a_plan_verifies_the_written_or_removed_state(
    monkeypatch, patched_modal, first_publication
):
    state = (
        None
        if first_publication
        else published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    )
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))

    class DroppedRestore(FakeDict):
        def __setitem__(self, key, value):
            pass

        def pop(self, key):
            return self._data[key]

    store = DroppedRestore({"active": plan["after"]})
    patched_modal["staging/simulation-api-routing-state"] = store
    with pytest.raises(
        registry.RoutingConflictError,
        match="restored routing state reads back differently",
    ):
        registry.restore_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == plan["after"]


class _UnopenableDict:
    """A Modal Dict lookup that finds nothing, even when asked to create the Dict."""

    @staticmethod
    def from_name(name: str, **lookup_options: object):
        missing_dict = f"{lookup_options['environment_name']}/{name}"
        raise KeyError(missing_dict)


def test_routing_writes_refuse_a_routing_store_that_cannot_be_opened(
    monkeypatch, serialized_workflow
):
    """Recording or applying a plan stops with a RuntimeError, not an AttributeError."""
    monkeypatch.setattr(routing_stores, "modal", SimpleNamespace(Dict=_UnopenableDict))
    original = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, original, ("6.1.0", "1.1.0", "2.2.0"))

    for operation in (registry.record_routing_plan, registry.apply_routing_plan):
        with pytest.raises(
            RuntimeError, match="the routing state Modal Dict could not be opened"
        ) as raised:
            operation(environment="staging", plan=plan)
        # Not a RoutingConflictError: no plan disagrees with the live state.
        assert type(raised.value) is RuntimeError


class NotFoundError(Exception):
    """Named like modal.exception.NotFoundError, which the utility matches by name."""


class _LazyMissingDict:
    """A missing Dict as modal 1.x reports it.

    Dict.from_name returns a handle without contacting Modal; looking the Dict
    up, or the first read, raises NotFoundError.
    """

    @staticmethod
    def from_name(name: str, **lookup_options: object):
        del lookup_options
        return _LazyMissingDict.Handle(name)

    class Handle:
        def __init__(self, name: str):
            self.name = name

        def hydrate(self):
            raise NotFoundError(self.name)

        def get(self, key, default=None):
            raise NotFoundError(self.name)


@pytest.mark.parametrize(
    "missing_dict", [_UnopenableDict, _LazyMissingDict], ids=["eager", "lazy"]
)
def test_completing_a_plan_refuses_a_missing_routing_store(
    monkeypatch, serialized_workflow, missing_dict
):
    monkeypatch.setattr(routing_stores, "modal", SimpleNamespace(Dict=missing_dict))
    original = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, original, ("6.1.0", "1.1.0", "2.2.0"))

    with pytest.raises(RuntimeError, match=registry.UNOPENED_ROUTING_STORE) as raised:
        registry.complete_routing_plan(environment="staging", plan=plan)
    assert type(raised.value) is RuntimeError


def test_restoring_with_a_lazily_missing_routing_store(
    monkeypatch, serialized_workflow
):
    """With modal 1.x a missing Dict is reported at lookup; restore sees no store."""
    monkeypatch.setattr(routing_stores, "modal", SimpleNamespace(Dict=_LazyMissingDict))
    first = publication_plan(monkeypatch, None, ("6.1.0", "1.1.0", "2.2.0"))
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first])
        == "unchanged"
    )
    later = publication_plan(monkeypatch, first["after"], ("6.2.0", "1.1.0", "2.3.0"))
    with pytest.raises(
        registry.RoutingConflictError,
        match=r"^there is no routing state to restore into$",
    ):
        registry.restore_routing_plans(environment="staging", plans=[first, later])


def test_runner_loss_cannot_be_adopted_by_the_next_release(monkeypatch, patched_modal):
    """Failed A cannot become successful B's starting state or rollback target."""
    original = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, original)
    failed_a = publication_plan(monkeypatch, original, ("6.1.0", "1.1.0", "2.2.0"))
    registry.record_routing_plan(environment="staging", plan=failed_a)
    registry.apply_routing_plan(environment="staging", plan=failed_a)
    # Runner disappears here, before outputs or a release completion record.
    attempted_b = publication_plan(
        monkeypatch, store.get("active"), ("6.2.0", "1.2.0", "2.3.0")
    )
    for operation in (registry.record_routing_plan, registry.apply_routing_plan):
        with pytest.raises(
            registry.RoutingConflictError, match="Unfinished routing publication"
        ):
            operation(environment="staging", plan=attempted_b)
    assert store.get("active") == failed_a["after"]
    assert store.get("publication") == {"status": "pending", "plan": failed_a}

    # Explicit recovery withdraws A before a later release can publish.
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed_a)
        == "restored"
    )
    assert store.get("active") == original
    assert store.get("publication") == {"status": "restored", "plan": failed_a}
    healthy_b = publication_plan(monkeypatch, original, ("6.2.0", "1.2.0", "2.3.0"))
    registry.record_routing_plan(environment="staging", plan=healthy_b)
    registry.apply_routing_plan(environment="staging", plan=healthy_b)
    registry.complete_routing_plan(environment="staging", plan=healthy_b)
    assert "6.1.0" not in store.get("active")["routes"]["policyengine"]
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=failed_a)
    # A subsequent failed release restores healthy B and cannot resurrect A.
    failed_c = publication_plan(
        monkeypatch, healthy_b["after"], ("6.3.0", "1.3.0", "2.4.0")
    )
    registry.record_routing_plan(environment="staging", plan=failed_c)
    registry.apply_routing_plan(environment="staging", plan=failed_c)
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed_c)
        == "restored"
    )
    assert store.get("active") == healthy_b["after"]
    assert "6.1.0" not in store.get("active")["routes"]["policyengine"]


def test_a_failed_same_version_redeploy_is_resolved_by_its_restore(
    monkeypatch, patched_modal
):
    """A redeploy that fails a gate must not block later releases.

    A plan's published state carries its own publication id, so even a
    same-version redeploy differs from what it replaces and has a restore.
    """
    live = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, live)
    redeploy = publication_plan(monkeypatch, live, ("6.0.0", "1.1.0", "2.1.0"))
    assert redeploy["before_sha256"] != redeploy["after_sha256"]
    changed = {
        key
        for key in redeploy["after"]
        if redeploy["after"][key] != redeploy["before"][key]
    }
    assert changed == {"generation"}
    registry.record_routing_plan(environment="staging", plan=redeploy)
    registry.apply_routing_plan(environment="staging", plan=redeploy)
    # A release gate fails here. Until the restore runs, nothing else may write.
    later = publication_plan(monkeypatch, live, ("6.1.0", "1.1.0", "2.2.0"))
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=later)
    assert (
        registry.restore_routing_plan(environment="staging", plan=redeploy)
        == "restored"
    )
    assert store.get("active") == live
    assert store.get("publication") == {"status": "restored", "plan": redeploy}
    registry.record_routing_plan(environment="staging", plan=later)


@pytest.mark.parametrize("first_publication", [False, True])
def test_recorded_but_unpublished_plan_blocks_writes_until_restored(
    monkeypatch, patched_modal, first_publication
):
    before = (
        None
        if first_publication
        else published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    )
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    registry.record_routing_plan(environment="staging", plan=pending)
    # Covers loss after recording, before upload or before apply.
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=pending)
    assert (
        registry.restore_routing_plan(environment="staging", plan=pending)
        == "unchanged"
    )
    assert store.get("active") == before
    assert store.get("publication")["status"] == "restored"
    registry.record_routing_plan(environment="staging", plan=pending)


@pytest.mark.parametrize("writer", ["publish", "latest", "seed", "restore"])
def test_every_other_routing_write_refuses_an_unfinished_release(
    monkeypatch, patched_modal, writer
):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    saved = json.loads(json.dumps(store.snapshot()))
    other = publication_plan(monkeypatch, pending["after"], ("6.2.0", "1.2.0", "2.3.0"))

    def write_routing():
        if writer == "publish":
            registry.publish_routing_state(
                environment="staging",
                deployed=registry.DeployedAppVersions(
                    app_name=versioned_app_name("6.2.0"),
                    policyengine_version="6.2.0",
                    us_version="1.2.0",
                    uk_version="2.3.0",
                ),
            )
        elif writer == "latest":
            registry.restore_routing_state(
                environment="staging", policyengine_version="6.0.0"
            )
        elif writer == "seed":
            registry.seed_active_routing_state_from_legacy(environment="staging")
        else:
            registry.restore_routing_plans(environment="staging", plans=[other])

    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        write_routing()
    assert store.snapshot() == saved


def test_only_matching_live_publication_can_be_completed(monkeypatch, patched_modal):
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    with pytest.raises(registry.RoutingConflictError, match="no matching unfinished"):
        registry.complete_routing_plan(environment="staging", plan=pending)
    registry.record_routing_plan(environment="staging", plan=pending)
    with pytest.raises(
        registry.RoutingConflictError, match="routing state is not live"
    ):
        registry.complete_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    other = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    with pytest.raises(registry.RoutingConflictError, match="no matching unfinished"):
        registry.complete_routing_plan(environment="staging", plan=other)
    assert store.get("publication")["status"] == "pending"
    registry.complete_routing_plan(environment="staging", plan=pending)
    assert store.get("publication") == {"status": "completed", "plan": pending}


@pytest.mark.parametrize("operation", ["apply", "restore", "complete"])
def test_publication_ownership_compares_the_entire_plan(
    monkeypatch, patched_modal, operation
):
    """A reused publication id never grants permission to a changed plan."""
    before = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, before)
    pending = publication_plan(monkeypatch, before, ("6.1.0", "1.1.0", "2.2.0"))
    registry.record_routing_plan(environment="staging", plan=pending)
    if operation != "apply":
        registry.apply_routing_plan(environment="staging", plan=pending)
    changed = json.loads(json.dumps(pending))
    changed["advances_latest"] = not pending["advances_latest"]
    registry.validate_routing_plan(changed)
    assert changed["publication_id"] == pending["publication_id"]
    saved = json.loads(json.dumps(store.snapshot()))
    writer = {
        "apply": registry.apply_routing_plan,
        "restore": registry.restore_routing_plan,
        "complete": registry.complete_routing_plan,
    }[operation]
    with pytest.raises(registry.RoutingConflictError):
        writer(environment="staging", plan=changed)
    assert store.snapshot() == saved


@pytest.mark.usefixtures("patched_modal")
def test_restoring_no_plans_is_refused():
    with pytest.raises(ValueError, match=r"^no routing plan to restore$"):
        registry.restore_routing_plans(environment="staging", plans=[])


def test_restoring_without_a_routing_store(monkeypatch, patched_modal):
    first = publication_plan(monkeypatch, None, ("6.1.0", "1.1.0", "2.2.0"))
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first])
        == "unchanged"
    )
    later = publication_plan(monkeypatch, first["after"], ("6.2.0", "1.1.0", "2.3.0"))
    with pytest.raises(
        registry.RoutingConflictError,
        match=r"^there is no routing state to restore into$",
    ):
        registry.restore_routing_plans(environment="staging", plans=[first, later])
    assert patched_modal == {}
