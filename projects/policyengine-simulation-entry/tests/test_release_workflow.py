"""Tests for how a merge to main is released, deployed once, and checked first.

Rule sim.deploy.release-commit-deploys-once. A push to main starts
simulation-deploy.yml. For a merge, only the versioning job runs: it raises
VERSION, builds CHANGELOG.md and pushes one release commit with a fixed
message, using a GitHub App token, so that push starts the workflow again.
For the release commit, only the deploy jobs run, and the versioning job is
skipped, so nothing is released twice and nothing loops. A manual dispatch
deploys without a release. Client publication follows only the release
commit's run, which is the one that deployed.

The workflow tests evaluate each job's ``if`` with the GitHub Actions
expression reader in test_deployment_assets_support, following ``needs``, so
they test what the conditions decide rather than their text.
commit-release.sh runs against a real local remote. The last tests check
that pr.yml runs the changelog fragment check (rule
sim.process.changelog-fragment) on every pull request CI runs on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from changelog_release_support import (
    GIT_ENVIRONMENT,
    commit_all,
    git,
    write_files,
)
from test_deployment_assets_support import evaluate_github_expression
from workflow_script_support import REPOSITORY_ROOT, WORKFLOWS_DIR, run_script

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

DEPLOY_WORKFLOW = WORKFLOWS_DIR / "simulation-deploy.yml"
PUBLISH_WORKFLOW = WORKFLOWS_DIR / "publish-clients.yml"
PR_WORKFLOW = WORKFLOWS_DIR / "pr.yml"
REUSABLE_DEPLOY = "./.github/workflows/simulation-deploy.reusable.yml"
BUILD_CHANGELOG = ".github/scripts/build-changelog.sh"
COMMIT_RELEASE = ".github/scripts/commit-release.sh"
RELEASE_MESSAGE = "Update PolicyEngine simulation API"
PROOF_FIXTURES = (
    REPOSITORY_ROOT / "rules" / "fixtures" / "sim.deploy.release-commit-deploys-once"
)
# The head commit message of each way a pull request reaches main.
MERGE_MESSAGES = {
    "merge commit": (
        "Merge pull request #982 from PolicyEngine/towncrier-changelog\n\n"
        "Move the changelog to Towncrier fragments"
    ),
    "squash merge": "Move the changelog to Towncrier fragments (#982)",
    "rebase merge": "Build CHANGELOG.md when a merge to main is released",
    # GitHub adds the pull request number to a squash merge's title.
    "squash merge titled like the release": f"{RELEASE_MESSAGE} (#982)",
}
FULL_CI_CONDITION = (
    "${{ !github.event.pull_request.stack || "
    "github.event.pull_request.stack.base.ref == github.event.pull_request.base.ref }}"
)
# A release commit must never start another; more pushes than this is a loop.
MAX_PUSHES_PER_MERGE = 5


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _push(message: str) -> dict:
    return {
        "github": {"event_name": "push", "event": {"head_commit": {"message": message}}}
    }


def _dispatch() -> dict:
    return {"github": {"event_name": "workflow_dispatch", "event": {"inputs": {}}}}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def jobs_that_run(workflow: dict, context: dict) -> set[str]:
    """Return the jobs of ``workflow`` that run for an event, in GitHub's rules.

    A job runs when its ``if`` holds, or ``success()`` when it has none, given
    the results of the jobs it needs: success for a job that ran, skipped for
    one that didn't.
    """
    jobs = workflow["jobs"]
    results: dict[str, str] = {}
    while len(results) < len(jobs):
        ready = [
            job_id
            for job_id, job in jobs.items()
            if job_id not in results and all(need in results for need in _needs(job))
        ]
        assert ready, f"jobs whose needs never run: {sorted(set(jobs) - set(results))}"
        for job_id in ready:
            job = jobs[job_id]
            needs = {need: {"result": results[need]} for need in _needs(job)}
            condition = str(job.get("if", "${{ success() }}"))
            runs = evaluate_github_expression(
                condition, {**context, "needs": needs}, job_if=True
            )
            results[job_id] = "success" if runs else "skipped"
    return {job_id for job_id, result in results.items() if result == "success"}


def _deploy_jobs(workflow: dict) -> set[str]:
    return {
        job_id
        for job_id, job in workflow["jobs"].items()
        if job.get("uses") == REUSABLE_DEPLOY
    }


def _release_jobs(workflow: dict) -> set[str]:
    return {
        job_id
        for job_id, job in workflow["jobs"].items()
        if any(step.get("run") == COMMIT_RELEASE for step in job.get("steps") or [])
    }


def release_commit_message(workflow: dict) -> str | None:
    """Return the message the release job's commit-release.sh step commits with."""
    for job_id in sorted(_release_jobs(workflow)):
        for step in workflow["jobs"][job_id]["steps"]:
            if step.get("run") == COMMIT_RELEASE:
                return (step.get("env") or {}).get("RELEASE_COMMIT_MESSAGE")
    return None


def _event_violations(
    workflow: dict, event: str, context: dict, *, releases: bool, deploys: bool
) -> list[str]:
    """Return how the jobs that run for ``event`` differ from what it should run."""
    runs = jobs_that_run(workflow, context)
    released = bool(_release_jobs(workflow) & runs)
    deployed = sorted(_deploy_jobs(workflow) & runs)
    missing = sorted(_deploy_jobs(workflow) - runs)
    violations = []
    if releases and not released:
        violations.append(f"{event} builds no release")
    if not releases and released:
        violations.append(f"{event} builds a release")
    if deploys and missing:
        violations.append(f"{event} does not run {', '.join(missing)}")
    if not deploys and deployed:
        violations.append(
            f"{event} deploys before its release commit: {', '.join(deployed)}"
        )
    return violations


def release_violations(workflow: dict) -> list[str]:
    """Return every way a push to main could deploy unreleased, twice or in a loop."""
    message = release_commit_message(workflow)
    violations = []
    if not _release_jobs(workflow):
        violations.append(f"no job commits a release with {COMMIT_RELEASE}")
    elif not message:
        violations.append("the release commit's RELEASE_COMMIT_MESSAGE is not set")
    for kind, merge_message in MERGE_MESSAGES.items():
        violations += _event_violations(
            workflow,
            f"a {kind} to main",
            _push(merge_message),
            releases=True,
            deploys=False,
        )
    if message:
        violations += _event_violations(
            workflow, "the release commit", _push(message), releases=False, deploys=True
        )
    violations += _event_violations(
        workflow, "a manual dispatch", _dispatch(), releases=False, deploys=True
    )
    return violations


# --- simulation-deploy.yml ------------------------------------------------------


def test_the_deploy_workflow_releases_each_merge_before_deploying_it():
    assert release_violations(_workflow(DEPLOY_WORKFLOW)) == []


def test_the_release_commit_message_is_fixed():
    assert release_commit_message(_workflow(DEPLOY_WORKFLOW)) == RELEASE_MESSAGE


@pytest.mark.parametrize("kind", sorted(MERGE_MESSAGES))
def test_a_merge_to_main_only_builds_its_release(kind):
    workflow = _workflow(DEPLOY_WORKFLOW)

    assert jobs_that_run(workflow, _push(MERGE_MESSAGES[kind])) == {"versioning"}


def test_the_release_commit_only_deploys():
    workflow = _workflow(DEPLOY_WORKFLOW)

    assert jobs_that_run(workflow, _push(RELEASE_MESSAGE)) == {
        "beta",
        "prod",
        "summary",
    }


def test_a_manual_dispatch_deploys_without_a_release():
    workflow = _workflow(DEPLOY_WORKFLOW)

    assert jobs_that_run(workflow, _dispatch()) == {"beta", "prod", "summary"}


def test_each_merge_deploys_once_and_its_release_commit_starts_nothing():
    """Follow the pushes one merge causes until none is left."""
    workflow = _workflow(DEPLOY_WORKFLOW)
    message = release_commit_message(workflow)
    pushes = [MERGE_MESSAGES["merge commit"]]
    handled = []
    deploys = 0
    while pushes:
        assert len(handled) < MAX_PUSHES_PER_MERGE, f"the pushes loop: {handled}"
        head_commit_message = pushes.pop(0)
        handled.append(head_commit_message)
        runs = jobs_that_run(workflow, _push(head_commit_message))
        if _release_jobs(workflow) & runs:
            pushes.append(message)
        if _deploy_jobs(workflow) <= runs:
            deploys += 1

    assert handled == [MERGE_MESSAGES["merge commit"], RELEASE_MESSAGE]
    assert deploys == 1


def test_the_versioning_job_pushes_its_release_with_the_app_token():
    """A push made with the job's own token would start no deploy run.

    The GitHub App's token starts one, and the App may push to main past the
    branch ruleset that requires pull requests.
    """
    job = _workflow(DEPLOY_WORKFLOW)["jobs"]["versioning"]
    steps = job["steps"]
    token_step, checkout_step = steps[0], steps[1]

    assert job["permissions"] == {"contents": "read"}
    assert token_step["uses"] == "actions/create-github-app-token@v3"
    assert token_step["with"] == {
        "app-id": "${{ secrets.APP_ID }}",
        "private-key": "${{ secrets.APP_PRIVATE_KEY }}",
        "permission-contents": "write",
    }
    assert checkout_step["uses"] == "actions/checkout@v6"
    assert checkout_step["with"] == {
        "token": f"${{{{ steps.{token_step['id']}.outputs.token }}}}"
    }
    runs = [step["run"] for step in steps if "run" in step]
    assert runs == [BUILD_CHANGELOG, COMMIT_RELEASE]


def test_the_versioning_job_runs_on_the_python_and_uv_ci_pins():
    steps = _workflow(DEPLOY_WORKFLOW)["jobs"]["versioning"]["steps"]
    setup = {step["uses"]: step.get("with") for step in steps if "uses" in step}

    assert setup["actions/setup-python@v6"] == {"python-version": "3.13"}
    assert setup["astral-sh/setup-uv@v8.1.0"] == {"version": "0.13.6"}


def test_rule_proof_release_commit_deploys_once():
    """The workflow before releases deployed every push; the new one deploys once."""
    bad = yaml.safe_load(
        (PROOF_FIXTURES / "bad_simulation-deploy.yml").read_text(encoding="utf-8")
    )
    good = yaml.safe_load(
        (PROOF_FIXTURES / "good_simulation-deploy.yml").read_text(encoding="utf-8")
    )

    assert release_violations(bad) == [
        f"no job commits a release with {COMMIT_RELEASE}",
        "a merge commit to main builds no release",
        "a merge commit to main deploys before its release commit: beta, prod",
        "a squash merge to main builds no release",
        "a squash merge to main deploys before its release commit: beta, prod",
        "a rebase merge to main builds no release",
        "a rebase merge to main deploys before its release commit: beta, prod",
        "a squash merge titled like the release to main builds no release",
        (
            "a squash merge titled like the release to main deploys before its "
            "release commit: beta, prod"
        ),
    ]
    assert release_violations(good) == []


# --- publish-clients.yml --------------------------------------------------------


def _publication_runs(conclusion: str, event: str, message: str) -> bool:
    job = _workflow(PUBLISH_WORKFLOW)["jobs"]["publish"]
    context = {
        "github": {
            "event": {
                "workflow_run": {
                    "conclusion": conclusion,
                    "event": event,
                    "head_commit": {"message": message},
                }
            }
        }
    }
    return bool(evaluate_github_expression(job["if"], context, job_if=True))


def test_clients_publish_after_the_release_commit_deploys():
    assert _publication_runs("success", "push", RELEASE_MESSAGE)


@pytest.mark.parametrize(
    ("conclusion", "event", "message"),
    [
        # The merge's run only built the release; nothing deployed.
        ("success", "push", MERGE_MESSAGES["merge commit"]),
        ("success", "push", MERGE_MESSAGES["squash merge"]),
        ("failure", "push", RELEASE_MESSAGE),
        ("cancelled", "push", RELEASE_MESSAGE),
        # A manual deployment never published clients, and still doesn't.
        ("success", "workflow_dispatch", RELEASE_MESSAGE),
    ],
)
def test_clients_publish_after_no_other_run(conclusion, event, message):
    assert not _publication_runs(conclusion, event, message)


# --- commit-release.sh ----------------------------------------------------------


def _released_repository(tmp_path: Path) -> tuple[Path, Path]:
    """Return a clone of a remote main, changed as build-changelog.sh changes it.

    Return the clone and the bare remote it pushes to.
    """
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "--quiet", str(remote), str(seed))
    write_files(
        seed,
        {
            "VERSION": "0.1.0\n",
            "CHANGELOG.md": "# Changelog\n\n<!-- towncrier release notes start -->\n",
            "README.md": "Readme.\n",
            "changelog.d/.gitkeep": "",
            "changelog.d/982.added.md": "Built CHANGELOG.md at each release.\n",
        },
    )
    commit_all(seed, "Merge pull request #982 from PolicyEngine/towncrier-changelog")
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(remote), str(clone))
    write_files(
        clone,
        {
            "VERSION": "0.2.0\n",
            "CHANGELOG.md": (
                "# Changelog\n\n<!-- towncrier release notes start -->\n\n"
                "## [0.2.0] - 2026-10-28\n\n### Added\n\n"
                "- Built CHANGELOG.md at each release.\n"
            ),
            # An untracked file the build left behind; no release commits it.
            "stray-build-output.txt": "not part of the release\n",
        },
    )
    git(clone, "add", "CHANGELOG.md")
    git(clone, "rm", "--quiet", "changelog.d/982.added.md")
    return clone, remote


def _commit_release(
    clone: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    return run_script(
        "commit-release.sh",
        env={
            **GIT_ENVIRONMENT,
            "RELEASE_COMMIT_MESSAGE": RELEASE_MESSAGE,
            **environment,
        },
        cwd=clone,
    )


def _remote_main(remote: Path) -> str:
    return git(remote, "rev-parse", "refs/heads/main")


def test_commit_release_pushes_one_release_commit_to_main(tmp_path):
    clone, remote = _released_repository(tmp_path)
    merged = git(clone, "rev-parse", "HEAD")

    result = _commit_release(clone)

    assert result.returncode == 0, result.stdout + result.stderr
    released = _remote_main(remote)
    assert git(remote, "rev-parse", f"{released}^") == merged
    assert git(remote, "log", "-1", "--format=%B", released) == RELEASE_MESSAGE
    assert git(remote, "log", "-1", "--format=%an <%ae>", released) == (
        "github-actions[bot] <github-actions[bot]@users.noreply.github.com>"
    )
    assert git(
        remote, "diff-tree", "--no-commit-id", "--name-status", "-r", released
    ).splitlines() == [
        "M\tCHANGELOG.md",
        "M\tVERSION",
        "D\tchangelog.d/982.added.md",
    ]
    assert f"Pushed release commit {released} to main" in result.stdout


def test_commit_release_leaves_the_release_to_a_newer_merge(tmp_path):
    """Another merge's run waits for this one, and releases both merges' fragments."""
    clone, remote = _released_repository(tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "clone", "--quiet", str(remote), str(other))
    write_files(other, {"changelog.d/983.fixed.md": "Fixed another thing.\n"})
    newer_merge = commit_all(other, "Merge pull request #983")
    git(other, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    result = _commit_release(clone)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_main(remote) == newer_merge
    assert f"::notice::main moved on to {newer_merge}" in result.stdout


def test_commit_release_fails_when_main_no_longer_holds_the_merge(tmp_path):
    clone, remote = _released_repository(tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "init", "--quiet", "--initial-branch", "main", str(other))
    write_files(other, {"README.md": "Unrelated history.\n"})
    unrelated = commit_all(other, "Unrelated")
    git(other, "push", "--quiet", "--force", str(remote), "HEAD:refs/heads/main")

    result = _commit_release(clone)

    assert result.returncode == 1
    assert _remote_main(remote) == unrelated
    assert "::error::Could not push the release commit to main" in result.stdout


def test_commit_release_refuses_a_change_outside_the_release(tmp_path):
    clone, remote = _released_repository(tmp_path)
    before = _remote_main(remote)
    write_files(clone, {"README.md": "Changed by the build.\n"})

    result = _commit_release(clone)

    assert result.returncode == 1
    assert "README.md" in result.stdout
    assert _remote_main(remote) == before


def test_commit_release_refuses_an_empty_release(tmp_path):
    clone, remote = _released_repository(tmp_path)
    git(clone, "reset", "--quiet", "--hard")
    before = _remote_main(remote)

    result = _commit_release(clone)

    assert result.returncode == 1
    assert "::error::Nothing to release" in result.stdout
    assert _remote_main(remote) == before


def test_commit_release_requires_the_message(tmp_path):
    clone, _remote = _released_repository(tmp_path)

    result = _commit_release(clone, RELEASE_COMMIT_MESSAGE="")

    assert result.returncode != 0
    assert "RELEASE_COMMIT_MESSAGE is required" in result.stderr


# --- pr.yml ---------------------------------------------------------------------


def test_every_full_ci_run_checks_the_changelog_fragment():
    jobs = _workflow(PR_WORKFLOW)["jobs"]
    job = jobs["check-changelog"]

    assert job["if"] == FULL_CI_CONDITION
    assert "needs" not in job
    assert "check-changelog" in jobs["ci-complete"]["needs"]
    assert job["permissions"] == {"contents": "read"}


def test_the_fragment_check_compares_the_merged_result_with_its_base():
    steps = _workflow(PR_WORKFLOW)["jobs"]["check-changelog"]["steps"]
    checkout, python, check = steps

    assert checkout["uses"] == "actions/checkout@v6"
    # The merge base needs the base branch's history.
    assert checkout["with"] == {"fetch-depth": 0}
    assert python == {
        "name": "Set up Python",
        "uses": "actions/setup-python@v6",
        "with": {"python-version": "3.13"},
    }
    assert check == {
        "name": "Require a changelog fragment",
        "env": {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"},
        "run": ".github/scripts/check-changelog-fragment.sh",
    }
