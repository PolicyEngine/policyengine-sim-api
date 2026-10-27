"""Tests for the check that only serialized workflows write routing."""

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
from src.modal.routing_publisher import publication as routing_publication
from src.modal.routing_publisher import stores as routing_stores
from src.modal.routing_publisher import writers as routing_writers
from src.modal.utils import update_version_registry as registry


@pytest.mark.parametrize(
    "workflow",
    ["simulation-deploy.yml", "stage12-deploy.yml", "simulation-rollback.yml"],
)
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v6.3.0", "a" * 40])
@pytest.mark.usefixtures("serialized_workflow")
def test_routing_guard_accepts_the_serialized_caller_workflows(
    monkeypatch, workflow, ref
):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"PolicyEngine/policyengine-sim-api/.github/workflows/{workflow}@{ref}",
    )
    routing_writers.require_serialized_routing_workflow()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GITHUB_ACTIONS", None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_ACTIONS", "True"),
        ("GITHUB_REPOSITORY", None),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        ("GITHUB_REPOSITORY", "policyengine/policyengine-sim-api"),
        ("GITHUB_WORKFLOW_REF", None),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.reusable.yml@refs/heads/main"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "a-fork/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/simulation-deploy.yml",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/simulation-deploy.yml@",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main@extra"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main extra"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml.extra@refs/heads/main"
            ),
        ),
    ],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_routing_guard_requires_every_condition(monkeypatch, variable, value):
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError, match="Routing writes require GitHub Actions"):
        routing_writers.require_serialized_routing_workflow()


@pytest.mark.parametrize(
    ("writer", "kwargs"),
    [
        ("record_routing_plan", {"plan": {}}),
        ("complete_routing_plan", {"plan": {}}),
        (
            "finalize_routing_publication",
            {"release_environment": "beta", "gates_passed": False},
        ),
        ("apply_routing_plan", {"plan": {}}),
        ("restore_routing_plans", {"plans": [{}]}),
        ("restore_routing_plan", {"plan": {}}),
        ("restore_routing_state", {"policyengine_version": "6.2.6"}),
        (
            "publish_routing_state",
            {
                "deployed": registry.DeployedAppVersions(
                    app_name="policyengine-simulation-py6-3-0",
                    policyengine_version="6.3.0",
                    us_version="1.1.0",
                    uk_version="2.2.0",
                ),
            },
        ),
        ("seed_active_routing_state_from_legacy", {}),
    ],
    ids=[
        "record",
        "complete",
        "finalize",
        "apply",
        "restore-plans",
        "restore-plan",
        "rollback",
        "publish",
        "seed",
    ],
)
@pytest.mark.parametrize(
    "environment_override",
    [
        (None, None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
    ],
    ids=["local", "not-actions", "fork", "unserialized-workflow"],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_library_writers_refuse_unserialized_calls_before_validation_or_modal_access(
    monkeypatch, patched_modal, writer, kwargs, environment_override
):
    """``environment_override`` names a GitHub variable and its value, or None."""
    variable, value = environment_override
    if variable is None:
        for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
            monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(variable, value)

    def forbidden_access(*_args, **_kwargs):
        pytest.fail("a refused library write accessed Modal or delegated its write")

    monkeypatch.setattr(routing_stores.modal.Dict, "from_name", forbidden_access)
    if writer == "restore_routing_plan":
        # Each public writer checks its caller, including the one-plan wrapper.
        monkeypatch.setattr(
            routing_publication, "restore_routing_plans", forbidden_access
        )
    with pytest.raises(RuntimeError, match="Routing writes require GitHub Actions"):
        getattr(registry, writer)(environment="staging", **kwargs)
    assert patched_modal == {}


@pytest.mark.parametrize(
    "args",
    [
        (
            "--app-name",
            "policyengine-simulation-py6-3-0",
            "--policyengine-version",
            "6.3.0",
            "--us-version",
            "1.1.0",
            "--uk-version",
            "2.2.0",
        ),
        ("--record-plan", "missing-plan.json"),
        ("--complete-plan", "missing-plan.json"),
        (
            "--finalize-publication",
            "--release-environment",
            "beta",
            "--gates-passed",
            "false",
        ),
        ("--apply-plan", "missing-plan.json"),
        ("--restore-plan", "missing-plan.json"),
        ("--restore-latest", "6.2.6"),
        ("--seed-active-from-legacy",),
        ("--apply-plan", "missing-plan.json", "--plan-out", "unused-plan.json"),
        ("--restore-latest", "6.2.6", "--dry-run"),
    ],
    ids=[
        "publish",
        "record",
        "complete",
        "finalize",
        "apply",
        "restore-plan",
        "rollback-version",
        "seed",
        "apply-with-plan-out",
        "restore-with-dry-run",
    ],
)
@pytest.mark.parametrize(
    "environment_override",
    [
        (None, None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
    ],
    ids=["local", "not-actions", "fork", "unserialized-workflow"],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_main_refuses_unserialized_writes_before_accessing_modal_or_plan_files(
    monkeypatch, patched_modal, capsys, args, environment_override
):
    """``environment_override`` names a GitHub variable and its value, or None."""
    variable, value = environment_override
    if variable is None:
        for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
            monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(variable, value)

    def forbidden_modal_access(*_args, **_kwargs):
        pytest.fail("a refused CLI write accessed Modal")

    monkeypatch.setattr(routing_stores.modal.Dict, "from_name", forbidden_modal_access)
    with pytest.raises(SystemExit) as refusal:
        run_registry_main(monkeypatch, *args)
    assert refusal.value.code == 2
    error = capsys.readouterr().err
    assert "simulation-deploy-main" in error
    for workflow in (
        "simulation-deploy.yml",
        "stage12-deploy.yml",
        "simulation-rollback.yml",
    ):
        assert workflow in error
    assert patched_modal == {}


@pytest.mark.parametrize(
    "mode", ["--print-latest", "--print-identity", "--plan-out", "--dry-run"]
)
def test_main_keeps_read_only_and_plan_modes_available_locally(
    monkeypatch, patched_modal, capsys, tmp_path, mode
):
    for variable in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(variable, raising=False)
    state = published_routing_state(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = install_routing_store(patched_modal, state)
    args = [mode]
    if mode in {"--plan-out", "--dry-run"}:
        if mode == "--plan-out":
            args.append(str(tmp_path / "local-plan.json"))
        args.extend(
            [
                "--app-name",
                versioned_app_name("6.3.0"),
                "--policyengine-version",
                "6.3.0",
                "--us-version",
                "1.1.0",
                "--uk-version",
                "2.2.0",
            ]
        )
        install_bundle_manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    run_registry_main(monkeypatch, *args)
    assert capsys.readouterr().out
    assert store.snapshot() == {"active": json.loads(json.dumps(state))}
    if mode == "--plan-out":
        plan = json.loads((tmp_path / "local-plan.json").read_text())
        assert plan["before"] == state
        assert plan["after"]["latest"]["policyengine"] == "6.3.0"


def test_cli_help_explains_serialized_writes_and_has_no_lease_option(
    monkeypatch, capsys
):
    with pytest.raises(SystemExit) as help_exit:
        run_registry_main(monkeypatch, "--help")
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "simulation-deploy-main" in help_text
    assert "--lock-wait-seconds" not in help_text
    for workflow in (
        "simulation-deploy.yml",
        "stage12-deploy.yml",
        "simulation-rollback.yml",
    ):
        assert workflow in help_text
