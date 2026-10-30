"""Tests for the token pr.yml's quality-guards job gives the rules guard.

The rules guard looks up the tracking issue of each [rule.downgrade] with gh,
which reads its token from GH_TOKEN (scripts/rules/tracking_issues.py). On
GitHub Actions the guard fails without a token, so the step that runs it needs
the job's token, and the job needs read access to issues.
"""

from __future__ import annotations

import pytest
import yaml
from workflow_script_support import WORKFLOWS_DIR

PR_WORKFLOW = WORKFLOWS_DIR / "pr.yml"
QUALITY_GUARDS_JOB = "quality-guards"
GUARD_COMMAND = "python scripts/run_quality_guards.py"


@pytest.fixture(scope="module")
def quality_guards_job() -> dict:
    workflow = yaml.safe_load(PR_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"][QUALITY_GUARDS_JOB]


def test_the_job_token_can_only_read_contents_and_issues(quality_guards_job):
    assert quality_guards_job["permissions"] == {"contents": "read", "issues": "read"}


def test_only_the_step_that_runs_the_guards_gets_the_token(quality_guards_job):
    steps_with_a_token = [
        step
        for step in quality_guards_job["steps"]
        if "GH_TOKEN" in step.get("env", {})
    ]

    assert [step["run"] for step in steps_with_a_token] == [GUARD_COMMAND]
    assert steps_with_a_token[0]["env"] == {"GH_TOKEN": "${{ github.token }}"}
