"""Tests for the smoke check of the release identity the gateway serves."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import (
    IDENTITY,
    REPOSITORY_ROOT,
    REUSABLE_DEPLOY_WORKFLOW,
    VERSIONS_BODY,
    run_identity_smoke,
    workflow_jobs,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_smoke_accepts_the_gateway_identity_of_a_new_release(tmp_path: Path):
    result = run_identity_smoke(tmp_path, VERSIONS_BODY, **IDENTITY)

    assert result.returncode == 0, result.stderr
    assert (
        "serves policyengine 6.2.7 from policyengine-simulation-py6-2-7, "
        "policyengine-uk 2.126.8, UK dataset " + "a" * 64 + " as latest"
    ) in result.stdout


def test_smoke_accepts_an_older_release_by_its_route(tmp_path: Path):
    result = run_identity_smoke(
        tmp_path,
        VERSIONS_BODY,
        EXPECTED_IDENTITY="required",
        EXPECTED_POLICYENGINE_VERSION="6.2.6",
        EXPECTED_APP_NAME="policyengine-simulation-py6-2-6",
        EXPECTED_UK_MODEL_VERSION="2.125.1",
        EXPECTED_UK_DATASET_SHA256="5" * 64,
    )

    assert result.returncode == 0, result.stderr


def test_smoke_without_expectations_only_requires_versions_json(tmp_path: Path):
    result = run_identity_smoke(tmp_path, {})

    assert result.returncode == 0, result.stderr


def test_smoke_rejects_latest_routing_to_another_version(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["latest"] = "6.2.6"
    versions["uk"]["latest"] = "2.125.1"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to policyengine 6.2.6 and policyengine-uk 2.125.1" in (
        result.stderr
    )


def test_smoke_rejects_a_uk_latest_that_lags_the_wrapper(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["uk"]["latest"] = "2.125.1"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to" in result.stderr


def test_smoke_rejects_a_release_without_a_route(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    del versions["policyengine"]["6.2.7"]

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr


def test_smoke_rejects_a_uk_model_routed_to_another_app(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["uk"]["2.126.8"] = "policyengine-simulation-py6-2-6"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert (
        "policyengine-uk 2.126.8 routes to policyengine-simulation-py6-2-6, "
        "expected policyengine-simulation-py6-2-7"
    ) in result.stderr


def test_smoke_rejects_another_uk_dataset(tmp_path: Path):
    result = run_identity_smoke(
        tmp_path, VERSIONS_BODY, **{**IDENTITY, "EXPECTED_UK_DATASET_SHA256": "b" * 64}
    )

    assert result.returncode == 1
    assert "UK dataset SHA-256" in result.stderr


def test_smoke_rejects_identity_expectations_without_a_version(tmp_path: Path):
    result = run_identity_smoke(
        tmp_path, VERSIONS_BODY, EXPECTED_UK_DATASET_SHA256="a" * 64
    )

    assert result.returncode == 2
    assert "EXPECTED_POLICYENGINE_VERSION is required" in result.stderr


def test_smoke_accepts_an_unrecorded_digest_only_when_none_is_served(
    tmp_path: Path,
):
    """A release published before digests were recorded serves none."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    del versions["bundles"]["6.2.7"]["uk"]["dataset_sha256"]
    expected = {**IDENTITY, "EXPECTED_UK_DATASET_SHA256": "unrecorded"}
    (tmp_path / "unrecorded").mkdir()
    (tmp_path / "served").mkdir()
    result = run_identity_smoke(tmp_path / "unrecorded", versions, **expected)
    assert result.returncode == 0, result.stderr
    assert "UK dataset unrecorded as latest" in result.stdout
    served = run_identity_smoke(tmp_path / "served", VERSIONS_BODY, **expected)
    assert served.returncode == 1
    assert "is " + "a" * 64 + ", expected unrecorded" in served.stderr


def test_smoke_rejects_worker_routes_to_an_old_app(tmp_path: Path):
    """#743 round-2 review: the version and its model route to the new app."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["6.2.7"] = "policyengine-simulation-py6-2-6"
    versions["uk"]["2.126.8"] = "policyengine-simulation-py6-2-6"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert (
        "policyengine 6.2.7 routes to policyengine-simulation-py6-2-6, "
        "expected policyengine-simulation-py6-2-7"
    ) in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        "EXPECTED_POLICYENGINE_VERSION",
        "EXPECTED_APP_NAME",
        "EXPECTED_UK_MODEL_VERSION",
        "EXPECTED_UK_DATASET_SHA256",
    ],
)
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_smoke_refuses_an_incomplete_identity(tmp_path: Path, missing, how):
    """#743 round-2 review: an absent expectation never weakens the gate."""
    expected = dict(IDENTITY)
    if how == "unset":
        del expected[missing]
    else:
        expected[missing] = ""

    result = run_identity_smoke(tmp_path, VERSIONS_BODY, **expected)

    assert result.returncode == 2
    assert f"{missing} is required" in result.stderr


def test_smoke_requires_every_expectation_when_identity_is_required(tmp_path: Path):
    result = run_identity_smoke(tmp_path, VERSIONS_BODY, EXPECTED_IDENTITY="required")

    assert result.returncode == 2
    assert "is required" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EXPECTED_IDENTITY", "optional"),
        ("EXPECTED_LATEST", "yes"),
        ("EXPECTED_UK_DATASET_SHA256", "A" * 64),
        ("EXPECTED_UK_DATASET_SHA256", "a" * 63),
    ],
)
def test_smoke_refuses_malformed_expectations(tmp_path: Path, name, value):
    result = run_identity_smoke(tmp_path, VERSIONS_BODY, **{**IDENTITY, name: value})

    assert result.returncode == 2


def test_smoke_rejects_a_bundle_recorded_for_another_wrapper(tmp_path: Path):
    """#743 round-2 review: the bundle must be the expected version's own."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["bundles"]["6.2.7"]["uk"]["policyengine_version"] = "6.2.6"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr


def test_deploy_gates_pass_the_release_identity_to_the_smoke():
    """Review M2: the active workflow (not comments) wires the whole identity."""
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    assert (
        jobs["prepare"]["outputs"]["uk_dataset_sha256"]
        == "${{ steps.versions.outputs.uk_dataset_sha256 }}"
    )
    assert (
        jobs["deploy_executor"]["outputs"]["app_name"]
        == "${{ steps.app.outputs.name }}"
    )
    smoke_step = next(
        step
        for step in jobs["integration"]["steps"]
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", "")
    )
    expected = {
        "EXPECTED_IDENTITY": (
            "${{ inputs.deploy_existing_stack && 'required' || 'none' }}"
        ),
        "EXPECTED_APP_NAME": (
            "${{ inputs.deploy_existing_stack && "
            "needs.deploy_executor.outputs.app_name || '' }}"
        ),
        "EXPECTED_POLICYENGINE_VERSION": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.policyengine_version || '' }}"
        ),
        "EXPECTED_UK_MODEL_VERSION": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.uk_version || '' }}"
        ),
        "EXPECTED_UK_DATASET_SHA256": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.uk_dataset_sha256 || '' }}"
        ),
        # Latest must route to this release only when it should advance it.
        "EXPECTED_LATEST": (
            "${{ inputs.deploy_existing_stack && "
            "needs.update_routing.outputs.advances_latest == 'true' && 'true' || '' }}"
        ),
    }
    promote = jobs["promote_entrypoint"]
    for environment in (smoke_step["env"], promote["env"]):
        assert {key: environment.get(key) for key in expected} == expected
    for job in ("integration", "promote_entrypoint"):
        assert {"prepare", "deploy_executor", "update_routing"} <= set(
            jobs[job]["needs"]
        )
    # The promotion smoke runs inherit the job environment; no step overrides it.
    for step in promote["steps"]:
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", ""):
            assert not set(step.get("env") or {}) & set(expected)
    updates = workflow_jobs(
        REPOSITORY_ROOT / ".github" / "workflows" / "check-policyengine-updates.yml"
    )
    update_envs = [
        step.get("env") or {}
        for job in updates.values()
        for step in job.get("steps", [])
    ]
    assert any(
        env.get("RELEASE_PAYLOAD") == "${{ toJSON(github.event.client_payload) }}"
        for env in update_envs
    )


def test_smoke_rejects_another_uk_model(tmp_path: Path):
    result = run_identity_smoke(
        tmp_path, VERSIONS_BODY, **{**IDENTITY, "EXPECTED_UK_MODEL_VERSION": "2.125.1"}
    )

    assert result.returncode == 1
    assert "records policyengine-uk 2.126.8, expected 2.125.1" in result.stderr


def test_smoke_rejects_a_missing_bundle_identity(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["bundles"] = {}

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr
