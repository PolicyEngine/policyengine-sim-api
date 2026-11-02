"""Characterization tests of the command line's mode order, outputs and errors."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from registry_test_support import run_registry_main
from src.modal.utils import update_version_registry as registry

# --- Characterization: the CLI's mode order, outputs and argument errors -----

PUBLISHING_ARGUMENTS = (
    "--app-name",
    "policyengine-simulation-py6-3-0",
    "--policyengine-version",
    "6.3.0",
    "--us-version",
    "1.1.0",
    "--uk-version",
    "2.2.0",
)
DEPLOYED_6_3_0 = registry.DeployedAppVersions(
    app_name="policyengine-simulation-py6-3-0",
    policyengine_version="6.3.0",
    us_version="1.1.0",
    uk_version="2.2.0",
)
RECORDED_STATE = {
    "latest": {"policyengine": "6.2.6", "us": "1.1.0", "uk": "2.1.0"},
    "routes": {
        "policyengine": {"6.2.6": "app-6-2-6", "6.3.0": "app-6-3-0"},
        "us": {"1.1.0": "app-6-2-6"},
        "uk": {"2.1.0": "app-6-2-6", "2.2.0": "app-6-3-0", "2.0.0": "app-6-0-0"},
    },
    "bundles": {"6.2.6": {}},
}


@pytest.fixture
def recorded_writers(monkeypatch, patched_modal, tmp_path):
    """Replace each routing operation with a recorder, and write one plan file."""
    calls = []
    plan = {
        "publication_id": "p-1",
        "policyengine_version": "6.3.0",
        "after_sha256": "a" * 64,
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))

    def recorder(name, result):
        def record(**kwargs):
            calls.append((name, kwargs))
            return result

        return record

    for attribute, name, result in (
        ("finalize_routing_publication", "finalize", "restored"),
        ("record_routing_plan", "record", None),
        ("complete_routing_plan", "complete", None),
        ("apply_routing_plan", "apply", None),
        ("restore_routing_plans", "restore-plans", "restored"),
        ("restore_routing_state", "restore-latest", RECORDED_STATE),
        ("seed_active_routing_state_from_legacy", "seed", RECORDED_STATE),
        (
            "plan_routing_publication",
            "plan",
            {
                "before_sha256": "b" * 64,
                "after_sha256": "a" * 64,
                "advances_latest": False,
            },
        ),
        ("publish_routing_state", "publish", RECORDED_STATE),
    ):
        monkeypatch.setattr(registry, attribute, recorder(name, result))
    return SimpleNamespace(calls=calls, plan=plan, plan_file=str(plan_file))


@pytest.mark.parametrize(
    "mode_case",
    [
        (
            ("--print-latest", "--print-identity", "--apply-plan", "{plan}"),
            None,
            [""],
        ),
        (
            ("--print-identity", "--print-pending-plan", "--apply-plan", "{plan}"),
            None,
            [
                "policyengine_version=",
                "app_name=",
                "uk_model_version=",
                "uk_dataset_sha256=",
            ],
        ),
        (("--print-pending-plan", "--apply-plan", "{plan}"), None, ["null"]),
        (
            (
                "--finalize-publication",
                "--release-environment",
                "beta",
                "--gates-passed",
                "false",
                "--record-plan",
                "{plan}",
            ),
            (
                "finalize",
                {
                    "environment": "staging",
                    "release_environment": "beta",
                    "gates_passed": False,
                    "publication_run": None,
                },
            ),
            [
                "No pending routing publication; routing is unchanged.",
                "Routing publication finalization: restored",
            ],
        ),
        (
            ("--record-plan", "{plan}", "--complete-plan", "{plan}"),
            ("record", {"environment": "staging", "plan": "{plan_body}"}),
            ["Routing publication p-1: pending"],
        ),
        (
            ("--complete-plan", "{plan}", "--apply-plan", "{plan}"),
            ("complete", {"environment": "staging", "plan": "{plan_body}"}),
            ["Routing publication p-1: completed"],
        ),
        (
            ("--apply-plan", "{plan}", "--restore-plan", "{plan}"),
            ("apply", {"environment": "staging", "plan": "{plan_body}"}),
            [
                f"Published policyengine 6.3.0 routing ({'a' * 64}) in Modal "
                "environment staging"
            ],
        ),
        (
            ("--restore-plan", "{plan}", "{plan}", "--restore-latest", "6.2.6"),
            (
                "restore-plans",
                {"environment": "staging", "plans": ["{plan_body}", "{plan_body}"]},
            ),
            ["Routing for policyengine 6.3.0: restored in Modal environment staging"],
        ),
        (
            (
                "--restore-latest",
                "6.2.6",
                "--expected-latest",
                "6.3.0",
                "--seed-active-from-legacy",
            ),
            (
                "restore-latest",
                {
                    "environment": "staging",
                    "policyengine_version": "6.2.6",
                    "expected_latest": "6.3.0",
                },
            ),
            [
                "Restored routing state in Modal environment: staging",
                "  latest.policyengine: 6.2.6",
                "  latest.us: 1.1.0",
                "  latest.uk: 2.1.0",
            ],
        ),
        (
            ("--seed-active-from-legacy", *PUBLISHING_ARGUMENTS),
            ("seed", {"environment": "staging"}),
            [
                "Seeding active routing state from legacy Modal dicts in "
                "environment: staging",
                "",
                "  simulation-api-routing-state[active]: updated",
                "  policyengine routes: 2",
                "  US routes: 1",
                "  UK routes: 3",
                "  bundle manifests: 1",
                "",
                "Legacy routing state seed completed successfully.",
            ],
        ),
        (
            (*PUBLISHING_ARGUMENTS, "--dry-run", "--force-latest"),
            (
                "plan",
                {
                    "current_state": None,
                    "deployed": DEPLOYED_6_3_0,
                    "force_latest": True,
                    "publication_id": None,
                },
            ),
            [
                f"before_sha256={'b' * 64}",
                f"after_sha256={'a' * 64}",
                "advances_latest=false",
            ],
        ),
        (
            PUBLISHING_ARGUMENTS,
            (
                "publish",
                {
                    "environment": "staging",
                    "deployed": DEPLOYED_6_3_0,
                    "force_latest": False,
                },
            ),
            [
                "Publishing routing state in Modal environment: staging",
                "  App name: policyengine-simulation-py6-3-0",
                "  policyengine.py version: 6.3.0",
                "  US version: 1.1.0",
                "  UK version: 2.2.0",
                "",
                "",
                "  simulation-api-routing-state[active]: updated",
                "  latest.policyengine: 6.2.6",
                "  latest.us: 1.1.0",
                "  latest.uk: 2.1.0",
                "",
                "Routing state published successfully.",
            ],
        ),
    ],
    ids=[
        "print-latest",
        "print-identity",
        "print-pending-plan",
        "finalize",
        "record",
        "complete",
        "apply",
        "restore-plans",
        "restore-latest",
        "seed",
        "plan",
        "publish",
    ],
)
def test_main_runs_the_first_requested_mode_and_reports_it(
    monkeypatch, capsys, recorded_writers, mode_case
):
    """``mode_case`` is the arguments, the operation they run and its output."""
    arguments, operation, output = mode_case

    def resolved(value):
        if value == "{plan}":
            return recorded_writers.plan_file
        if value == "{plan_body}":
            return recorded_writers.plan
        if isinstance(value, list):
            return [resolved(item) for item in value]
        return value

    run_registry_main(monkeypatch, *(resolved(argument) for argument in arguments))

    expected_calls = []
    if operation is not None:
        name, kwargs = operation
        expected_calls = [
            (name, {key: resolved(value) for key, value in kwargs.items()})
        ]
    assert recorded_writers.calls == expected_calls
    assert capsys.readouterr().out.splitlines() == output


def test_main_reports_a_foreign_publication_before_its_outcome(
    monkeypatch, capsys, recorded_writers
):
    monkeypatch.setattr(
        registry,
        "finalize_routing_publication",
        lambda **_kwargs: "foreign-publication",
    )

    run_registry_main(
        monkeypatch,
        "--finalize-publication",
        "--publication-run",
        "74401",
        "--release-environment",
        "prod",
        "--gates-passed",
        "true",
    )

    assert recorded_writers.calls == []
    assert capsys.readouterr().out.splitlines() == [
        "No pending routing publication; routing is unchanged.",
        "Pending routing publication belongs to another run or environment; "
        "routing is unchanged.",
        "Routing publication finalization: foreign-publication",
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ("--finalize-publication", "--gates-passed", "false"),
            "--finalize-publication requires --release-environment and --gates-passed",
        ),
        (
            (
                "--finalize-publication",
                "--release-environment",
                "beta",
                "--gates-passed",
                "true",
            ),
            "operator recovery requires --gates-passed false",
        ),
        (
            ("--expected-latest", "6.2.6", *PUBLISHING_ARGUMENTS),
            "--expected-latest needs --restore-latest",
        ),
        (
            (),
            "the following arguments are required: --app-name, "
            "--policyengine-version, --us-version, --uk-version",
        ),
        (
            ("--app-name", "app", "--uk-version", "2.2.0", "--dry-run"),
            "the following arguments are required: --policyengine-version, "
            "--us-version",
        ),
    ],
    ids=[
        "finalize-inputs",
        "operator-recovery",
        "expected-latest",
        "publish-arguments",
        "plan-arguments",
    ],
)
def test_main_refuses_incomplete_arguments_before_any_operation(
    monkeypatch, capsys, recorded_writers, arguments, message
):
    with pytest.raises(SystemExit) as refusal:
        run_registry_main(monkeypatch, *arguments)

    assert refusal.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"update_version_registry: error: {message}"
    )
    assert recorded_writers.calls == []
