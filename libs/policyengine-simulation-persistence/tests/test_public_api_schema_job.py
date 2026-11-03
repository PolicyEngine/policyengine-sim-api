"""The PR job that compares sim's table models with the Public API's tables.

Rule sim.persistence.models-match-public-api. The job builds the Public
API's tables with its own v2 migration chain at the pinned commit, runs the
tests marked ``public_api_schema`` against them, and requires the pinned
commit to be on the Public API's default branch.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import tomllib
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PR_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pr.yml"
REGISTRY = REPOSITORY_ROOT / "rules" / "registry.toml"
JOB_ID = "public-api-schema"
PIN_FILE = "libs/policyengine-simulation-persistence/public-api-commit"
DRIFT_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "public-api-schema-drift.yml"
)
DRIFT_JOB_ID = "public-api-schema-drift"
STACK_CONDITION = (
    "${{ !github.event.pull_request.stack || "
    "github.event.pull_request.stack.base.ref == github.event.pull_request.base.ref }}"
)


def _jobs() -> dict:
    return yaml.safe_load(PR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_the_job_checks_out_the_pinned_public_api_commit() -> None:
    job = _jobs()[JOB_ID]
    pin = _step(job, "Read the pinned Public API commit")
    checkout = _step(job, "Check out the pinned Public API commit")

    assert pin["run"] == f".github/scripts/public-api-schema-commit.sh {PIN_FILE}"
    assert checkout["uses"] == "actions/checkout@v6"
    assert checkout["with"] == {
        "repository": "PolicyEngine/policyengine-api",
        "ref": f"${{{{ steps.{pin['id']}.outputs.commit }}}}",
        "path": "public-api",
        "persist-credentials": False,
    }


def test_the_job_builds_the_reference_and_compares_without_skipping() -> None:
    job = _jobs()[JOB_ID]
    server = job["services"]["postgres"]
    password = server["env"]["POSTGRES_PASSWORD"]

    assert server["image"] == "postgres:17"
    # The Public API's disposable migration mode accepts only this database.
    assert server["env"]["POSTGRES_DB"] == "policyengine_v2_alembic_test"
    assert job["env"] == {
        "SIMULATION_TEST_DATABASE_URL": (
            f"postgresql://postgres:{password}@127.0.0.1:5432/postgres"
        ),
        "PUBLIC_API_SCHEMA_DATABASE_URL": (
            f"postgresql://postgres:{password}@127.0.0.1:5432/"
            "policyengine_v2_alembic_test"
        ),
        "SIMULATION_REQUIRE_DATABASE_TESTS": "1",
        "SIMULATION_REQUIRE_PUBLIC_API_SCHEMA": "1",
    }
    names = [step.get("name") for step in job["steps"]]
    build = _step(job, "Build the Public API's tables at the pinned commit")
    compare = _step(job, "Compare sim's table models with the Public API's tables")
    on_default_branch = _step(
        job, "Require the pinned commit on the Public API's default branch"
    )

    assert build["run"] == ".github/scripts/public-api-schema-reference.sh public-api"
    assert compare["working-directory"] == "libs/policyengine-simulation-persistence"
    assert compare["run"] == "uv run --frozen pytest tests/ -v -m public_api_schema"
    assert on_default_branch["run"] == (
        ".github/scripts/public-api-commit-on-default-branch.sh public-api master"
    )
    assert (
        names.index(build["name"])
        < names.index(compare["name"])
        < names.index(on_default_branch["name"])
    )


def test_the_job_reads_no_secret() -> None:
    job_text = yaml.safe_dump(_jobs()[JOB_ID])

    assert "secrets." not in job_text
    assert _jobs()[JOB_ID]["permissions"] == {"contents": "read"}


def test_ci_complete_needs_the_job_at_the_bottom_of_a_stack() -> None:
    jobs = _jobs()

    assert JOB_ID in jobs["ci-complete"]["needs"]
    assert jobs[JOB_ID]["if"] == STACK_CONDITION


def _drift_workflow() -> dict:
    return yaml.safe_load(DRIFT_WORKFLOW.read_text(encoding="utf-8"))


def test_a_weekly_run_compares_the_models_with_the_default_branch() -> None:
    workflow = _drift_workflow()
    # PyYAML reads the bare key `on` as True.
    triggers = workflow[True]
    job = workflow["jobs"][DRIFT_JOB_ID]
    checkout = _step(job, "Check out the Public API's default branch")
    compare = _step(job, "Compare sim's table models with the Public API's tables")

    assert [entry["cron"] for entry in triggers["schedule"]] == ["17 6 * * 1"]
    assert "workflow_dispatch" in triggers
    assert checkout["with"] == {
        "repository": "PolicyEngine/policyengine-api",
        "ref": "master",
        "path": "public-api",
        "persist-credentials": False,
    }
    assert compare["run"] == "uv run --frozen pytest tests/ -v -m public_api_schema"
    assert job["env"] == _jobs()[JOB_ID]["env"]
    assert job["services"] == _jobs()[JOB_ID]["services"]


def test_the_weekly_run_reads_no_secret() -> None:
    workflow = _drift_workflow()

    assert "secrets." not in DRIFT_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}


SKIPPING_MARKS = frozenset({"skip", "skipif", "xfail"})
# The Simulation database job selects postgres but has no Public API
# database, so a schema test marked postgres could only skip there.
EXCLUDED_MARKERS = {"sim.persistence.models-match-public-api": {"postgres"}}


def _persistence_rules() -> list[dict]:
    return [
        entry
        for entry in tomllib.loads(REGISTRY.read_text(encoding="utf-8"))["rule"]
        if entry["enforcer"].get("project")
        == "libs/policyengine-simulation-persistence"
        and entry["enforcer"]["kind"] == "pytest"
    ]


def _marks(node_id: str) -> set[str]:
    """Return the names of a test's marks, its module's included."""
    module_path, test_name = node_id.split("::")
    module = importlib.import_module(Path(module_path).stem)
    module_marks = getattr(module, "pytestmark", [])
    if not isinstance(module_marks, list):
        module_marks = [module_marks]
    test_marks = getattr(getattr(module, test_name), "pytestmark", [])
    return {mark.name for mark in [*module_marks, *test_marks]}


def test_every_persistence_rule_test_runs_in_the_jobs_that_enforce_it() -> None:
    """Each rule's jobs select tests by marker, which the rules guard can't read.

    So each test a persistence rule names, its proof included, must carry
    every marker its jobs select, none that would send it to a job without
    its database, and no mark that would let it skip or fail unnoticed.
    """
    rules = _persistence_rules()
    assert {rule["id"] for rule in rules} >= set(EXCLUDED_MARKERS)
    for rule in rules:
        selected = {
            ci["invocation"].split(" -m ", 1)[1].split()[0]
            for ci in rule["ci"]
            if " -m " in ci["invocation"]
        }
        assert selected, rule["id"]
        for node_id in [*rule["enforcer"]["nodes"], rule["proof"]["test"]]:
            marks = _marks(node_id)
            assert selected <= marks, (rule["id"], node_id)
            assert not marks & EXCLUDED_MARKERS.get(rule["id"], set()), node_id
            assert not marks & SKIPPING_MARKS, node_id


def test_no_checkout_leaves_a_token_while_the_public_api_code_runs() -> None:
    """The jobs run the Public API's code from a pinned, perhaps unmerged commit."""
    jobs = [_jobs()[JOB_ID], _drift_workflow()["jobs"][DRIFT_JOB_ID]]

    for job in jobs:
        checkouts = [
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        assert len(checkouts) == 2
        for checkout in checkouts:
            assert checkout["with"]["persist-credentials"] is False, checkout


def test_the_weekly_run_builds_the_reference_and_installs_like_the_pr_job() -> None:
    pr_job = _jobs()[JOB_ID]
    drift_job = _drift_workflow()["jobs"][DRIFT_JOB_ID]
    build = _step(drift_job, "Build the Public API's tables at its default branch")

    assert build["run"] == ".github/scripts/public-api-schema-reference.sh public-api"
    assert _step(drift_job, "Install dependencies") == _step(
        pr_job, "Install dependencies"
    )
    assert _step(drift_job, "Install uv") == _step(pr_job, "Install uv")
    assert _step(drift_job, "Set up Python") == _step(pr_job, "Set up Python")
