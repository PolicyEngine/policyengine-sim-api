"""The weekly comparison's failure report: an issue, opened or commented on.

When the ``Public API schema drift`` workflow's comparison fails, a second job
runs ``public-api-schema-drift-issue.sh`` with the workflow's own token. The
script comments on the open issue that carries the drift label, or opens one.
The comparison job keeps its read-only token.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from test_public_api_schema_scripts import _fake_command, _run

if TYPE_CHECKING:
    import subprocess

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DRIFT_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "public-api-schema-drift.yml"
)
ISSUE_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "public-api-schema-drift-issue.sh"
)
COMPARISON_JOB_ID = "public-api-schema-drift"
REPORT_JOB_ID = "report-public-api-schema-drift"
DRIFT_LABEL = "public-api-schema-drift"
RUN_ENVIRONMENT = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "PolicyEngine/policyengine-sim-api",
    "GITHUB_RUN_ID": "123456",
}
RUN_URL = "https://github.com/PolicyEngine/policyengine-sim-api/actions/runs/123456"
# Logs each call; for `issue list`, applies the script's own --jq filter to
# the open issues as GitHub returns them, as gh does; saves the body an issue
# or comment reads from standard input; fails the named call.
FAKE_GH = """\
if [ "${1} ${2}" = "${FAKE_GH_FAILING_CALL:-}" ]; then exit 1; fi
case "${1} ${2}" in
  "issue list")
    while [ "${1}" != "--jq" ]; do shift; done
    jq -r "${2}" <<<"${FAKE_OPEN_ISSUES_JSON}"
    ;;
  "issue create" | "issue comment") cat > "${FAKE_COMMAND_LOG}.body" ;;
esac
"""


def _jobs() -> dict:
    return yaml.safe_load(DRIFT_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_a_second_job_reports_only_a_failed_comparison() -> None:
    job = _jobs()[REPORT_JOB_ID]

    assert job["needs"] == COMPARISON_JOB_ID
    assert job["if"] == "${{ failure() }}"


def test_only_the_reporting_job_may_write_issues() -> None:
    workflow = yaml.safe_load(DRIFT_WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"][COMPARISON_JOB_ID]
    assert workflow["jobs"][REPORT_JOB_ID]["permissions"] == {
        "contents": "read",
        "issues": "write",
    }


def test_the_reporting_job_runs_only_sims_script_with_the_workflow_token() -> None:
    """It never checks out or runs the Public API's code, and reads no secret."""
    job = _jobs()[REPORT_JOB_ID]
    checkout, report = job["steps"]

    assert checkout == {
        "uses": "actions/checkout@v6",
        "with": {"persist-credentials": False},
    }
    assert report["run"] == ".github/scripts/public-api-schema-drift-issue.sh"
    assert report["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
    }
    job_text = yaml.safe_dump(job)
    assert "secrets." not in job_text
    assert "policyengine-api" not in job_text


def _report(
    tmp_path: Path, **environment: str
) -> tuple[subprocess.CompletedProcess[str], list[str], str | None]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_command(fake_bin, "gh", FAKE_GH)
    result, calls = _run(ISSUE_SCRIPT, [], tmp_path, **(RUN_ENVIRONMENT | environment))
    body_file = tmp_path / "commands.log.body"
    body = body_file.read_text(encoding="utf-8") if body_file.exists() else None
    return result, calls, body


def _calls_starting(calls: list[str], prefix: str) -> list[str]:
    return [call for call in calls if call.startswith(f"gh|{prefix}")]


def test_a_first_failure_opens_an_issue_with_the_drift_label(tmp_path):
    result, calls, body = _report(tmp_path, FAKE_OPEN_ISSUES_JSON="[]")

    assert result.returncode == 0, result.stderr
    [label] = _calls_starting(calls, "label create")
    assert label.startswith(f"gh|label create {DRIFT_LABEL} --force")
    [created] = _calls_starting(calls, "issue create")
    assert f"--label {DRIFT_LABEL}" in created
    assert "--body-file -" in created
    assert _calls_starting(calls, "issue comment") == []
    assert RUN_URL in body
    assert "libs/policyengine-simulation-persistence/public-api-commit" in body
    # Any failure of the comparison job reports, setup steps included.
    assert "re-run" in body


def test_a_later_failure_comments_on_the_open_issue(tmp_path):
    result, calls, body = _report(
        tmp_path, FAKE_OPEN_ISSUES_JSON='[{"number": 42}, {"number": 7}]'
    )

    assert result.returncode == 0, result.stderr
    [listed] = _calls_starting(calls, "issue list")
    assert f"--label {DRIFT_LABEL}" in listed
    assert "--state open" in listed
    assert _calls_starting(calls, "issue comment") == [
        "gh|issue comment 42 --body-file -"
    ]
    assert _calls_starting(calls, "issue create") == []
    assert RUN_URL in body


def test_a_failed_github_request_fails_the_job(tmp_path):
    result, calls, body = _report(
        tmp_path, FAKE_OPEN_ISSUES_JSON="[]", FAKE_GH_FAILING_CALL="issue list"
    )

    assert result.returncode != 0
    assert _calls_starting(calls, "issue create") == []
    assert body is None


def test_the_report_needs_the_runs_address(tmp_path):
    result, calls, _ = _report(tmp_path, GITHUB_RUN_ID="")

    assert result.returncode != 0
    assert calls == []
