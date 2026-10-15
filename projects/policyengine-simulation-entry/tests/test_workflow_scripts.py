"""Tests for the workflow scripts under .github/scripts/ that no deployment
test covers: each runs the script with fake commands first on PATH."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
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

FAKE_GH_PR_LIST = """\
if [[ "$1 $2" == "pr list" ]]; then
  printf '%s' "${FAKE_OPEN_PR:-}"
  exit "${FAKE_GH_EXIT:-0}"
fi
exit "${FAKE_GH_EXIT:-0}"
"""

FAKE_GIT = """\
case "$1" in
  status) printf '%s' "${FAKE_GIT_STATUS:-}" ;;
  rev-list) printf '%s\\n' "${FAKE_COMMITS_TO_PUSH:-0}" ;;
  push) exit "${FAKE_GIT_PUSH_EXIT:-0}" ;;
esac
"""


def test_update_dependencies_workflow_runs_the_tested_scripts():
    workflow = (WORKFLOWS_DIR / "update-dependencies.yml").read_text(encoding="utf-8")

    for script in (PREPARE_BRANCH, COMMIT_AND_PUSH, CREATE_PR):
        assert f"run: .github/scripts/{script}" in workflow


def _prepare_branch(fakes: FakeCommands, tmp_path: Path, **env: str):
    output = tmp_path / "github-output"
    fakes.add("gh", FAKE_GH_PR_LIST)
    fakes.add("git")
    result = run_script(
        PREPARE_BRANCH,
        env=fakes.env(
            GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api",
            GITHUB_OUTPUT=str(output),
            **env,
        ),
    )
    return result, output


def test_prepare_branch_reuses_the_branch_of_an_open_update_pr(fakes, tmp_path):
    result, output = _prepare_branch(fakes, tmp_path, FAKE_OPEN_PR="42")

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {"has_open_pr": "true"}
    assert fakes.calls("gh") == [
        [
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
            "number",
            "--jq",
            ".[0].number // empty",
        ]
    ]
    assert fakes.calls("git") == [
        ["git", "config", "user.name", "policyengine-auto"],
        [
            "git",
            "config",
            "user.email",
            "policyengine-auto@users.noreply.github.com",
        ],
        ["git", "fetch", "origin", "update-dependencies"],
        [
            "git",
            "checkout",
            "-B",
            "update-dependencies",
            "origin/update-dependencies",
        ],
        ["git", "merge", "--no-edit", "-X", "theirs", "origin/main"],
    ]


def test_prepare_branch_starts_from_main_without_an_open_update_pr(fakes, tmp_path):
    result, output = _prepare_branch(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {"has_open_pr": "false"}
    assert fakes.calls("git")[2:] == [
        ["git", "checkout", "-B", "update-dependencies", "origin/main"]
    ]


def test_prepare_branch_fails_when_the_pr_lookup_fails(fakes, tmp_path):
    result, output = _prepare_branch(fakes, tmp_path, FAKE_GH_EXIT="1")

    assert result.returncode != 0
    assert github_output(output) == {}
    assert not any(call[1] == "checkout" for call in fakes.calls("git"))


def test_prepare_branch_requires_the_repository(fakes, tmp_path):
    fakes.add("gh", FAKE_GH_PR_LIST)
    fakes.add("git")
    env = fakes.env(GITHUB_OUTPUT=str(tmp_path / "github-output"))
    env.pop("GITHUB_REPOSITORY", None)

    result = run_script(PREPARE_BRANCH, env=env)

    assert result.returncode != 0
    assert "GITHUB_REPOSITORY is required" in result.stderr
    assert fakes.calls() == []


def _commit_and_push(fakes: FakeCommands, tmp_path: Path, has_open_pr: str, **env):
    output = tmp_path / "github-output"
    fakes.add("git", FAKE_GIT)
    fakes.add("date", "echo 2026-10-13\n")
    result = run_script(
        COMMIT_AND_PUSH,
        env=fakes.env(GITHUB_OUTPUT=str(output), HAS_OPEN_PR=has_open_pr, **env),
    )
    return result, output


def test_commit_and_push_replaces_the_branch_when_no_pr_owns_it(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes, tmp_path, "false", FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert "Changes detected" in result.stdout
    assert github_output(output) == {"branch_pushed": "true"}
    assert fakes.calls("git") == [
        ["git", "status", "--porcelain"],
        ["git", "add", "--all"],
        ["git", "commit", "-m", "Update dependencies (2026-10-13)"],
        [
            "git",
            "push",
            "--force-with-lease",
            "origin",
            "HEAD:update-dependencies",
        ],
    ]
    assert fakes.calls("date") == [["date", "-u", "+%Y-%m-%d"]]


def test_commit_and_push_appends_to_the_branch_of_an_open_pr(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes,
        tmp_path,
        "true",
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_COMMITS_TO_PUSH="1",
    )

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {"branch_pushed": "true"}
    assert fakes.calls("git")[-2:] == [
        ["git", "rev-list", "--count", "origin/update-dependencies..HEAD"],
        ["git", "push", "origin", "HEAD:update-dependencies"],
    ]


def test_commit_and_push_pushes_earlier_commits_to_an_open_pr(fakes, tmp_path):
    """A merge from main can leave commits to push even without new changes."""
    result, output = _commit_and_push(fakes, tmp_path, "true", FAKE_COMMITS_TO_PUSH="2")

    assert result.returncode == 0, result.stderr
    assert "No changes detected" in result.stdout
    assert github_output(output) == {"branch_pushed": "true"}
    assert ["git", "push", "origin", "HEAD:update-dependencies"] in fakes.calls("git")
    assert not any(call[1] == "commit" for call in fakes.calls("git"))


@pytest.mark.parametrize("has_open_pr", ["true", "false"])
def test_commit_and_push_skips_the_push_when_nothing_is_new(
    fakes, tmp_path, has_open_pr
):
    result, output = _commit_and_push(fakes, tmp_path, has_open_pr)

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {"branch_pushed": "false"}
    assert not any(call[1] in ("commit", "push") for call in fakes.calls("git"))


def test_commit_and_push_rejects_an_invalid_open_pr_flag(fakes, tmp_path):
    result, output = _commit_and_push(fakes, tmp_path, "yes")

    assert result.returncode == 1
    assert "HAS_OPEN_PR must be either true or false" in result.stderr
    assert github_output(output) == {}
    assert fakes.calls() == []


def test_commit_and_push_fails_when_the_push_fails(fakes, tmp_path):
    result, output = _commit_and_push(
        fakes,
        tmp_path,
        "false",
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_GIT_PUSH_EXIT="1",
    )

    assert result.returncode != 0
    assert github_output(output) == {}


def test_create_pr_opens_the_update_pr(fakes, tmp_path):
    fakes.add("gh")

    result = run_script(
        CREATE_PR, env=fakes.env(GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api")
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [
            "gh",
            "pr",
            "create",
            "--repo",
            "PolicyEngine/policyengine-sim-api",
            "--title",
            "Update dependencies",
            "--body",
            "Automated daily dependency updates. Subsequent runs append commits "
            "to this branch while the pull request remains open.",
            "--base",
            "main",
            "--head",
            "update-dependencies",
        ]
    ]


def test_create_pr_fails_when_gh_fails(fakes, tmp_path):
    fakes.add("gh", "exit 1\n")

    result = run_script(
        CREATE_PR, env=fakes.env(GITHUB_REPOSITORY="PolicyEngine/policyengine-sim-api")
    )

    assert result.returncode == 1


def test_create_pr_requires_the_repository(fakes, tmp_path):
    fakes.add("gh")
    env = fakes.env()
    env.pop("GITHUB_REPOSITORY", None)

    result = run_script(CREATE_PR, env=env)

    assert result.returncode != 0
    assert "GITHUB_REPOSITORY is required" in result.stderr
    assert fakes.calls() == []


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
