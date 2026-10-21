"""Tests for .github/scripts/check_pr_description.py.

Rule sim.process.pr-tracking-issue: the first line of every pull request's
description is ``Fixes #ISSUE_NUMBER``, naming an open issue in this
repository. Tests of how the script reads a description load it in this
process; tests of the issue lookup run it as the workflow does, with a fake
``gh`` first on PATH, so no test reaches GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import yaml
from workflow_script_support import (
    REPOSITORY_ROOT,
    SCRIPTS_DIR,
    WORKFLOWS_DIR,
    FakeCommands,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

CHECK_SCRIPT = SCRIPTS_DIR / "check_pr_description.py"
CHECK_WORKFLOW = WORKFLOWS_DIR / "pr-description.yml"
CHECK_JOB = "pr-description"
PROOF_FIXTURES = (
    REPOSITORY_ROOT / "rules" / "fixtures" / "sim.process.pr-tracking-issue"
)
REPOSITORY = "PolicyEngine/policyengine-sim-api"
VALID_BODY = "Fixes #861\n\n## Summary\n\n- Adds a check.\n"

# Prints FAKE_GH_STDOUT, and FAKE_GH_STDERR on stderr, then exits with
# FAKE_GH_EXIT, as gh api does with GitHub's answer.
FAKE_GH = """\
printf '%s' "${FAKE_GH_STDOUT:-}"
if [[ -n "${FAKE_GH_STDERR:-}" ]]; then
  printf '%s\\n' "$FAKE_GH_STDERR" >&2
fi
exit "${FAKE_GH_EXIT:-0}"
"""
# What gh api prints for a number GitHub has no issue or pull request for.
NOT_FOUND_STDOUT = (
    '{"message":"Not Found","documentation_url":'
    '"https://docs.github.com/rest/issues/issues#get-an-issue","status":"404"}'
)
NOT_FOUND_STDERR = "gh: Not Found (HTTP 404)"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    """Load the check script in this process, as the workflow runs it."""
    spec = importlib.util.spec_from_file_location("check_pr_description", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rejection(check_module: ModuleType, body: str | None) -> str:
    """Return the message the check gives for a description it rejects."""
    with pytest.raises(check_module.DescriptionError) as rejected:
        check_module.issue_number_from_description(body)
    return str(rejected.value)


# --- The first line -------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Fixes #861",
        "Fixes #861\n\n## Summary\n\n- Adds a check.\n",
    ],
)
def test_a_first_line_of_fixes_and_a_number_names_the_issue(check_module, body):
    assert check_module.issue_number_from_description(body) == 861


@pytest.mark.parametrize(
    "body",
    [
        # Descriptions edited in the GitHub web interface use CRLF.
        "Fixes #861\r\n\r\n## Summary\r\n",
        # A carriage return on its own also ends a line in Markdown.
        "Fixes #861\r## Summary",
    ],
)
def test_carriage_return_line_endings_end_the_first_line(check_module, body):
    assert check_module.issue_number_from_description(body) == 861


def test_trailing_whitespace_after_the_number_is_ignored(check_module):
    # From PR #405, edited in the web interface: a space, then CRLF.
    body = "Fixes #404 \r\n\r\n## Summary\r\n"

    assert check_module.issue_number_from_description(body) == 404


def test_a_description_without_a_fixes_line_is_rejected(check_module):
    message = _rejection(check_module, "## Summary\n\n- Adds a check.\n")

    assert 'The first line is "## Summary"' in message
    assert "first line is exactly Fixes #ISSUE_NUMBER" in message
    assert "sim.process.pr-tracking-issue" in message


def test_a_fixes_line_that_is_not_first_is_rejected(check_module):
    message = _rejection(check_module, "## Summary\n\nFixes #861\n")

    assert "Fixes #861 is on line 3" in message
    assert "must be the first line" in message


def test_a_blank_line_before_the_fixes_line_is_rejected(check_module):
    message = _rejection(check_module, "\r\nFixes #861\r\n")

    assert "Fixes #861 is on line 2" in message


def test_leading_whitespace_before_fixes_is_rejected(check_module):
    # Four leading spaces would make the line a Markdown code block, where
    # GitHub does not read closing keywords.
    message = _rejection(check_module, "    Fixes #861\n")

    assert "starts with whitespace" in message


@pytest.mark.parametrize("body", [None, "", " \r\n\t\n"])
def test_an_empty_or_missing_description_is_rejected(check_module, body):
    message = _rejection(check_module, body)

    assert "The description is empty." in message
    assert "first line is exactly Fixes #ISSUE_NUMBER" in message


@pytest.mark.parametrize(
    "first_line",
    [
        # From PR #616.
        "Fixes #615.",
        # From PR #543.
        "Refs #434",
        # From PR #750.
        "Fixes # (no tracking issue exists yet.)",
        "fixes #861",
        "Closes #861",
        "Fixes 861",
        "Fixes #",
        "Fixes #0",
        "Fixes #0861",
        "Fixes #861 and #862",
        "Fixes PolicyEngine/policyengine-api#3000",
        "Fixes https://github.com/PolicyEngine/policyengine-sim-api/issues/861",
        # Digits from another script are not issue numbers.
        "Fixes #٨٦١",
    ],
)
def test_other_first_lines_are_rejected(check_module, first_line):
    message = _rejection(check_module, f"{first_line}\n\n## Summary\n")

    assert f'The first line is "{first_line}"' in message


def test_a_long_first_line_is_shortened_in_the_message(check_module):
    first_line = "Draft, stacked on #743. " + "Gate releases on a sentinel. " * 10

    message = _rejection(check_module, first_line)

    assert 'The first line is "Draft, stacked on #743.' in message
    assert first_line not in message


# --- The issue lookup -----------------------------------------------------


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("gh", FAKE_GH)
    return commands


def _issue(
    number: int = 861, *, state: str = "open", repository: str = REPOSITORY, **fields
) -> dict:
    """Return the fields gh api prints for an issue, with ``fields`` added."""
    return {
        "number": number,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repository}",
        "html_url": f"https://github.com/{repository}/issues/{number}",
        **fields,
    }


def _write_event(tmp_path: Path, event: dict) -> Path:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return event_path


def _pull_request_event(body: str | None) -> dict:
    return {"action": "edited", "number": 862, "pull_request": {"body": body}}


def _run_check(
    fakes: FakeCommands, tmp_path: Path, body: str | None = VALID_BODY, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the check on a pull request event whose description is ``body``."""
    event_path = _write_event(tmp_path, _pull_request_event(body))
    return _run_script(
        fakes.env(
            GITHUB_EVENT_PATH=str(event_path), GITHUB_REPOSITORY=REPOSITORY, **env
        )
    )


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _gh_answers(issue: dict) -> dict[str, str]:
    """Environment for the fake gh to print ``issue`` and succeed."""
    return {"FAKE_GH_STDOUT": json.dumps(issue)}


def _error_annotation(result: subprocess.CompletedProcess[str]) -> str:
    """Return the one workflow error annotation the check printed."""
    annotations = [
        output_line
        for output_line in result.stdout.splitlines()
        if output_line.startswith("::error")
    ]
    assert len(annotations) == 1, result.stdout
    return annotations[0]


def test_a_description_naming_an_open_issue_in_this_repository_passes(fakes, tmp_path):
    result = _run_check(fakes, tmp_path, **_gh_answers(_issue()))

    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("gh") == [
        ["gh", "api", "repos/PolicyEngine/policyengine-sim-api/issues/861"]
    ]
    assert result.stdout == (
        "The first line is Fixes #861, and #861 is an open issue in "
        "PolicyEngine/policyengine-sim-api.\n"
    )


def test_a_rejected_description_is_reported_without_asking_github(fakes, tmp_path):
    result = _run_check(fakes, tmp_path, "## Summary\n\nFixes #861\n")

    assert result.returncode == 1
    assert "Fixes #861 is on line 3" in _error_annotation(result)
    assert fakes.calls("gh") == []


def test_a_null_description_in_the_event_fails(fakes, tmp_path):
    result = _run_check(fakes, tmp_path, None)

    assert result.returncode == 1
    assert "The description is empty." in _error_annotation(result)
    assert fakes.calls("gh") == []


def test_a_crlf_description_from_the_event_passes(fakes, tmp_path):
    result = _run_check(
        fakes, tmp_path, "Fixes #861\r\n\r\n## Summary\r\n", **_gh_answers(_issue())
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_closed_issue_fails(fakes, tmp_path):
    result = _run_check(fakes, tmp_path, **_gh_answers(_issue(state="closed")))

    assert result.returncode == 1
    annotation = _error_annotation(result)
    assert "Issue #861 is closed." in annotation
    assert "reopen it and re-run this job" in annotation


def test_a_pull_request_number_fails(fakes, tmp_path):
    pull_request = _issue(
        857,
        pull_request={
            "url": f"https://api.github.com/repos/{REPOSITORY}/pulls/857",
        },
    )

    result = _run_check(fakes, tmp_path, "Fixes #857\n", **_gh_answers(pull_request))

    assert result.returncode == 1
    assert "#857 is a pull request, not an issue." in _error_annotation(result)


@pytest.mark.parametrize(
    "gh_error",
    [NOT_FOUND_STDERR, "gh: This issue was deleted (HTTP 410)"],
)
def test_a_missing_issue_fails(fakes, tmp_path, gh_error):
    result = _run_check(
        fakes,
        tmp_path,
        "Fixes #999999\n",
        FAKE_GH_STDOUT=NOT_FOUND_STDOUT,
        FAKE_GH_STDERR=gh_error,
        FAKE_GH_EXIT="1",
    )

    assert result.returncode == 1
    assert (
        "There is no issue #999999 in PolicyEngine/policyengine-sim-api."
        in _error_annotation(result)
    )


def test_an_issue_moved_to_another_repository_fails(fakes, tmp_path):
    # Issue #154 was transferred to policyengine.py, where it is #115; gh api
    # follows GitHub's redirect and prints the moved issue.
    moved_issue = _issue(115, state="closed", repository="PolicyEngine/policyengine.py")

    result = _run_check(fakes, tmp_path, "Fixes #154\n", **_gh_answers(moved_issue))

    assert result.returncode == 1
    annotation = _error_annotation(result)
    assert (
        "#154 is not an issue in PolicyEngine/policyengine-sim-api: it moved to "
        "PolicyEngine/policyengine.py#115."
    ) in annotation


def test_an_issue_in_a_repository_whose_name_differs_only_in_case_passes(
    fakes, tmp_path
):
    issue = _issue(repository="policyengine/PolicyEngine-Sim-API")

    result = _run_check(fakes, tmp_path, **_gh_answers(issue))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "repository_url",
    [None, "", "https://api.github.com/orgs/PolicyEngine", "not a URL"],
)
def test_an_answer_that_names_no_repository_fails(fakes, tmp_path, repository_url):
    issue = _issue()
    issue["repository_url"] = repository_url

    result = _run_check(fakes, tmp_path, **_gh_answers(issue))

    assert result.returncode == 1
    assert "GitHub's answer for #861 names no repository" in _error_annotation(result)


def test_a_failed_lookup_fails_with_what_gh_reported(fakes, tmp_path):
    result = _run_check(
        fakes,
        tmp_path,
        FAKE_GH_STDERR="gh: Bad credentials (HTTP 401)",
        FAKE_GH_EXIT="1",
    )

    assert result.returncode == 1
    annotation = _error_annotation(result)
    assert "Could not look up #861 in PolicyEngine/policyengine-sim-api" in annotation
    assert "gh: Bad credentials (HTTP 401)" in annotation


@pytest.mark.parametrize("gh_stdout", ["not JSON", "[]"])
def test_an_answer_that_is_not_an_issue_object_fails(fakes, tmp_path, gh_stdout):
    result = _run_check(fakes, tmp_path, FAKE_GH_STDOUT=gh_stdout)

    assert result.returncode == 1
    assert "gh api did not print an issue for #861" in _error_annotation(result)


def test_messages_stay_on_one_annotation_line(fakes, tmp_path):
    result = _run_check(
        fakes,
        tmp_path,
        FAKE_GH_STDERR="first problem\nsecond problem 100%",
        FAKE_GH_EXIT="1",
    )

    assert result.returncode == 1
    assert result.stdout.count("\n") == 1
    assert "first problem%0Asecond problem 100%25" in _error_annotation(result)


@pytest.mark.parametrize("variable", ["GITHUB_EVENT_PATH", "GITHUB_REPOSITORY"])
def test_the_check_requires_its_environment(fakes, tmp_path, variable):
    event_path = _write_event(tmp_path, _pull_request_event(VALID_BODY))
    env = fakes.env(GITHUB_EVENT_PATH=str(event_path), GITHUB_REPOSITORY=REPOSITORY)
    del env[variable]

    result = _run_script(env)

    assert result.returncode == 1
    assert f"{variable} is required" in result.stderr
    assert fakes.calls("gh") == []


def test_the_check_requires_a_pull_request_event(fakes, tmp_path):
    event_path = _write_event(tmp_path, {"ref": "refs/heads/main"})

    result = _run_script(
        fakes.env(GITHUB_EVENT_PATH=str(event_path), GITHUB_REPOSITORY=REPOSITORY)
    )

    assert result.returncode == 1
    assert "has no pull_request" in result.stderr
    assert fakes.calls("gh") == []


# --- The workflow job -----------------------------------------------------


@pytest.fixture(scope="module")
def check_workflow() -> dict:
    return yaml.safe_load(CHECK_WORKFLOW.read_text(encoding="utf-8"))


def test_the_check_runs_on_every_event_that_can_change_the_description(
    check_workflow,
):
    # PyYAML reads the key "on" as True.
    triggers = check_workflow.get("on", check_workflow.get(True))

    # No branches or paths filter: every pull request is checked, whatever it
    # targets and whatever it changes.
    assert triggers == {
        "pull_request": {"types": ["opened", "edited", "reopened", "synchronize"]}
    }


def test_the_check_runs_on_every_open_pull_request_including_stacked_ones(
    check_workflow,
):
    """Unlike pr.yml's jobs, this one has no stack condition and needs no job.

    It skips closed pull requests: editing a merged pull request's description
    would otherwise fail the check, because merging closed its issue.
    """
    assert list(check_workflow["jobs"]) == [CHECK_JOB]
    job = check_workflow["jobs"][CHECK_JOB]

    assert job["name"] == "PR description"
    assert job["if"] == "${{ github.event.pull_request.state == 'open' }}"
    assert "needs" not in job


def test_the_check_can_only_read(check_workflow):
    assert check_workflow["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert "permissions" not in check_workflow["jobs"][CHECK_JOB]


def test_the_check_runs_the_tested_script_with_the_job_token(check_workflow):
    steps = check_workflow["jobs"][CHECK_JOB]["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    check_steps = [step for step in steps if CHECK_SCRIPT.name in step.get("run", "")]

    assert checkout["with"]["sparse-checkout"] == (
        f".github/scripts/{CHECK_SCRIPT.name}"
    )
    assert len(check_steps) == 1
    assert check_steps[0]["run"] == f"python .github/scripts/{CHECK_SCRIPT.name}"
    assert check_steps[0]["env"] == {"GH_TOKEN": "${{ github.token }}"}


# --- Proof ----------------------------------------------------------------


def test_rule_proof_pr_tracking_issue(fakes):
    """The check rejects PR #616's first line, "Fixes #615.", and accepts it fixed.

    Both fixtures are pull_request event payloads; the fake gh answers that
    #615 is an open issue in this repository.
    """
    results = {
        fixture: _run_script(
            fakes.env(
                GITHUB_EVENT_PATH=str(PROOF_FIXTURES / f"{fixture}_event.json"),
                GITHUB_REPOSITORY=REPOSITORY,
                **_gh_answers(_issue(615)),
            )
        )
        for fixture in ("bad", "good")
    }

    assert results["bad"].returncode == 1
    assert 'The first line is "Fixes #615."' in _error_annotation(results["bad"])
    assert results["good"].returncode == 0, results["good"].stdout
    assert fakes.calls("gh") == [
        ["gh", "api", "repos/PolicyEngine/policyengine-sim-api/issues/615"]
    ]
