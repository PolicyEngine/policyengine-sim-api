"""Tests for routing plans: applying them and restoring what they replaced."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from registry_test_support import (
    FakeDict,
    install_bundle_manifests,
    install_routing_store,
    publication_plan,
    published_routing_state,
    run_registry_main,
    versioned_app_name,
)
from src.modal.routing_publisher import state as routing_state
from src.modal.routing_publisher import stores as routing_stores
from src.modal.utils import update_version_registry as registry

# --- Rollback: routing plans and restoring latest -----------------------------
#
# Invariants:
# - applying a release's plan and then restoring it returns the routing state
#   to exactly what it was (equal digests), whatever came before: no state, a
#   redeploy of the same version, an older release that does not move latest,
#   or legacy routes without manifests;
# - a plan is applied only onto its own starting state, and restored only
#   from its own published state; anything else is a conflict that changes
#   nothing, and restoring a plan that was never applied changes nothing;
# - restoring latest to a published version points latest and that bundle's
#   model pins at it and leaves every other route pinnable.

VERSIONS = [f"6.{minor}.0" for minor in range(5)]
RELEASES = st.lists(
    st.tuples(
        st.sampled_from(VERSIONS),
        st.sampled_from(["1.1.0", "1.2.0"]),
        st.sampled_from(["2.1.0", "2.2.0"]),
        st.booleans(),
    ),
    min_size=1,
    max_size=6,
)


@settings(max_examples=80, deadline=None)
@given(releases=RELEASES, legacy=st.booleans())
def test_publish_then_restore_returns_exactly_the_previous_state(releases, legacy):
    """#744 review: first, repeat and older releases are all undone exactly."""
    monkeypatch = pytest.MonkeyPatch()
    stores: dict = {}
    try:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REPOSITORY", "PolicyEngine/policyengine-sim-api")
        monkeypatch.setenv(
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/"
            "simulation-deploy.yml@refs/heads/main",
        )

        class _Dict:
            @staticmethod
            def from_name(name, environment_name, create_if_missing):
                key = f"{environment_name}/{name}"
                if key not in stores:
                    if not create_if_missing:
                        raise KeyError(key)
                    stores[key] = FakeDict()
                return stores[key]

        monkeypatch.setattr(routing_stores, "modal", type("M", (), {"Dict": _Dict}))
        state = None
        if legacy:
            # A route seeded from the legacy dicts, with no manifest.
            state = routing_state.coerce_routing_state(None)
            state["routes"]["policyengine"]["5.9.0"] = versioned_app_name("5.9.0")
            state["routes"]["uk"]["2.1.0"] = versioned_app_name("5.9.0")
        for version, us, uk, force in releases[:-1]:
            state = publication_plan(
                monkeypatch, state, (version, us, uk), force=force
            )["after"]
        version, us, uk, force = releases[-1]
        plan = publication_plan(monkeypatch, state, (version, us, uk), force=force)
        if state is not None:
            stores["staging/simulation-api-routing-state"] = FakeDict(
                {"active": json.loads(json.dumps(state))}
            )
        before = registry.routing_state_digest(state)
        registry.apply_routing_plan(environment="staging", plan=plan)
        live = stores["staging/simulation-api-routing-state"]
        assert registry.routing_state_digest(live.get("active")) == plan["after_sha256"]
        outcome = registry.restore_routing_plan(environment="staging", plan=plan)
        assert registry.routing_state_digest(live.get("active")) == before
        assert outcome == (
            "unchanged" if plan["after_sha256"] == before else "restored"
        )
    finally:
        monkeypatch.undo()


def test_the_review_counterexample_restores_every_pin(monkeypatch, patched_modal):
    """Legacy 6.0/model1, then 6.1/model2, then a failed 6.2/model1."""
    state = routing_state.coerce_routing_state(None)
    state["routes"]["policyengine"]["6.0.0"] = versioned_app_name("6.0.0")
    state["routes"]["uk"]["2.1.0"] = versioned_app_name("6.0.0")
    state["latest"]["policyengine"] = "6.0.0"
    state = published_routing_state(monkeypatch, state, "6.1.0", "1.1.0", "2.2.0")
    store = install_routing_store(patched_modal, state)
    plan = publication_plan(monkeypatch, state, ("6.2.0", "1.1.0", "2.1.0"))
    registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active")["routes"]["uk"]["2.1.0"] == versioned_app_name("6.2.0")
    assert registry.restore_routing_plan(environment="staging", plan=plan) == "restored"
    assert store.get("active") == json.loads(json.dumps(state))
    assert store.get("active")["routes"]["uk"]["2.1.0"] == versioned_app_name("6.0.0")


def test_a_first_publication_is_undone_by_removing_the_state(
    monkeypatch, patched_modal
):
    plan = publication_plan(monkeypatch, None, ("6.2.6", "1.1.0", "2.1.0"))
    registry.apply_routing_plan(environment="staging", plan=plan)
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("active") == plan["after"]
    assert registry.restore_routing_plan(environment="staging", plan=plan) == "restored"
    assert store.get("active") is None


def test_a_plan_that_was_never_applied_restores_nothing(monkeypatch, patched_modal):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, state)
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))
    assert (
        registry.restore_routing_plan(environment="staging", plan=plan) == "unchanged"
    )
    assert store.get("active") == json.loads(json.dumps(state))


def test_a_plan_is_applied_only_onto_its_own_starting_state(monkeypatch, patched_modal):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))
    moved = published_routing_state(monkeypatch, state, "6.2.7", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, moved)
    with pytest.raises(registry.RoutingConflictError, match="nothing was published"):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == json.loads(json.dumps(moved))


def test_a_restore_never_overwrites_a_later_change(monkeypatch, patched_modal):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, state)
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=plan)
    later = published_routing_state(
        monkeypatch, store.get("active"), "6.4.0", "1.2.0", "2.2.0"
    )
    store["active"] = later
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == later


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_tampered_plan_is_refused(monkeypatch, patched_modal, side):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    install_routing_store(patched_modal, state)
    plan = publication_plan(monkeypatch, state, ("6.3.0", "1.1.0", "2.2.0"))
    plan[side] = {**(plan[side] or {}), "generation": "edited"}
    with pytest.raises(ValueError, match=f"{side} state does not match"):
        registry.apply_routing_plan(environment="staging", plan=plan)
    with pytest.raises(ValueError, match=f"{side} state does not match"):
        registry.restore_routing_plan(environment="staging", plan=plan)


@pytest.mark.parametrize(
    ("version", "force", "advances"),
    [
        ("6.3.0", False, True),
        ("6.2.5", False, False),
        ("6.2.5", True, True),
        ("6.2.6", False, True),
    ],
    ids=["newer", "older", "forced", "redeploy"],
)
def test_plans_say_whether_latest_routes_to_the_release(
    monkeypatch, version, force, advances
):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = publication_plan(
        monkeypatch, state, (version, "1.1.0", "2.1.0"), force=force
    )
    assert plan["advances_latest"] is advances


def test_restore_keeps_the_newer_release_pinnable(monkeypatch):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = published_routing_state(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    restored = registry.build_restored_routing_state(
        current_state=state, policyengine_version="6.2.6"
    )
    assert restored["latest"] == {"policyengine": "6.2.6", "us": "1.1.0", "uk": "2.1.0"}
    assert restored["routes"]["policyengine"]["6.3.0"] == versioned_app_name("6.3.0")
    assert restored["routes"]["uk"] == {
        "2.1.0": versioned_app_name("6.2.6"),
        "2.2.0": versioned_app_name("6.3.0"),
    }
    assert restored["generation"] == f"rollback:6.2.6:{versioned_app_name('6.2.6')}"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"policyengine_version": "6.1.0"}, "no published route"),
        (
            {"policyengine_version": "6.2.6", "expected_latest": "6.2.6"},
            "another release changed routing",
        ),
    ],
)
def test_unsafe_restores_are_refused(monkeypatch, kwargs, message):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = published_routing_state(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    with pytest.raises(ValueError, match=message):
        registry.build_restored_routing_state(current_state=state, **kwargs)


def test_a_restore_needs_a_manifest_that_matches_its_route(monkeypatch):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = published_routing_state(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    state["bundles"]["6.2.6"]["app_name"] = "someone-else"
    with pytest.raises(ValueError, match="manifest names app"):
        registry.build_restored_routing_state(
            current_state=state, policyengine_version="6.2.6"
        )


@pytest.mark.usefixtures("serialized_workflow")
def test_main_plans_applies_and_restores(patched_modal, monkeypatch, capsys, tmp_path):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, state)
    install_bundle_manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    plan_file = tmp_path / "plan.json"
    run_registry_main(
        monkeypatch,
        "--app-name",
        versioned_app_name("6.3.0"),
        "--policyengine-version",
        "6.3.0",
        "--us-version",
        "1.1.0",
        "--uk-version",
        "2.2.0",
        "--plan-out",
        str(plan_file),
    )
    plan = json.loads(plan_file.read_text())
    assert capsys.readouterr().out.splitlines() == [
        f"before_sha256={plan['before_sha256']}",
        f"after_sha256={plan['after_sha256']}",
        "advances_latest=true",
    ]
    assert store.get("active") == json.loads(
        json.dumps(state)
    )  # planning changes nothing
    run_registry_main(monkeypatch, "--apply-plan", str(plan_file))
    assert store.get("active")["latest"]["policyengine"] == "6.3.0"
    run_registry_main(monkeypatch, "--print-identity")
    assert capsys.readouterr().out.splitlines()[-4:] == [
        "policyengine_version=6.3.0",
        f"app_name={versioned_app_name('6.3.0')}",
        "uk_model_version=2.2.0",
        "uk_dataset_sha256=" + "5" * 64,
    ]
    run_registry_main(monkeypatch, "--restore-plan", str(plan_file))
    assert "restored" in capsys.readouterr().out
    assert store.get("active") == json.loads(json.dumps(state))


@pytest.mark.usefixtures("serialized_workflow")
def test_main_prints_and_restores_latest(patched_modal, monkeypatch, capsys):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = published_routing_state(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    store = install_routing_store(patched_modal, state)
    run_registry_main(monkeypatch, "--print-latest")
    assert capsys.readouterr().out.strip() == "6.3.0"
    run_registry_main(
        monkeypatch, "--restore-latest", "6.2.6", "--expected-latest", "6.3.0"
    )
    active = store.get("active")
    assert active["latest"]["policyengine"] == "6.2.6"
    assert active["routes"]["policyengine"]["6.3.0"] == versioned_app_name("6.3.0")
    with pytest.raises(SystemExit):
        run_registry_main(monkeypatch, "--expected-latest", "6.2.6")


def test_a_failed_retry_attempt_cannot_hide_the_publication(monkeypatch, patched_modal):
    """#744 review: attempt 1 published A to B; attempt 2 republished B."""
    a = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, a)
    first = publication_plan(monkeypatch, a, ("6.1.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=first)
    second = publication_plan(monkeypatch, first["after"], ("6.1.0", "1.1.0", "2.2.0"))
    # Each publication is its own state, even of the same release.
    assert second["before_sha256"] != second["after_sha256"]
    registry.apply_routing_plan(environment="staging", plan=second)
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "restored"
    )
    assert store.get("active") == json.loads(json.dumps(a))


def test_an_old_plan_cannot_withdraw_a_healthy_republication(
    monkeypatch, patched_modal
):
    """#744 review: A to B failed and was restored; a healthy run republished B."""
    a = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, a)
    failed = publication_plan(monkeypatch, a, ("6.1.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=failed)
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed) == "restored"
    )
    healthy = publication_plan(monkeypatch, a, ("6.1.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=healthy)
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=failed)
    assert store.get("active") == healthy["after"]


def test_a_chain_of_attempts_is_undone_in_order(monkeypatch, patched_modal):
    a = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, a)
    first = publication_plan(monkeypatch, a, ("6.1.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=first)
    second = publication_plan(monkeypatch, first["after"], ("6.1.1", "1.2.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=second)
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "restored"
    )
    assert store.get("active") == json.loads(json.dumps(a))
    # Done again, nothing is left to undo.
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "unchanged"
    )


@pytest.mark.usefixtures("serialized_workflow")
def test_main_restores_several_plans(patched_modal, monkeypatch, capsys, tmp_path):
    a = published_routing_state(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, a)
    first = publication_plan(monkeypatch, a, ("6.1.0", "1.1.0", "2.2.0"))
    registry.apply_routing_plan(environment="staging", plan=first)
    second = publication_plan(monkeypatch, first["after"], ("6.1.0", "1.1.0", "2.2.0"))
    files = []
    for index, plan in enumerate((first, second), start=1):
        path = tmp_path / f"plan-{index}.json"
        path.write_text(json.dumps(plan))
        files.append(str(path))
    run_registry_main(monkeypatch, "--restore-plan", *files)
    assert "restored" in capsys.readouterr().out
    assert store.get("active") == json.loads(json.dumps(a))


def test_plans_carry_their_publication_id(monkeypatch):
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    install_bundle_manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    plan = registry.plan_routing_publication(
        current_state=state,
        deployed=registry.DeployedAppVersions(
            app_name=versioned_app_name("6.3.0"),
            policyengine_version="6.3.0",
            us_version="1.1.0",
            uk_version="2.2.0",
        ),
        publication_id="run-7-attempt-2-prod",
    )
    assert plan["publication_id"] == "run-7-attempt-2-prod"
    assert (
        plan["after"]["generation"]
        == f"6.3.0:{versioned_app_name('6.3.0')}:run-7-attempt-2-prod"
    )
    registry.validate_routing_plan(plan)
