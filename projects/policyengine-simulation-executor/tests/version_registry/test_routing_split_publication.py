"""Tests for publishing a release's routes before its checks and latest after them.

A deployment adds the release's routes (its policyengine.py version and its
US and UK model versions, mapped to the deployed app) and bundle manifest
before its checks, leaving ``latest`` alone. A later job moves ``latest``
once every check has passed.
"""

from __future__ import annotations

import json
import re

import pytest
from registry_test_support import (
    install_bundle_manifests,
    install_routing_store,
    published_routing_state,
    run_registry_main,
    versioned_app_name,
)
from src.modal.routing_publisher import state as routing_state
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


@pytest.mark.parametrize(
    "empty",
    [lambda: None, dict, routing_state.empty_routing_state],
    ids=["missing", "empty", "empty-skeleton"],
)
def test_routes_state_routes_unpinned_requests_to_the_first_release(monkeypatch, empty):
    # With no routing yet there is no earlier release in the state to keep.
    _install(monkeypatch, NEW)

    state = registry.build_routes_routing_state(
        current_state=empty(), deployed=_deployed(NEW), now="2026-11-06T12:00:00Z"
    )

    assert state["latest"] == {"policyengine": NEW[0], "us": NEW[1], "uk": NEW[2]}
    assert state["latest_since"] == "2026-11-06T12:00:00Z"


def _stored_state(latest: dict, *, policyengine_routes: bool = True) -> dict:
    """Return a stored state with 6.0.0's routes and the given latest."""
    legacy = routing_state.coerce_routing_state(None)
    if policyengine_routes:
        legacy["routes"]["policyengine"]["6.0.0"] = versioned_app_name("6.0.0")
    legacy["routes"]["us"]["1.0.0"] = versioned_app_name("6.0.0")
    legacy["routes"]["uk"]["2.1.0"] = versioned_app_name("6.0.0")
    legacy["latest"] = latest
    return legacy


LEGACY_APP = "policyengine-simulation-us1-0-0-uk2-1-0"


def _model_version_latest_state() -> dict:
    """Seed a state from legacy Dicts that route by model version only."""
    return registry.build_legacy_seed_routing_state(
        policyengine_versions={},
        us_versions={"latest": "1.0.0", "1.0.0": LEGACY_APP},
        uk_versions={"latest": "2.1.0", "2.1.0": LEGACY_APP},
    )


def _with_manifest_of_another_app() -> dict:
    state = _stored_state({"policyengine": "6.0.0", "us": "1.0.0", "uk": "2.1.0"})
    state["bundles"]["6.0.0"] = {"policyengine_version": "6.0.0", "app_name": "other"}
    return state


def _complete_state() -> dict:
    return _stored_state({"policyengine": "6.0.0", "us": "1.0.0", "uk": "2.1.0"})


def _with_us_route(version: str, app: object) -> dict:
    state = _complete_state()
    state["routes"]["us"][version] = app
    return state


def _with_manifest(version: str, manifest: object) -> dict:
    state = _complete_state()
    state["bundles"][version] = manifest
    return state


def _with_routes_but_no_latest() -> dict:
    return _stored_state({})


# Stored values the routes step refuses: each is invalid, its latest does
# not name a routed policyengine.py, US and UK version, or it is not an empty
# state although it reads back as one.
INCOMPLETE_OR_INVALID = {
    "latest-without-model-versions": lambda: _stored_state({"policyengine": "6.0.0"}),
    "latest-without-a-uk-version": lambda: _stored_state(
        {"policyengine": "6.0.0", "us": "1.0.0"}
    ),
    "latest-without-a-policyengine-route": lambda: _stored_state(
        {"policyengine": "5.0.0", "us": "1.0.0", "uk": "2.1.0"},
        policyengine_routes=False,
    ),
    "latest-of-model-versions-only": _model_version_latest_state,
    "empty-latest-version": lambda: _stored_state(
        {"policyengine": "", "us": "1.0.0", "uk": "2.1.0"}
    ),
    "manifest-of-another-app": _with_manifest_of_another_app,
    "routes-without-latest": _with_routes_but_no_latest,
    "manifest-without-routes": lambda: {
        **routing_state.empty_routing_state(),
        "bundles": {"6.0.0": {"policyengine_version": "6.0.0", "app_name": "x"}},
    },
    # Values that read back as an empty state but are not one.
    "not-a-mapping": lambda: "not a routing state",
    "dates-only": lambda: {
        "published_at": {"6.0.0": "2026-11-01T00:00:00Z"},
        "latest_since": "2026-11-01T00:00:00Z",
    },
    "fields-of-the-wrong-type": lambda: {
        "schema_version": 2,
        "latest": "6.0.0",
        "routes": [],
        "bundles": "none",
    },
    # Complete states that read back differently: coercion would drop or
    # rewrite what makes them invalid.
    "another-schema-version": lambda: {**_complete_state(), "schema_version": 2},
    "a-route-that-is-not-an-app-name": lambda: _with_us_route("9.9.9", 5),
    "a-manifest-that-is-not-a-mapping": lambda: _with_manifest("6.0.0", "broken"),
    "a-field-routing-states-do-not-have": lambda: {**_complete_state(), "extra": 1},
}

# The combined publication accepts these, as before the split.
COMBINED_ACCEPTS = (
    "latest-without-model-versions",
    "latest-without-a-uk-version",
    "latest-without-a-policyengine-route",
    "latest-of-model-versions-only",
    "routes-without-latest",
)


@pytest.mark.parametrize(
    "stored", INCOMPLETE_OR_INVALID.values(), ids=INCOMPLETE_OR_INVALID
)
def test_the_routes_plan_refuses_a_stored_state_that_is_not_complete_and_valid(
    monkeypatch, stored
):
    # The planner raises, so the deploy has no plan to record or publish; an
    # operator repairs the stored state before deploying again.
    _install(monkeypatch, NEW)

    with pytest.raises(ValueError, match="is not complete and valid"):
        registry.plan_routes_publication(
            current_state=stored(), deployed=_deployed(NEW)
        )


@pytest.mark.parametrize("case", COMBINED_ACCEPTS)
def test_the_combined_publication_still_accepts_what_the_routes_step_refuses(
    monkeypatch, case
):
    _install(monkeypatch, NEW)

    state = registry.build_next_routing_state(
        current_state=INCOMPLETE_OR_INVALID[case](), deployed=_deployed(NEW)
    )

    assert state["latest"] == {"policyengine": NEW[0], "us": NEW[1], "uk": NEW[2]}


def test_the_routes_plan_refuses_rerouting_the_latest_version_to_the_release(
    monkeypatch,
):
    # latest is complete and names the release's version, whose stored route
    # names another app: publishing the release's route would send unpinned
    # requests to the release before its checks.
    stored = _stored_state({"policyengine": NEW[0], "us": "1.0.0", "uk": "2.1.0"})
    stored["routes"]["policyengine"][NEW[0]] = "another-app"
    _install(monkeypatch, NEW)

    with pytest.raises(
        ValueError,
        match=re.escape(f"latest.policyengine is {NEW[0]!r}, routed to 'another-app'"),
    ):
        registry.plan_routes_publication(current_state=stored, deployed=_deployed(NEW))


def test_routes_state_republishes_the_latest_release_to_its_own_app(monkeypatch):
    # A redeploy of the latest policyengine.py version routes to the same
    # app name, so unpinned requests reach the app they reached before.
    current = published_routing_state(monkeypatch, None, *NEW)
    _install(monkeypatch, NEW)

    routed = registry.build_routes_routing_state(
        current_state=current, deployed=_deployed(NEW)
    )

    assert routed["latest"] == current["latest"]
    assert routed["routes"]["policyengine"][NEW[0]] == versioned_app_name(NEW[0])


def test_the_combined_publication_stores_an_explicit_time_as_given(monkeypatch):
    _install(monkeypatch, NEW)
    now = "2026-11-06T13:00:00+01:00"

    state = registry.build_next_routing_state(
        current_state=None, deployed=_deployed(NEW), now=now
    )

    assert state["published_at"][NEW[0]] == now
    assert state["latest_since"] == now


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


# --- The command-line interface ---------------------------------------------


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


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda state: state.pop("bundles"), "missing bundles"),
        (lambda state: state.update(extra=1), "dropped when read: extra"),
        (
            lambda state: state.update(published_at={}),
            "dropped when read: published_at",
        ),
        (
            lambda state: state.update(latest_since="2026-11-06T13:00:00+01:00"),
            "rewritten when read: latest_since",
        ),
        (
            lambda state: state.update(schema_version=2),
            "rewritten when read: schema_version",
        ),
    ],
    ids=["missing", "extra", "empty-dates", "offset-time", "schema-version"],
)
def test_the_routes_plan_names_the_fields_that_do_not_read_back(
    monkeypatch, change, reason
):
    stored = _complete_state()
    change(stored)
    _install(monkeypatch, NEW)

    with pytest.raises(ValueError, match=re.escape(f"({reason})")):
        registry.plan_routes_publication(current_state=stored, deployed=_deployed(NEW))


def test_the_routes_plan_names_a_stored_value_that_is_not_a_mapping(monkeypatch):
    _install(monkeypatch, NEW)

    with pytest.raises(ValueError, match=re.escape("(it is a str, not a mapping)")):
        registry.plan_routes_publication(
            current_state="not a routing state", deployed=_deployed(NEW)
        )


def _restored_state(monkeypatch) -> dict:
    newer = published_routing_state(monkeypatch, None, *OLD)
    newer = published_routing_state(monkeypatch, newer, *NEW)
    return registry.build_restored_routing_state(
        current_state=newer, policyengine_version=OLD[0]
    )


def _latest_plan_state(monkeypatch) -> dict:
    before = _old_release_state(monkeypatch)
    _install(monkeypatch, OLD, NEW)
    routes = registry.plan_routes_publication(
        current_state=before, deployed=_deployed(NEW), publication_id="74401-1-beta"
    )
    return registry.plan_latest_publication(
        current_state=routes["after"],
        deployed=_deployed(NEW),
        publication_id="74401-1-beta",
    )["after"]


@pytest.mark.parametrize(
    "written",
    [_restored_state, _latest_plan_state],
    ids=["restored-latest", "latest-plan"],
)
def test_the_routes_plan_accepts_states_the_publisher_writes(monkeypatch, written):
    stored = json.loads(json.dumps(written(monkeypatch)))
    release = ("6.2.0", "1.3.0", "2.3.0")
    _install(monkeypatch, OLD, NEW, release)

    plan = registry.plan_routes_publication(
        current_state=stored, deployed=_deployed(release)
    )

    assert plan["after"]["latest"] == stored["latest"]
