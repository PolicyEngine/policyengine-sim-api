"""The PR job that runs the persistence tests needing PostgreSQL.

Those tests skip without a server, so the ``Test`` job never runs them; the
``Simulation database`` job gives them one and makes a missing server fail.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PR_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "pr.yml"
JOB_ID = "simulation-database"
STACK_CONDITION = (
    "${{ !github.event.pull_request.stack || "
    "github.event.pull_request.stack.base.ref == github.event.pull_request.base.ref }}"
)


def _jobs() -> dict:
    return yaml.safe_load(PR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_database_tests_run_on_postgres_and_may_not_skip() -> None:
    job = _jobs()[JOB_ID]
    server = job["services"]["postgres"]

    assert server["image"] == "postgres:17"
    assert job["env"]["SIMULATION_TEST_DATABASE_URL"] == (
        f"postgresql://postgres:{server['env']['POSTGRES_PASSWORD']}"
        "@127.0.0.1:5432/postgres"
    )
    assert job["env"]["SIMULATION_REQUIRE_DATABASE_TESTS"] == "1"
    test_step = job["steps"][-1]
    assert test_step["working-directory"] == "libs/policyengine-simulation-persistence"
    assert test_step["run"] == "uv run --frozen pytest tests/ -v -m postgres"


def test_ci_complete_needs_the_database_job_at_the_bottom_of_a_stack() -> None:
    jobs = _jobs()

    assert JOB_ID in jobs["ci-complete"]["needs"]
    assert jobs[JOB_ID]["if"] == STACK_CONDITION
