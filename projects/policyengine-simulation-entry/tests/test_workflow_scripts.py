"""Tests for the .github/scripts/ workflow scripts no deployment test covers.

Each test runs the script with fake commands first on PATH.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from changelog_release_support import load_fragments_module
from workflow_script_support import (
    REPOSITORY_ROOT,
    WORKFLOWS_DIR,
    FakeCommands,
    github_output,
    run_script,
)


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    return FakeCommands.create(tmp_path)


def test_scripts_that_workflows_run_directly_are_executable():
    """A step that runs a script without ``bash`` needs its executable bit."""
    direct = re.compile(r"^\s*run: (\.github/scripts/[\w.-]+\.sh)\b", re.MULTILINE)
    scripts = {
        script
        for workflow in WORKFLOWS_DIR.glob("*.yml")
        for script in direct.findall(workflow.read_text(encoding="utf-8"))
    }

    assert ".github/scripts/modal-image-smoke.sh" in scripts
    for script in sorted(scripts):
        assert os.access(REPOSITORY_ROOT / script, os.X_OK), script


# --- update-dependencies.yml ----------------------------------------------

PREPARE_BRANCH = "dependency-update-prepare-branch.sh"
COMMIT_AND_PUSH = "dependency-update-commit-and-push.sh"
CREATE_PR = "dependency-update-create-pr.sh"

REMOTE_HEAD = "0123456789abcdef0123456789abcdef01234567"

# The scripts no longer ask gh about pull requests; this fake answers as the
# pre-fix script expected, so a test can show that an open PR changes nothing.
FAKE_GH_PR_LIST = """\
if [[ "$1 $2" == "pr list" ]]; then
  printf '%s' "${FAKE_OPEN_PR:-}"
fi
"""

# Each run rebuilds the update branch from main; the fake git reports the
# remote branch's head from FAKE_REMOTE_HEAD (unset: the branch does not exist).
FAKE_GIT = """\
case "$1" in
  ls-remote)
    if [[ -n "${FAKE_REMOTE_HEAD:-}" ]]; then
      printf '%s\\trefs/heads/update-dependencies\\n' "${FAKE_REMOTE_HEAD}"
    fi
    exit "${FAKE_GIT_LS_REMOTE_EXIT:-0}"
    ;;
  status) printf '%s' "${FAKE_GIT_STATUS:-}" ;;
  diff) exit "${FAKE_GIT_DIFF_EXIT:-1}" ;;
  push) exit "${FAKE_GIT_PUSH_EXIT:-0}" ;;
esac
"""
GIT_IDENTITY = [
    ["git", "config", "user.name", "policyengine-auto"],
    [
        "git",
        "config",
        "user.email",
        "policyengine-auto@users.noreply.github.com",
    ],
]
REMOTE_BRANCH_LOOKUP = [
    "git",
    "ls-remote",
    "--heads",
    "origin",
    "refs/heads/update-dependencies",
]
REBUILD_FROM_MAIN = ["git", "checkout", "-B", "update-dependencies", "origin/main"]


def test_update_dependencies_workflow_runs_the_tested_scripts():
    workflow = (WORKFLOWS_DIR / "update-dependencies.yml").read_text(encoding="utf-8")

    for script in (PREPARE_BRANCH, COMMIT_AND_PUSH, CREATE_PR):
        assert f"run: .github/scripts/{script}" in workflow


def _update_dependencies_steps() -> dict[str, dict]:
    workflow = yaml.safe_load(
        (WORKFLOWS_DIR / "update-dependencies.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["update-dependencies"]["steps"]
    return {step["run"]: step for step in steps if "run" in step}


def _prepare_branch(fakes: FakeCommands, tmp_path: Path, **env: str):
    output = tmp_path / "github-output"
    fakes.add("gh", FAKE_GH_PR_LIST)
    fakes.add("git", FAKE_GIT)
    result = run_script(
        PREPARE_BRANCH,
        env=fakes.env(
            GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api",
            GITHUB_OUTPUT=str(output),
            **env,
        ),
    )
    return result, output


def test_prepare_branch_rebuilds_the_branch_from_main_when_an_update_pr_is_open(
    fakes, tmp_path
):
    """Merging main into an old update branch mixed two uv.lock resolutions."""
    result, output = _prepare_branch(
        fakes, tmp_path, FAKE_OPEN_PR="583", FAKE_REMOTE_HEAD=REMOTE_HEAD
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls("git") == [
        *GIT_IDENTITY,
        REMOTE_BRANCH_LOOKUP,
        ["git", "fetch", "--quiet", "origin", REMOTE_HEAD],
        REBUILD_FROM_MAIN,
    ]
    assert fakes.calls("gh") == []
    assert github_output(output) == {"remote_head": REMOTE_HEAD}


def test_prepare_branch_starts_from_main_without_an_update_branch(fakes, tmp_path):
    result, output = _prepare_branch(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {"remote_head": ""}
    assert fakes.calls("git") == [
        *GIT_IDENTITY,
        REMOTE_BRANCH_LOOKUP,
        REBUILD_FROM_MAIN,
    ]


def test_prepare_branch_fails_when_the_branch_lookup_fails(fakes, tmp_path):
    result, output = _prepare_branch(fakes, tmp_path, FAKE_GIT_LS_REMOTE_EXIT="128")

    assert result.returncode != 0
    assert github_output(output) == {}
    assert not any(call[1] == "checkout" for call in fakes.calls("git"))


def test_prepare_branch_requires_the_output_file(fakes, tmp_path):
    fakes.add("git", FAKE_GIT)
    env = fakes.env()
    env.pop("GITHUB_OUTPUT", None)

    result = run_script(PREPARE_BRANCH, env=env)

    assert result.returncode != 0
    assert "GITHUB_OUTPUT is required" in result.stderr
    assert fakes.calls() == []


def test_update_dependencies_workflow_passes_the_recorded_head_to_the_push():
    steps = _update_dependencies_steps()

    assert "env" not in steps[f".github/scripts/{PREPARE_BRANCH}"]
    assert steps[f".github/scripts/{COMMIT_AND_PUSH}"]["env"] == {
        "REMOTE_HEAD": "${{ steps.branch.outputs.remote_head }}"
    }


# Every pull request adds a changelog fragment (rule
# sim.process.changelog-fragment); the update's never changes, so the push
# still compares only the update with the remote branch.
DEPENDENCY_UPDATE_FRAGMENT = "changelog.d/update-dependencies.changed.md"
DEPENDENCY_UPDATE_FRAGMENT_TEXT = (
    "Updated every project's locked dependencies to the newest versions their "
    "requirements allow.\n"
)
# Records which fragments exist when git add stages the update.
FAKE_GIT_RECORDING_FRAGMENTS = """\
if [[ "$1" == "add" ]]; then
  ls changelog.d > "$FAKE_FRAGMENTS_AT_ADD" 2>/dev/null || true
fi
"""


def _commit_and_push(
    fakes: FakeCommands, tmp_path: Path, git_body: str = FAKE_GIT, **env: str
):
    """Run the script in an empty checkout folder, which it may write a fragment to."""
    output = tmp_path / "github-output"
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    fakes.add("git", git_body)
    fakes.add("date", "echo 2026-10-13\n")
    result = run_script(
        COMMIT_AND_PUSH,
        env=fakes.env(GITHUB_OUTPUT=str(output), **env),
        cwd=checkout,
    )
    return result, output


def test_commit_and_push_replaces_the_branch_with_a_lease_on_the_recorded_head(
    fakes, tmp_path
):
    result, output = _commit_and_push(
        fakes, tmp_path, REMOTE_HEAD=REMOTE_HEAD, FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert "Changes detected" in result.stdout
    assert github_output(output)["branch_pushed"] == "true"
    git_calls = fakes.calls("git")
    assert git_calls[:3] == [
        ["git", "status", "--porcelain"],
        ["git", "add", "--all"],
        ["git", "commit", "-m", "Update dependencies (2026-10-13)"],
    ]
    assert git_calls[-1] == [
        "git",
        "push",
        f"--force-with-lease=refs/heads/update-dependencies:{REMOTE_HEAD}",
        "origin",
        "HEAD:refs/heads/update-dependencies",
    ]
    assert fakes.calls("date") == [["date", "-u", "+%Y-%m-%d"]]


def test_commit_and_push_adds_a_changelog_fragment_to_the_update(fakes, tmp_path):
    fragments_at_add = tmp_path / "fragments-at-add"
    result, _output = _commit_and_push(
        fakes,
        tmp_path,
        git_body=FAKE_GIT_RECORDING_FRAGMENTS + FAKE_GIT,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_FRAGMENTS_AT_ADD=str(fragments_at_add),
    )

    assert result.returncode == 0, result.stderr
    fragment = tmp_path / "checkout" / DEPENDENCY_UPDATE_FRAGMENT
    assert fragment.read_text(encoding="utf-8") == DEPENDENCY_UPDATE_FRAGMENT_TEXT
    assert fragments_at_add.read_text(encoding="utf-8") == (
        "update-dependencies.changed.md\n"
    )
    definition = load_fragments_module().FragmentDefinition.load(REPOSITORY_ROOT)
    assert definition.problem_with(DEPENDENCY_UPDATE_FRAGMENT, fragment) is None


def test_commit_and_push_adds_no_fragment_without_an_update(fakes, tmp_path):
    result, _output = _commit_and_push(fakes, tmp_path, REMOTE_HEAD=REMOTE_HEAD)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "checkout" / "changelog.d").exists()


def test_commit_and_push_creates_the_branch_with_a_plain_push(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes, tmp_path, REMOTE_HEAD="", FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert github_output(output)["branch_pushed"] == "true"
    assert fakes.calls("git")[3:] == [
        ["git", "push", "origin", "HEAD:refs/heads/update-dependencies"]
    ]


def test_commit_and_push_skips_the_push_when_the_branch_holds_the_update(
    fakes, tmp_path
):
    """The commit's date differs every day, so the push compares files."""
    result, output = _commit_and_push(
        fakes,
        tmp_path,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_GIT_DIFF_EXIT="0",
    )

    assert result.returncode == 0, result.stderr
    assert "update-dependencies already holds this update" in result.stdout
    assert github_output(output) == {
        "changes_detected": "true",
        "branch_pushed": "false",
    }
    assert fakes.calls("git")[-1] == [
        "git",
        "diff",
        "--quiet",
        REMOTE_HEAD,
        "HEAD",
        "--",
    ]


def test_commit_and_push_reports_a_push_that_changed_the_branch(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes, tmp_path, REMOTE_HEAD=REMOTE_HEAD, FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {
        "changes_detected": "true",
        "branch_pushed": "true",
    }
    assert ["git", "diff", "--quiet", REMOTE_HEAD, "HEAD", "--"] in fakes.calls("git")


@pytest.mark.parametrize("remote_head", [REMOTE_HEAD, ""])
def test_commit_and_push_does_nothing_when_the_update_changes_nothing(
    fakes, tmp_path, remote_head
):
    """Pushing main's own commit would leave an open update PR with no changes."""
    result, output = _commit_and_push(fakes, tmp_path, REMOTE_HEAD=remote_head)

    assert result.returncode == 0, result.stderr
    assert "No changes detected" in result.stdout
    assert github_output(output) == {
        "changes_detected": "false",
        "branch_pushed": "false",
    }
    assert fakes.calls("git") == [["git", "status", "--porcelain"]]


def test_commit_and_push_rejects_a_remote_head_that_is_not_a_commit(fakes, tmp_path):
    result, output = _commit_and_push(fakes, tmp_path, REMOTE_HEAD="main")

    assert result.returncode == 1
    assert "REMOTE_HEAD must be empty or a full commit hash" in result.stderr
    assert github_output(output) == {}
    assert fakes.calls() == []


def test_commit_and_push_fails_when_the_push_fails(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes,
        tmp_path,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_GIT_PUSH_EXIT="1",
    )

    assert result.returncode != 0
    assert github_output(output) == {}


# The create-PR script finds the open "Update dependencies" issue, or opens
# one, and names it on the first line of the update PR's description (rule
# sim.process.pr-tracking-issue). The fake gh answers each call from the
# environment and writes each description it is given to FAKE_PR_BODY_LOG.
FAKE_GH_FOR_CREATE_PR = """\
case "$1 $2" in
  "pr list") printf '%s' "${FAKE_OPEN_PR:-}" ;;
  "pr view") printf '%s' "${FAKE_OPEN_PR_BODY:-}" ;;
  "pr create" | "pr edit") cat >> "$FAKE_PR_BODY_LOG" ;;
  "issue create") printf '%s\\n' "${FAKE_CREATED_ISSUE_URL:-}" ;;
  "api --paginate")
    printf '%s' "${FAKE_OPEN_ISSUES:-}"
    exit "${FAKE_GH_API_EXIT:-0}"
    ;;
esac
"""
UPDATE_PR_SUMMARY = (
    "Automated daily dependency update by "
    "`.github/workflows/update-dependencies.yml`. It runs "
    "`./scripts/generate-clients.sh` (the generated clients are not "
    "committed), then `make update`, which runs `uv lock --upgrade` in every "
    "project, so this pull request changes only `uv.lock` files. CI on this "
    "pull request tests the upgraded versions.\n\n"
    "Each run rebuilds `update-dependencies` from `main` and replaces it, so "
    "commits pushed to this branch by hand are lost on the next run."
)
# The summary of update PRs opened before each run rebuilt the branch.
APPENDING_UPDATE_PR_SUMMARY = (
    "Automated daily dependency updates. Subsequent runs append commits to "
    "this branch while the pull request remains open."
)
LEGACY_UPDATE_PR_BODY = (
    "Automated dependency updates\n\n"
    "This PR was automatically created by the dependency update workflow."
)
OPEN_UPDATE_PR_LOOKUP = [
    "gh",
    "pr",
    "list",
    "--repo",
    "PolicyEngine/policyengine-sim-api",
    "--base",
    "main",
    "--head",
    "update-dependencies",
    "--state",
    "open",
    "--json",
    "number,isCrossRepository",
    "--jq",
    "map(select(.isCrossRepository | not)) | .[0].number // empty",
]
OPEN_TRACKING_ISSUE_LOOKUP = [
    "gh",
    "api",
    "--paginate",
    "repos/PolicyEngine/policyengine-sim-api/issues?state=open&per_page=100",
    "--jq",
    '.[] | select(.pull_request == null and .title == "Update dependencies") | .number',
]


def _create_pr(fakes: FakeCommands, tmp_path: Path, **env: str):
    """Run the create-PR script; return its result and the descriptions it set."""
    body_log = tmp_path / "pr-bodies"
    fakes.add("gh", FAKE_GH_FOR_CREATE_PR)
    result = run_script(
        CREATE_PR,
        env=fakes.env(
            GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api",
            FAKE_PR_BODY_LOG=str(body_log),
            **env,
        ),
    )
    # Read bytes: read_text would turn the CRLF line endings under test into LF.
    descriptions = body_log.read_bytes().decode("utf-8") if body_log.exists() else ""
    return result, descriptions


def test_create_pr_opens_the_update_pr_naming_the_open_tracking_issue(fakes, tmp_path):
    result, descriptions = _create_pr(fakes, tmp_path, FAKE_OPEN_ISSUES="870\n")

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        OPEN_UPDATE_PR_LOOKUP,
        OPEN_TRACKING_ISSUE_LOOKUP,
        [
            "gh",
            "pr",
            "create",
            "--repo",
            "PolicyEngine/policyengine-sim-api",
            "--title",
            "Update dependencies",
            "--body-file",
            "-",
            "--base",
            "main",
            "--head",
            "update-dependencies",
        ],
    ]
    assert descriptions == f"Fixes #870\n\n{UPDATE_PR_SUMMARY}\n"


def test_create_pr_names_the_oldest_open_tracking_issue(fakes, tmp_path):
    result, descriptions = _create_pr(
        fakes, tmp_path, FAKE_OPEN_ISSUES="875\n870\n1001\n"
    )

    assert result.returncode == 0, result.stderr
    assert descriptions.startswith("Fixes #870\n\n")


def test_create_pr_opens_a_tracking_issue_when_none_is_open(fakes, tmp_path):
    result, descriptions = _create_pr(
        fakes,
        tmp_path,
        FAKE_CREATED_ISSUE_URL=(
            "https://github.com/PolicyEngine/policyengine-sim-api/issues/871"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls()[2] == [
        "gh",
        "issue",
        "create",
        "--repo",
        "PolicyEngine/policyengine-sim-api",
        "--title",
        "Update dependencies",
        "--body",
        "Tracks the automated daily dependency updates from "
        "update-dependencies.yml. The update pull request names this issue on "
        "the first line of its description and closes it when it merges.",
    ]
    assert fakes.calls()[3][:3] == ["gh", "pr", "create"]
    assert descriptions == f"Fixes #871\n\n{UPDATE_PR_SUMMARY}\n"


def test_create_pr_fails_when_the_new_issue_has_no_number(fakes, tmp_path):
    result, descriptions = _create_pr(
        fakes, tmp_path, FAKE_CREATED_ISSUE_URL="could not create the issue"
    )

    assert result.returncode == 1
    assert "Could not resolve the dependency-update tracking issue" in result.stderr
    assert not any(call[1:3] == ["pr", "create"] for call in fakes.calls())
    assert descriptions == ""


def test_create_pr_fails_when_the_issue_lookup_fails(fakes, tmp_path):
    result, descriptions = _create_pr(fakes, tmp_path, FAKE_GH_API_EXIT="1")

    assert result.returncode == 1
    assert fakes.calls() == [OPEN_UPDATE_PR_LOOKUP, OPEN_TRACKING_ISSUE_LOOKUP]
    assert descriptions == ""


def test_create_pr_reuses_the_open_update_pr_and_rewrites_its_description(
    fakes, tmp_path
):
    """An update PR opened before the rule, such as #583, is kept and rewritten."""
    result, descriptions = _create_pr(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="583",
        FAKE_OPEN_PR_BODY=LEGACY_UPDATE_PR_BODY,
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        OPEN_UPDATE_PR_LOOKUP,
        OPEN_TRACKING_ISSUE_LOOKUP,
        [
            "gh",
            "pr",
            "view",
            "583",
            "--repo",
            "PolicyEngine/policyengine-sim-api",
            "--json",
            "body",
            "--jq",
            ".body",
        ],
        [
            "gh",
            "pr",
            "edit",
            "583",
            "--repo",
            "PolicyEngine/policyengine-sim-api",
            "--body-file",
            "-",
        ],
    ]
    assert descriptions == f"Fixes #870\n\n{UPDATE_PR_SUMMARY}\n"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_create_pr_names_the_open_issue_instead_of_a_closed_one(
    fakes, tmp_path, line_ending
):
    """An open update PR whose issue was closed names the open one instead."""
    result, descriptions = _create_pr(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="900",
        FAKE_OPEN_PR_BODY=f"Fixes #860{line_ending}{line_ending}{UPDATE_PR_SUMMARY}",
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert descriptions == f"Fixes #870\n\n{UPDATE_PR_SUMMARY}\n"


def test_create_pr_rewrites_a_description_that_says_runs_append_commits(
    fakes, tmp_path
):
    result, descriptions = _create_pr(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="900",
        FAKE_OPEN_PR_BODY=f"Fixes #870\n\n{APPENDING_UPDATE_PR_SUMMARY}",
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert descriptions == f"Fixes #870\n\n{UPDATE_PR_SUMMARY}\n"


def test_create_pr_leaves_an_open_update_pr_with_the_current_description(
    fakes, tmp_path
):
    result, descriptions = _create_pr(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="900",
        FAKE_OPEN_PR_BODY=f"Fixes #870\n\n{UPDATE_PR_SUMMARY}",
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        call[1:3] in (["pr", "edit"], ["pr", "create"]) for call in fakes.calls()
    )
    assert descriptions == ""


def test_create_pr_fails_when_gh_fails(fakes):
    fakes.add("gh", "exit 1\n")

    result = run_script(
        CREATE_PR, env=fakes.env(GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api")
    )

    assert result.returncode == 1
    assert fakes.calls() == [OPEN_UPDATE_PR_LOOKUP]


def test_create_pr_requires_the_repository(fakes):
    fakes.add("gh")
    env = fakes.env()
    env.pop("GITHUB_REPOSITORY", None)

    result = run_script(CREATE_PR, env=env)

    assert result.returncode != 0
    assert "GITHUB_REPOSITORY is required" in result.stderr
    assert fakes.calls() == []


def test_update_dependencies_workflow_updates_the_pr_whenever_files_change():
    """The PR step runs even when the branch already held the update."""
    create_pr = _update_dependencies_steps()[f".github/scripts/{CREATE_PR}"]

    assert create_pr["if"] == "steps.push.outputs.changes_detected == 'true'"
    assert create_pr["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}


# --- pr.yml ---------------------------------------------------------------

START_LOCAL_SERVICES = "start-local-services.sh"

FAKE_CURL_READY_AFTER = """\
count=$(($(cat "$FAKE_CURL_COUNT" 2>/dev/null || echo 0) + 1))
echo "$count" > "$FAKE_CURL_COUNT"
[[ "$count" -ge "${FAKE_CURL_READY_AT:-999}" ]]
"""


def test_pr_workflow_starts_local_services_with_the_tested_script():
    workflow = (WORKFLOWS_DIR / "pr.yml").read_text(encoding="utf-8")

    assert (
        f"run: .github/scripts/{START_LOCAL_SERVICES} deployment/docker-compose.yml"
        in workflow
    )


def _start_local_services(fakes: FakeCommands, tmp_path: Path, *args: str, **env):
    fakes.add("docker", 'exit "${FAKE_DOCKER_EXIT:-0}"\n')
    fakes.add("curl", FAKE_CURL_READY_AFTER)
    fakes.add("sleep")
    return run_script(
        START_LOCAL_SERVICES,
        *args,
        env=fakes.env(FAKE_CURL_COUNT=str(tmp_path / "curl-count"), **env),
    )


def test_start_local_services_waits_until_the_gateway_answers(fakes, tmp_path):
    result = _start_local_services(
        fakes, tmp_path, "deployment/docker-compose.yml", FAKE_CURL_READY_AT="3"
    )

    assert result.returncode == 0, result.stderr
    assert "Waiting for services... (attempt 2/30)" in result.stdout
    assert "Services are ready!" in result.stdout
    alive = ["curl", "-f", "http://localhost:8082/ping/alive"]
    assert fakes.calls() == [
        ["docker", "compose", "-f", "deployment/docker-compose.yml", "up", "-d"],
        ["sleep", "10"],
        alive,
        ["sleep", "2"],
        alive,
        ["sleep", "2"],
        alive,
    ]


def test_start_local_services_gives_up_quietly_after_thirty_attempts(fakes, tmp_path):
    """Unchanged from the inline step: the integration tests report the outage."""
    result = _start_local_services(fakes, tmp_path, "deployment/docker-compose.yml")

    assert result.returncode == 0, result.stderr
    assert "Waiting for services... (attempt 30/30)" in result.stdout
    assert "Services are ready!" not in result.stdout
    assert len(fakes.calls("curl")) == 30
    assert len(fakes.calls("sleep")) == 31


def test_start_local_services_fails_when_compose_fails(fakes, tmp_path):
    result = _start_local_services(
        fakes, tmp_path, "deployment/docker-compose.yml", FAKE_DOCKER_EXIT="1"
    )

    assert result.returncode == 1
    assert fakes.calls("curl") == []


def test_start_local_services_requires_a_compose_file(fakes, tmp_path):
    result = _start_local_services(fakes, tmp_path)

    assert result.returncode != 0
    assert "Docker Compose file required" in result.stderr
    assert fakes.calls() == []


# --- publish-clients.yml --------------------------------------------------

BUILD_CLIENT = "build-simulation-api-client.sh"
CLIENT_DIR = "projects/policyengine-simulation-entry/artifacts/clients/python"
CLIENT_PYPROJECT = """\
[project]
name = "policyengine_api_simulation_client"
version = "0.1.0"
description = "A client library for accessing PolicyEngine simulation API"
"""


def test_publish_workflow_builds_the_client_with_the_tested_script():
    workflow = (WORKFLOWS_DIR / "publish-clients.yml").read_text(encoding="utf-8")

    assert f"run: .github/scripts/{BUILD_CLIENT} {CLIENT_DIR}" in workflow
    assert "RUN_NUMBER: ${{ github.run_number }}" in workflow
    assert f"packages-dir: {CLIENT_DIR}/dist/" in workflow


def _build_client(fakes: FakeCommands, tmp_path: Path, *args: str, **env):
    client = tmp_path / "client"
    client.mkdir()
    (client / "pyproject.toml").write_text(CLIENT_PYPROJECT, encoding="utf-8")
    fakes.add("date", "echo 20261013\n")
    fakes.add("uv", 'pwd > "$FAKE_UV_CWD"\nexit "${FAKE_UV_EXIT:-0}"\n')
    result = run_script(
        BUILD_CLIENT,
        *(args or (str(client),)),
        env=fakes.env(FAKE_UV_CWD=str(tmp_path / "uv-cwd"), **env),
    )
    return result, client


def test_build_client_versions_the_client_by_date_and_run_number(fakes, tmp_path):
    result, client = _build_client(fakes, tmp_path, RUN_NUMBER="57")

    assert result.returncode == 0, result.stderr
    assert (client / "pyproject.toml").read_text(
        encoding="utf-8"
    ) == CLIENT_PYPROJECT.replace('version = "0.1.0"', 'version = "0.20261013.57"')
    assert sorted(path.name for path in client.iterdir()) == ["pyproject.toml"]
    assert fakes.calls() == [["date", "+%Y%m%d"], ["uv", "build"]]
    uv_cwd = (tmp_path / "uv-cwd").read_text(encoding="utf-8").strip()
    assert Path(uv_cwd).resolve() == client.resolve()


def test_build_client_fails_when_the_build_fails(fakes, tmp_path):
    result, _ = _build_client(fakes, tmp_path, RUN_NUMBER="57", FAKE_UV_EXIT="1")

    assert result.returncode == 1


def test_build_client_requires_the_run_number(fakes, tmp_path):
    env = fakes.env()
    env.pop("RUN_NUMBER", None)
    client = tmp_path / "client"
    client.mkdir()
    (client / "pyproject.toml").write_text(CLIENT_PYPROJECT, encoding="utf-8")
    fakes.add("uv")

    result = run_script(BUILD_CLIENT, str(client), env=env)

    assert result.returncode != 0
    assert "RUN_NUMBER is required" in result.stderr
    assert (client / "pyproject.toml").read_text(encoding="utf-8") == CLIENT_PYPROJECT
    assert fakes.calls() == []


def test_build_client_fails_without_the_generated_client(fakes, tmp_path):
    result, _ = _build_client(
        fakes, tmp_path, str(tmp_path / "missing"), RUN_NUMBER="57"
    )

    assert result.returncode != 0
    assert fakes.calls("uv") == []
