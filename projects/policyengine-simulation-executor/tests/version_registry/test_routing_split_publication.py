"""Tests for publishing a release's routes before its checks and latest after them.

A deployment adds the release's routes (its policyengine.py version and its
US and UK model versions, mapped to the deployed app) and bundle manifest
before its checks, leaving ``latest`` alone. A later job moves ``latest``
once every check has passed.
"""

from __future__ import annotations

import json

import pytest
from registry_test_support import (
    install_bundle_manifests,
    install_routing_store,
    published_routing_state,
    run_registry_main,
    versioned_app_name,
)
from src.modal.utils import update_version_registry as registry

OLD = ("6.0.0", "1.1.0", "2.1.0")
NEW = ("6.1.0", "1.2.0", "2.2.0")


def _deployed(versions) -> registry.DeployedAppVersions:
    version, us_version, uk_version = versions
    return registry.DeployedAppVersions(
        app_name=versioned_app_name(version),
        policyengine_version=version,
        us_version=us_version,
        uk_version=uk_version,
    )


def _old_release_state(monkeypatch):
    return published_routing_state(monkeypatch, None, *OLD)


def _install(monkeypatch, *releases):
    install_bundle_manifests(
        monkeypatch, {version: (us, uk) for version, us, uk in releases}
    )


# --- The routing state: routes first, latest later ---------------------------


def test_routes_state_adds_the_releases_routes_and_leaves_latest(monkeypatch):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)

    state = registry.build_routes_routing_state(
        current_state=before, deployed=_deployed(NEW), now="2026-11-06T12:00:00Z"
    )

    new_app = versioned_app_name(NEW[0])
    assert state["routes"]["policyengine"][NEW[0]] == new_app
    assert state["routes"]["us"][NEW[1]] == new_app
    assert state["routes"]["uk"][NEW[2]] == new_app
    assert state["bundles"][NEW[0]]["app_name"] == new_app
    assert state["published_at"][NEW[0]] == "2026-11-06T12:00:00Z"
    assert state["latest"] == before["latest"]
    assert state.get("latest_since") == before.get("latest_since")
    # The older release stays routed and pinnable.
    assert state["routes"]["policyengine"][OLD[0]] == versioned_app_name(OLD[0])


def test_routes_state_routes_unpinned_requests_to_the_first_release(monkeypatch):
    # With no routing yet there is no earlier release for latest to keep.
    _install(monkeypatch, NEW)

    state = registry.build_routes_routing_state(
        current_state=None, deployed=_deployed(NEW), now="2026-11-06T12:00:00Z"
    )

    assert state["latest"] == {"policyengine": NEW[0], "us": NEW[1], "uk": NEW[2]}
    assert state["latest_since"] == "2026-11-06T12:00:00Z"


def test_latest_state_moves_latest_to_a_newer_routed_release(monkeypatch):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    routed = registry.build_routes_routing_state(
        current_state=before, deployed=_deployed(NEW)
    )

    state = registry.build_latest_routing_state(
        current_state=routed, deployed=_deployed(NEW), now="2026-11-06T13:00:00Z"
    )

    assert state["latest"] == {"policyengine": NEW[0], "us": NEW[1], "uk": NEW[2]}
    assert state["latest_since"] == "2026-11-06T13:00:00Z"
    assert state["routes"] == routed["routes"]
    assert state["bundles"] == routed["bundles"]


def test_latest_state_keeps_a_newer_latest_unless_forced(monkeypatch):
    newer = published_routing_state(monkeypatch, None, *NEW)
    _install(monkeypatch, OLD, NEW)
    routed = registry.build_routes_routing_state(
        current_state=newer, deployed=_deployed(OLD)
    )

    kept = registry.build_latest_routing_state(
        current_state=routed, deployed=_deployed(OLD)
    )
    forced = registry.build_latest_routing_state(
        current_state=routed, deployed=_deployed(OLD), force_latest=True
    )

    assert kept["latest"]["policyengine"] == NEW[0]
    assert forced["latest"] == {"policyengine": OLD[0], "us": OLD[1], "uk": OLD[2]}


@pytest.mark.parametrize("unrouted", ["policyengine", "us", "uk", "bundle"])
def test_latest_state_refuses_a_release_whose_routes_are_not_published(
    monkeypatch, unrouted
):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    routed = registry.build_routes_routing_state(
        current_state=before, deployed=_deployed(NEW)
    )
    if unrouted == "bundle":
        routed["bundles"][NEW[0]] = {
            **routed["bundles"][NEW[0]],
            "app_name": "another-app",
        }
    else:
        version = {"policyengine": NEW[0], "us": NEW[1], "uk": NEW[2]}[unrouted]
        routed["routes"][unrouted][version] = "another-app"

    with pytest.raises(ValueError, match="routes are not published"):
        registry.build_latest_routing_state(
            current_state=routed, deployed=_deployed(NEW)
        )


@pytest.mark.parametrize(
    ("current", "release", "force", "latest_moves"),
    [
        (None, NEW, False, True),
        (OLD, NEW, False, True),
        (NEW, OLD, False, False),
        (NEW, OLD, True, True),
        (NEW, NEW, False, False),
    ],
    ids=["first-release", "newer", "older", "older-forced", "same-version"],
)
def test_the_combined_publication_routes_the_release_and_moves_latest_as_before(
    monkeypatch, current, release, force, latest_moves
):
    earlier = "2026-11-01T00:00:00Z"
    now = "2026-11-06T12:00:00Z"
    before = None
    if current is not None:
        _install(monkeypatch, current)
        before = registry.build_next_routing_state(
            current_state=None, deployed=_deployed(current), now=earlier
        )
    _install(monkeypatch, OLD, NEW)

    state = registry.build_next_routing_state(
        current_state=before, deployed=_deployed(release), force_latest=force, now=now
    )

    app = versioned_app_name(release[0])
    assert state["routes"]["policyengine"][release[0]] == app
    assert state["routes"]["us"][release[1]] == app
    assert state["routes"]["uk"][release[2]] == app
    assert state["bundles"][release[0]]["app_name"] == app
    assert state["generation"] == f"{release[0]}:{app}"
    assert state["published_at"][release[0]] == (earlier if current == release else now)
    release_latest = {"policyengine": release[0], "us": release[1], "uk": release[2]}
    if latest_moves:
        assert state["latest"] == release_latest
        assert state["latest_since"] == now
    else:
        assert state["latest"] == before["latest"]
        assert state["latest_since"] == earlier


# --- Plans -------------------------------------------------------------------


def test_routes_plan_publishes_routes_but_does_not_advance_latest(monkeypatch):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)

    plan = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )

    assert registry.validate_routing_plan(plan) is plan
    assert plan["publication_id"] == "74401-1-beta"
    assert plan["before"] == before
    assert plan["after"]["latest"] == before["latest"]
    assert plan["after"]["routes"]["policyengine"][NEW[0]] == versioned_app_name(NEW[0])
    assert plan["after"]["generation"] == (
        f"{NEW[0]}:{versioned_app_name(NEW[0])}:74401-1-beta"
    )
    assert plan["advances_latest"] is False


def test_latest_plan_advances_latest_with_its_own_generation(monkeypatch):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )

    plan = registry.plan_latest_publication(
        current_state=routes["after"],
        deployed=_deployed(NEW),
        publication_id="74401-1-beta",
    )

    assert registry.validate_routing_plan(plan) is plan
    assert plan["before"] == routes["after"]
    assert plan["after"]["latest"]["policyengine"] == NEW[0]
    assert plan["after"]["generation"] == (
        f"latest:{NEW[0]}:{versioned_app_name(NEW[0])}:74401-1-beta"
    )
    assert plan["after_sha256"] != routes["after_sha256"]
    assert plan["advances_latest"] is True


def test_latest_plan_writes_a_new_state_even_when_latest_stays(monkeypatch):
    # A rerun's latest job always journals its own publication, so no record
    # from an earlier attempt is left as the run's outcome.
    newer = published_routing_state(monkeypatch, None, *NEW)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=newer, deployed=_deployed(OLD), publication_id="74401-1-beta"
    )

    plan = registry.plan_latest_publication(
        current_state=routes["after"],
        deployed=_deployed(OLD),
        publication_id="74401-1-beta",
    )

    assert plan["after"]["latest"] == newer["latest"]
    assert plan["before_sha256"] != plan["after_sha256"]
    assert plan["advances_latest"] is False


# --- The journal across the two publications and reruns ---------------------


def _record_apply_complete(plan):
    registry.record_routing_plan(environment="staging", plan=plan)
    registry.apply_routing_plan(environment="staging", plan=plan)
    registry.complete_routing_plan(environment="staging", plan=plan)


def _finalize(*, gates_passed):
    return registry.finalize_routing_publication(
        environment="staging",
        release_environment="beta",
        publication_run="74401",
        gates_passed=gates_passed,
    )


def test_a_release_completes_its_routes_then_its_latest(monkeypatch, patched_modal):
    before = _old_release_state(monkeypatch)
    store = install_routing_store(patched_modal, before)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )
    _record_apply_complete(routes)
    latest = registry.plan_latest_publication(
        current_state=store.get("active"),
        deployed=_deployed(NEW),
        publication_id="74401-1-beta",
    )
    _record_apply_complete(latest)

    assert _finalize(gates_passed=True) == "completed"
    assert store.get("active") == latest["after"]
    assert store.get("publication") == {"status": "completed", "plan": latest}


def test_a_failed_check_keeps_the_routes_and_leaves_latest(monkeypatch, patched_modal):
    before = _old_release_state(monkeypatch)
    store = install_routing_store(patched_modal, before)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )
    _record_apply_complete(routes)

    # A check failed, so the latest job never ran.
    assert _finalize(gates_passed=False) == "completed"
    assert store.get("active") == routes["after"]
    assert store.get("active")["latest"] == before["latest"]


def test_a_rerun_of_the_failed_jobs_moves_latest_without_republishing_routes(
    monkeypatch, patched_modal
):
    before = _old_release_state(monkeypatch)
    store = install_routing_store(patched_modal, before)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )
    _record_apply_complete(routes)
    # Attempt 1: the check after moving latest failed, and latest was put back.
    first = registry.plan_latest_publication(
        current_state=store.get("active"),
        deployed=_deployed(NEW),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=first)
    registry.apply_routing_plan(environment="staging", plan=first)
    assert registry.restore_routing_plan(environment="staging", plan=first) == (
        "restored"
    )
    assert _finalize(gates_passed=False) == "restored"
    assert store.get("active") == routes["after"]

    # Attempt 2 reruns only the failed jobs: the latest job and the finalizer.
    second = registry.plan_latest_publication(
        current_state=store.get("active"),
        deployed=_deployed(NEW),
        publication_id="74401-2-beta",
    )
    _record_apply_complete(second)

    assert _finalize(gates_passed=True) == "completed"
    assert store.get("active") == second["after"]
    assert store.get("active")["routes"] == routes["after"]["routes"]


def test_a_deployment_rollback_undoes_latest_and_then_the_routes(
    monkeypatch, patched_modal
):
    before = _old_release_state(monkeypatch)
    store = install_routing_store(patched_modal, before)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )
    _record_apply_complete(routes)
    latest = registry.plan_latest_publication(
        current_state=store.get("active"),
        deployed=_deployed(NEW),
        publication_id="74401-1-beta",
    )
    _record_apply_complete(latest)

    # The rollback passes each attempt's plans oldest first: routes, latest.
    assert (
        registry.restore_routing_plans(environment="staging", plans=[routes, latest])
        == "restored"
    )
    assert store.get("active") == before


# --- The command line --------------------------------------------------------


def _publishing_arguments(versions):
    version, us_version, uk_version = versions
    return (
        "--app-name",
        versioned_app_name(version),
        "--policyengine-version",
        version,
        "--us-version",
        us_version,
        "--uk-version",
        uk_version,
        "--publication-id",
        "74401-1-beta",
    )


def test_cli_plans_the_routes_and_then_latest_without_writing(
    monkeypatch, patched_modal, capsys, tmp_path
):
    for variable in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(variable, raising=False)
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    routed = registry.build_routes_routing_state(
        current_state=before, deployed=_deployed(NEW)
    )
    store = install_routing_store(patched_modal, routed)
    routes_file = tmp_path / "routes.json"
    latest_file = tmp_path / "latest.json"

    run_registry_main(
        monkeypatch, *_publishing_arguments(NEW), "--plan-out", str(routes_file)
    )
    routes_output = capsys.readouterr().out.splitlines()
    run_registry_main(
        monkeypatch,
        *_publishing_arguments(NEW),
        "--plan-latest-out",
        str(latest_file),
    )
    latest_output = capsys.readouterr().out.splitlines()

    routes = json.loads(routes_file.read_text())
    latest = json.loads(latest_file.read_text())
    assert routes["after"]["latest"] == before["latest"]
    assert routes_output == [
        f"before_sha256={routes['before_sha256']}",
        f"after_sha256={routes['after_sha256']}",
        "advances_latest=false",
    ]
    assert latest["after"]["latest"]["policyengine"] == NEW[0]
    assert latest["after"]["generation"].startswith("latest:")
    assert latest_output == [
        f"before_sha256={latest['before_sha256']}",
        f"after_sha256={latest['after_sha256']}",
        "advances_latest=true",
    ]
    assert store.snapshot() == {"active": json.loads(json.dumps(routed))}


def test_cli_refuses_to_plan_latest_for_a_release_without_routes(
    monkeypatch, patched_modal, tmp_path
):
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    install_routing_store(patched_modal, before)

    with pytest.raises(ValueError, match="routes are not published"):
        run_registry_main(
            monkeypatch,
            *_publishing_arguments(NEW),
            "--plan-latest-out",
            str(tmp_path / "latest.json"),
        )
    assert not (tmp_path / "latest.json").exists()
