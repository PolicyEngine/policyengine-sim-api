"""The manual Simulation API rollback workflow and its input validation.

Invariants:
- no rollback input is interpolated into shell source; each reaches the
  scripts through the environment and is validated before anything changes;
- rollbacks share the deployments' concurrency group, so one never races a
  release;
- exactly one restore mode is given: a deployment run whose kept routing plan
  is restored, a published version to point latest at, or journal recovery;
- after restoring, the workflow verifies the stable service serves the whole
  identity latest now routes to (version, app, UK model and dataset digest).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets_support import step_shell

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ROLLBACK_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-rollback.yml"
)
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
VALIDATOR = (
    REPOSITORY_ROOT / ".github" / "scripts" / "validate-simulation-rollback-inputs.sh"
)
RESTORE_ROUTING_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "rollback-restore-routing.sh"
)


def _run_bash(
    *arguments: str | Path,
    check: bool,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run bash on a repository rollback script or step, with text output."""
    return subprocess.run(
        ["bash", *arguments],
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
    )


def _validate(**env: str) -> subprocess.CompletedProcess[str]:
    return _run_bash(
        VALIDATOR,
        check=False,
        capture_output=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_valid_rollbacks_pass_validation():
    result = _validate(
        TARGET="prod",
        POLICYENGINE_VERSION="6.2.6",
        EXPECTED_LATEST="6.3.0",
        ENTRYPOINT_REVISION="policyengine-simulation-entry-00042-abc",
    )
    assert result.returncode == 0, result.stderr
    assert "Rolling prod back to policyengine 6.2.6, entrypoint" in result.stdout
    assert _validate(TARGET="beta", POLICYENGINE_VERSION="6.2.6rc1").returncode == 0
    plan = _validate(TARGET="beta", DEPLOY_RUN_ID="18273645")
    assert plan.returncode == 0, plan.stderr
    assert "Undoing the routing change of beta deployment run 18273645" in plan.stdout


@pytest.mark.parametrize("target", ["beta", "prod"])
def test_operator_recovery_accepts_only_target_and_pending_resolution(target):
    result = _validate(TARGET=target, RESOLVE_PENDING_PUBLICATION="true")
    assert result.returncode == 0, result.stderr
    assert f"Resolving the unfinished {target} routing publication" in result.stdout


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEPLOY_RUN_ID", "744001"),
        ("POLICYENGINE_VERSION", "6.3.0"),
        ("EXPECTED_LATEST", "6.3.0"),
        ("ENTRYPOINT_REVISION", "policyengine-simulation-entry-00042-abc"),
    ],
)
def test_operator_recovery_refuses_any_other_rollback_input(name, value):
    result = _validate(
        TARGET="prod", RESOLVE_PENDING_PUBLICATION="true", **{name: value}
    )
    assert result.returncode == 2
    assert "requires all other rollback inputs blank" in result.stderr


@pytest.mark.parametrize("value", ["yes", "TRUE", "1", "$(id)", "true;false"])
def test_operator_recovery_refuses_invalid_boolean_input(value):
    result = _validate(TARGET="prod", RESOLVE_PENDING_PUBLICATION=value)
    assert result.returncode == 2
    assert "resolve_pending_publication must be true or false" in result.stderr


def test_explicitly_disabled_pending_resolution_keeps_legacy_validation():
    result = _validate(TARGET="prod", RESOLVE_PENDING_PUBLICATION="false")
    assert result.returncode == 2
    assert "give deploy_run_id or policyengine_version" in result.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"TARGET": "staging", "POLICYENGINE_VERSION": "6.2.6"},
        {"TARGET": "prod"},
        {"TARGET": "prod", "POLICYENGINE_VERSION": ""},
        {"TARGET": "prod", "POLICYENGINE_VERSION": "6.2.6; curl evil"},
        {"TARGET": "prod", "POLICYENGINE_VERSION": "$(id)"},
        {
            "TARGET": "prod",
            "POLICYENGINE_VERSION": "6.2.6",
            "EXPECTED_LATEST": "latest",
        },
        {
            "TARGET": "prod",
            "POLICYENGINE_VERSION": "6.2.6",
            "ENTRYPOINT_REVISION": "Rev; rm -rf /",
        },
        {"TARGET": "prod", "DEPLOY_RUN_ID": "123", "POLICYENGINE_VERSION": "6.2.6"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "12x"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "0"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "123", "EXPECTED_LATEST": "6.2.6"},
    ],
)
def test_invalid_rollbacks_are_refused_before_any_change(env):
    result = _validate(**env)
    assert result.returncode == 2
    assert "Rollback input error" in result.stderr


def test_rollback_inputs_never_reach_shell_source():
    workflow = ROLLBACK_WORKFLOW.read_text(encoding="utf-8")
    run_blocks = re.findall(r"run: \|\n((?:          .*\n|\n)+)", workflow)
    run_blocks += re.findall(r"run: ([^|\n].*)\n", workflow)
    assert run_blocks
    for block in run_blocks:
        assert "${{ inputs." not in block, block


def _rollback_steps() -> list[dict]:
    return _rollback_workflow()["jobs"]["rollback"]["steps"]


def _rollback_workflow() -> dict:
    import yaml

    return yaml.safe_load(ROLLBACK_WORKFLOW.read_text(encoding="utf-8"))


def _step_index(steps, fragment):
    """Return the first step whose shell (with its scripts) or uses: holds fragment."""
    return next(
        index
        for index, step in enumerate(steps)
        if fragment in step_shell(step) or fragment in step.get("uses", "")
    )


def test_rollback_validates_first_and_verifies_last():
    steps = _rollback_steps()
    validate = _step_index(steps, "validate-simulation-rollback-inputs.sh")
    restore = _step_index(steps, "--restore-plan")
    assert restore == _step_index(steps, "--restore-latest")  # one step, two modes
    identity = _step_index(steps, "--print-identity")
    traffic = _step_index(steps, "set-cloud-run-simulation-entry-revision.sh")
    verify = _step_index(steps, "cloud-run-simulation-entry-smoke.sh")
    assert validate < restore < identity < verify
    assert validate < traffic < verify
    download = steps[_step_index(steps, "actions/download-artifact@")]
    assert download["with"]["run-id"] == "${{ inputs.deploy_run_id }}"
    # Each attempt keeps a routes plan and, once latest moved, a latest plan.
    assert download["with"]["pattern"] == "routing-*plan-${{ inputs.target }}-*"
    assert validate < steps.index(download) < restore


def test_operator_recovery_prints_the_journal_then_restores_without_artifacts():
    workflow = _rollback_workflow()
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["resolve_pending_publication"]["type"] == "boolean"
    assert inputs["resolve_pending_publication"]["default"] is False
    job = workflow["jobs"]["rollback"]
    assert job["env"]["RESOLVE_PENDING_PUBLICATION"] == (
        "${{ inputs.resolve_pending_publication }}"
    )
    steps = job["steps"]
    validate = _step_index(steps, "validate-simulation-rollback-inputs.sh")
    recover = _step_index(steps, "--finalize-publication")
    assert validate < recover
    step = steps[recover]
    assert step["if"] == "${{ inputs.resolve_pending_publication }}"
    # test_simulation_rollback_scripts.py runs the script and checks its calls.
    shell = step_shell(step)
    assert shell.index("--print-pending-plan") < shell.index("--finalize-publication")
    assert '--environment "${MODAL_ENVIRONMENT}"' in shell
    assert '--release-environment "${TARGET}"' in shell
    assert "--gates-passed false" in shell
    assert "--publication-run" not in shell
    assert "--restore-plan" not in shell
    assert "--restore-latest" not in shell
    # A first-ever failed release has no restored identity or Cloud Run
    # service. Recovering its journal still succeeds and unblocks publishing.
    for fragment in (
        "--restore-plan",
        "--print-identity",
        "google-github-actions/auth@",
        "google-github-actions/setup-gcloud@",
        "gcloud run services describe",
        "cloud-run-simulation-entry-smoke.sh",
    ):
        skipped = steps[_step_index(steps, fragment)]
        assert skipped["if"] == "${{ !inputs.resolve_pending_publication }}"


def test_rollback_verifies_the_whole_restored_identity():
    """#743/#744: the smoke's identity is all or nothing, so pass all of it."""
    verify = _rollback_steps()[
        _step_index(_rollback_steps(), "cloud-run-simulation-entry-smoke.sh")
    ]
    assert verify["env"]["EXPECTED_IDENTITY"] == "required"
    assert verify["env"]["EXPECTED_LATEST"] == "true"
    for name, output in (
        ("EXPECTED_POLICYENGINE_VERSION", "policyengine_version"),
        ("EXPECTED_APP_NAME", "app_name"),
        ("EXPECTED_UK_MODEL_VERSION", "uk_model_version"),
    ):
        assert verify["env"][name] == f"${{{{ steps.identity.outputs.{output} }}}}"
    assert verify["env"]["EXPECTED_UK_DATASET_SHA256"] == (
        "${{ steps.identity.outputs.uk_dataset_sha256 || 'unrecorded' }}"
    )
    # A version-mode rollback must have made that version latest;
    # test_simulation_rollback_scripts.py runs the check.
    assert (
        '"${EXPECTED_POLICYENGINE_VERSION}" != "${POLICYENGINE_VERSION}"'
        in step_shell(verify)
    )


def _inventory_job(
    *,
    job=None,
    step=None,
    conclusion="success",
    target="prod",
    started_at="2026-11-05T00:00:00Z",
):
    """Return a job as an attempt's job list shows it.

    A rerun lists the jobs it carried over from the attempt before with
    their original ``started_at`` and steps, and uploads nothing for them.
    """
    return {
        "name": job
        or f"Complete {target} deployment / Publish routing after all services pass",
        "started_at": started_at,
        "steps": [
            {
                "name": step or "Keep the plan for a guarded restore",
                "conclusion": conclusion,
            }
        ],
    }


def _fake_gh(
    bin_dir: Path,
    attempts: int,
    kept: dict[int, int],
    inventory: dict[int, list[dict]] | None = None,
) -> None:
    """Return API JSON and execute the restore script's real --jq filter with jq."""
    payloads = {}
    for attempt in range(1, attempts + 1):
        # Each attempt's routes job ran in that attempt, so its start differs.
        jobs = [
            _inventory_job(started_at=f"2026-11-05T{attempt:02d}:00:00Z")
            for _ in range(kept.get(attempt, 0))
        ]
        # These are real-looking jobs/steps which must never count as a kept
        # plan. Dropping any job, step, or success selector changes the result.
        jobs.extend(
            [
                _inventory_job(
                    job=(
                        "Complete beta deployment / "
                        "Publish routing after all services pass"
                    )
                ),
                _inventory_job(step="Record the plan artifact"),
                _inventory_job(conclusion="failure"),
                _inventory_job(conclusion="skipped"),
            ]
        )
        payloads[str(attempt)] = {"jobs": jobs}
    if inventory is not None:
        payloads.update(
            {str(attempt): {"jobs": jobs} for attempt, jobs in inventory.items()}
        )
    script = f"""#!/usr/bin/env python3
import json
import re
import subprocess
import sys

args = sys.argv[1:]
attempt = re.search(r"/attempts/(\\d+)/jobs", " ".join(args))
payloads = {payloads!r}
payload = payloads[attempt.group(1)] if attempt else {{"run_attempt": {attempts}}}
query = args[args.index("--jq") + 1]
result = subprocess.run(["jq", "-r", query], input=json.dumps(payload), text=True)
sys.exit(result.returncode)
"""
    (bin_dir / "gh").write_text(script)
    (bin_dir / "gh").chmod(0o755)


def _routes_plan(attempt: int) -> str:
    return f"routing-plan-prod-{attempt}"


def _latest_plan(attempt: int) -> str:
    return f"routing-latest-plan-prod-{attempt}"


LATEST_JOB = "Complete prod deployment / Move latest to the release after every check"
LATEST_KEEP_STEP = "Keep the latest plan for a guarded restore"


def _run_restore(
    tmp_path: Path,
    attempts: int,
    kept: dict,
    present: list[str],
    inventory: dict[int, list[dict]] | None = None,
):
    """Run the restore with the named plan artifacts downloaded."""
    runner_temp = tmp_path / "runner-temp-with-dashes"
    (runner_temp / "routing-plans").mkdir(parents=True)
    for artifact in present:
        directory = runner_temp / "routing-plans" / artifact
        directory.mkdir(parents=True)
        (directory / "plan.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, attempts, kept, inventory)
    # The publisher's arguments, one per line, stand in for the restore.
    (bin_dir / "uv").write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    (bin_dir / "uv").chmod(0o755)
    return _run_bash(
        RESTORE_ROUTING_SCRIPT,
        check=False,
        capture_output=True,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "TARGET": "prod",
            "MODAL_ENVIRONMENT": "main",
            "DEPLOY_RUN_ID": "1",
            "POLICYENGINE_VERSION": "",
            "EXPECTED_LATEST": "",
            "GITHUB_REPOSITORY": "PolicyEngine/policyengine-sim-api",
        },
    )


def test_rollback_passes_every_attempts_plan_oldest_first(tmp_path):
    """#744 review: a later attempt must not hide an earlier publication."""
    attempts = [1, 10, 2, 9]
    result = _run_restore(
        tmp_path,
        10,
        dict.fromkeys(attempts, 1),
        present=[_routes_plan(attempt) for attempt in attempts],
    )
    assert result.returncode == 0, result.stderr
    passed = result.stdout.split("--restore-plan\n", 1)[1].split()
    assert [Path(path).parent.name for path in passed] == [
        "routing-plan-prod-1",
        "routing-plan-prod-2",
        "routing-plan-prod-9",
        "routing-plan-prod-10",
    ]


ROUTES_STARTED = "2026-11-05T14:04:57Z"
FIRST_LATEST_STARTED = "2026-11-05T14:40:00Z"
SECOND_LATEST_STARTED = "2026-11-06T09:00:00Z"


def _rerun_of_the_latest_job() -> dict[int, list[dict]]:
    """Attempt 1 ran both jobs; attempt 2 reran only the latest job.

    Attempt 2's job list still shows the routes job, carried over with
    attempt 1's start time, as GitHub lists it (run 42040665530).
    """
    routes = _inventory_job(started_at=ROUTES_STARTED)
    return {
        1: [
            routes,
            _inventory_job(
                job=LATEST_JOB, step=LATEST_KEEP_STEP, started_at=FIRST_LATEST_STARTED
            ),
        ],
        2: [
            routes,
            _inventory_job(
                job=LATEST_JOB, step=LATEST_KEEP_STEP, started_at=SECOND_LATEST_STARTED
            ),
        ],
    }


def test_rollback_passes_each_attempts_routes_then_latest_plan_oldest_first(
    tmp_path,
):
    """A rerun's latest plan is restored after the earlier attempt's plans.

    Attempt 1 published the routes and moved latest, which a check put back;
    attempt 2 reran only the latest job and moved latest again.
    """
    result = _run_restore(
        tmp_path,
        2,
        {},
        present=[_latest_plan(2), _routes_plan(1), _latest_plan(1)],
        inventory=_rerun_of_the_latest_job(),
    )
    assert result.returncode == 0, result.stderr
    passed = result.stdout.split("--restore-plan\n", 1)[1].split()
    assert [Path(path).parent.name for path in passed] == [
        _routes_plan(1),
        _latest_plan(1),
        _latest_plan(2),
    ]


def test_rollback_needs_no_plan_from_a_job_a_rerun_carried_over(tmp_path):
    """Attempt 2 lists attempt 1's routes job but uploaded no routes plan."""
    result = _run_restore(
        tmp_path,
        2,
        {},
        present=[_routes_plan(1), _latest_plan(1), _latest_plan(2)],
        inventory=_rerun_of_the_latest_job(),
    )
    assert result.returncode == 0, result.stderr
    assert _routes_plan(2) not in result.stdout


def test_rollback_refuses_when_an_attempts_latest_plan_is_missing(tmp_path):
    result = _run_restore(
        tmp_path,
        2,
        {},
        present=[_routes_plan(1), _latest_plan(1)],
        inventory=_rerun_of_the_latest_job(),
    )
    assert result.returncode == 1
    assert "Attempt 2 of run 1 kept a prod latest plan that cannot be found" in (
        result.stderr
    )
    assert "--restore-plan" not in result.stdout


def test_rollback_refuses_when_an_attempts_plan_is_missing(tmp_path):
    """#744 review: attempt 1 published but its plan is gone; attempt 2's is not."""
    result = _run_restore(tmp_path, 2, {1: 1, 2: 1}, present=[_routes_plan(2)])
    assert result.returncode == 1
    assert "Attempt 1 of run 1 kept a prod routing plan that cannot be found" in (
        result.stderr
    )
    assert "--restore-plan" not in result.stdout


def test_rollback_ignores_attempts_that_kept_no_plan(tmp_path):
    result = _run_restore(tmp_path, 2, {1: 0, 2: 1}, present=[_routes_plan(2)])
    assert result.returncode == 0, result.stderr
    assert "routing-plan-prod-2/plan.json" in result.stdout


def test_rollback_with_no_plan_at_all_fails(tmp_path):
    result = _run_restore(tmp_path, 1, {1: 0}, present=[])
    assert result.returncode == 1
    assert "kept no prod routing plan" in result.stderr


@pytest.mark.parametrize(
    "ignored_job",
    [
        _inventory_job(
            job="Complete beta deployment / Publish routing after all services pass"
        ),
        _inventory_job(job="Complete prod deployment / Read the release identity"),
        _inventory_job(step="Record the plan artifact"),
        _inventory_job(conclusion="failure"),
        _inventory_job(conclusion="skipped"),
    ],
    ids=["other-environment", "other-job", "other-step", "failed-step", "skipped-step"],
)
def test_inventory_jq_selects_only_successfully_kept_target_plans(
    tmp_path, ignored_job
):
    """The restore script's job/step/conclusion jq selectors all run, not a stub."""
    result = _run_restore(
        tmp_path,
        2,
        {2: 1},
        present=[_routes_plan(2)],
        inventory={1: [ignored_job]},
    )
    assert result.returncode == 0, result.stderr
    assert "routing-plan-prod-2/plan.json" in result.stdout


def test_rollbacks_share_the_deployment_concurrency_group():
    group = re.compile(
        r"^concurrency:\n(?:  #.*\n)*  group: (?P<group>\S+)\n", re.MULTILINE
    )
    rollback = group.search(ROLLBACK_WORKFLOW.read_text(encoding="utf-8"))
    deploy = group.search(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    assert rollback
    assert deploy
    assert rollback.group("group") == deploy.group("group") == "simulation-deploy-main"
    assert "cancel-in-progress: false" in ROLLBACK_WORKFLOW.read_text(encoding="utf-8")


def test_validator_has_valid_shell_syntax_and_is_executable():
    _run_bash("-n", VALIDATOR, check=True)
    assert os.access(VALIDATOR, os.X_OK)
