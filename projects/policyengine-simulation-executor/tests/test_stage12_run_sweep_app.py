"""Declaration tests for the Modal app that runs the stuck-run sweep on a schedule."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_entry import run_restart_check
from policyengine_simulation_entry.run_restart_check import (
    RESTART_STATEMENT_TIMEOUT_MILLISECONDS,
)
from policyengine_simulation_entry.run_restarts import (
    CALL_START_TIMEOUT_SECONDS,
    CHECK_TIME_LIMIT,
)
from policyengine_simulation_executor.stage12_runtime import (
    report_run_access as report_run_access_module,
)
from policyengine_simulation_executor.stage12_runtime import run_sweep
from policyengine_simulation_observability import observability

REUSABLE_DEPLOY_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "simulation-deploy.reusable.yml"
)


def _load(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.run_sweep_app", None)
    return importlib.import_module("src.modal.run_sweep_app")


def _deploy_steps(job: dict, app_file: str) -> list[dict]:
    return [step for step in job.get("steps", []) if app_file in step.get("run", "")]


def _telemetry_settings(environment: dict[str, str]) -> dict[str, str]:
    """Return the deploy-time settings ``modal_image_environment`` bakes in."""
    return {
        name: value
        for name, value in environment.items()
        if name.startswith(("OBSERVABILITY_", "OTEL_", "POLICYENGINE_OTEL_"))
    }


def _functions(module) -> dict[str, dict]:
    return dict(module.app.function_calls)


def _sweep_deploy_steps(job: dict) -> list[dict]:
    return [
        step
        for step in job.get("steps", [])
        if "src/modal/run_sweep_app.py" in step.get("run", "")
    ]


def test_the_sweep_deploys_with_the_stage12_default_after_every_check() -> None:
    """The unversioned sweep deploys only once the release has passed its checks.

    It deploys in the job that moves the Stage 12 v2 default, which runs after
    the integration and authenticated tests and the Cloud Run promotion, and
    only from ``main``: a dispatch from another branch, which may be behind
    ``main``, leaves the environment's sweep as it is. A failed sweep deploy
    fails that job, whose last step then puts the previous default back.
    """
    workflow = yaml.safe_load(REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    publish_job = jobs["publish_stage12_v2_default"]
    step_names = [step.get("name") for step in publish_job["steps"]]
    (sweep_step,) = _sweep_deploy_steps(publish_job)

    assert all(
        not _sweep_deploy_steps(job)
        for name, job in jobs.items()
        if name != "publish_stage12_v2_default"
    )
    assert {"integration", "authenticated_test", "promote_entrypoint"} <= set(
        publish_job["needs"]
    )
    assert sweep_step["run"] == (
        'uv run --frozen modal deploy --env="${{ inputs.modal_environment }}" '
        "src/modal/run_sweep_app.py"
    )
    assert (
        sweep_step["working-directory"] == "projects/policyengine-simulation-executor"
    )
    # Modal's tokens, the Modal environment the restart check starts calls in,
    # and the v2 workers' own telemetry settings, which the image bakes in.
    # The database URL reaches the scheduled functions from their Modal
    # secret, which must exist before this step can deploy them.
    (v2_workers_step,) = _deploy_steps(jobs["deploy_stage12_v2"], "src/modal/v2_app.py")
    telemetry = _telemetry_settings(v2_workers_step["env"])
    assert len(telemetry) == 8
    assert sweep_step["env"] == {
        "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
        "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
        "SIMULATION_V2_MODAL_ENVIRONMENT": "${{ inputs.modal_environment }}",
        **telemetry,
    }
    # Only from main. With no status function in it, the condition also
    # requires every earlier step to have passed (an implicit success()).
    assert sweep_step["if"] == "${{ github.ref == 'refs/heads/main' }}"
    sweep_position = step_names.index(sweep_step["name"])
    assert sweep_position > step_names.index("Verify a reader resolves the v2 default")
    assert sweep_position < step_names.index(
        "Put the v2 default back after a failed or cancelled step"
    )


def test_the_sweep_runs_every_ten_minutes_with_the_worker_database_secret(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    options = _functions(module)["sweep_stuck_runs"]

    assert module.app.name == "policyengine-simulation-run-sweep"
    assert options["schedule"] == {"schedule": "*/10 * * * *"}
    assert options["secrets"] == [
        {
            "args": ("policyengine-modal-worker-database",),
            "kwargs": {"required_keys": ["MODAL_WORKER_DATABASE_URL"]},
        }
    ]
    # One sweep at a time in this app; two at once would still be safe.
    assert options["max_containers"] == 1
    assert options["retries"] == 0
    assert options["image"] is module.sweep_image


def test_the_sweep_image_installs_the_locked_group_and_the_runtime_sources(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    calls = {call[0]: call[1:] for call in module.sweep_image.calls}

    assert calls["uv_sync"][1]["frozen"] is True
    assert calls["uv_sync"][1]["extra_options"] == "--only-group modal-simulation-image"
    # The app module imports src.modal at module level, in the container too;
    # the restart check runs the simulation service's own restart code.
    assert calls["add_local_python_source"][0] == (
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_stage12_persistence",
        "policyengine_simulation_persistence",
        "policyengine_simulation_entry",
    )


def test_the_scheduled_function_sweeps_with_the_report_run_store(monkeypatch) -> None:
    module = _load(monkeypatch)
    store, window_store = object(), object()
    swept = []
    monkeypatch.setattr(report_run_access_module, "report_run_store", lambda: store)
    monkeypatch.setattr(
        report_run_access_module, "budget_window_store", lambda: window_store
    )

    def recording_sweep(received_store, *, windows):
        swept.append((received_store, windows))
        return run_sweep.SweepSummary(
            checked=3,
            failed=1,
            running=1,
            unknown=1,
            left=0,
            failed_for_age=2,
            failed_without_call=4,
            windows_failed=5,
            windows_succeeded=6,
        )

    monkeypatch.setattr(run_sweep, "sweep_stuck_runs", recording_sweep)

    assert module.sweep_stuck_runs() == {
        "checked": 3,
        "failed": 1,
        "running": 1,
        "unknown": 1,
        "left": 0,
        "failed_for_age": 2,
        "failed_without_call": 4,
        "windows_failed": 5,
        "windows_succeeded": 6,
    }
    assert swept == [(store, window_store)]


def test_the_restart_check_runs_every_two_minutes_with_the_worker_database_secret(
    monkeypatch,
) -> None:
    """It restarts runs 3 to 10 minutes old, so each is seen several times."""
    module = _load(monkeypatch)
    options = _functions(module)["restart_unstarted_runs"]

    assert options["schedule"] == {"schedule": "*/2 * * * *"}
    assert options["secrets"] == [
        {
            "args": ("policyengine-modal-worker-database",),
            "kwargs": {"required_keys": ["MODAL_WORKER_DATABASE_URL"]},
        }
    ]
    # One check at a time; two at once would still restart each run once.
    assert options["max_containers"] == 1
    assert options["retries"] == 0
    assert options["image"] is module.sweep_image


def test_a_check_that_waits_on_every_call_still_ends_within_its_timeout(
    monkeypatch,
) -> None:
    """A row marked just before the timeout stops the check is never restarted.

    The check marks no row once its time limit has passed; after a row is
    marked, it waits at most for the call's start and for the mark, record or
    failure statements, each bounded by the statement timeout.
    """
    module = _load(monkeypatch)
    options = _functions(module)["restart_unstarted_runs"]
    statement_seconds = RESTART_STATEMENT_TIMEOUT_MILLISECONDS / 1000
    longest_after_the_limit = CALL_START_TIMEOUT_SECONDS + 3 * statement_seconds

    # Half a minute more for connecting and Modal's own start.
    assert (
        CHECK_TIME_LIMIT.total_seconds() + longest_after_the_limit + 30
        <= options["timeout"]
    )


def test_the_image_bakes_the_modal_environment_and_telemetry_settings_in(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIMULATION_V2_MODAL_ENVIRONMENT", "staging")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
    monkeypatch.setenv("OBSERVABILITY_LOG_NAME", "policyengine-api-v1-modal")
    module = _load(monkeypatch)
    baked: dict[str, str] = {}
    for call in module.sweep_image.calls:
        if call[0] == "env":
            baked.update(call[1])

    assert baked["SIMULATION_V2_MODAL_ENVIRONMENT"] == "staging"
    assert baked["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.example"
    assert baked["OBSERVABILITY_LOG_NAME"] == "policyengine-api-v1-modal"
    assert baked["OTEL_METRICS_EXPORTER"] == "otlp"


class RecordingRuntime:
    """Stands in for the observability runtime; records its shutdown."""

    def __init__(self) -> None:
        self.shut_down = False

    def shutdown(self) -> None:
        self.shut_down = True


def test_the_scheduled_restart_runs_the_check_on_the_worker_database(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    monkeypatch.setenv("MODAL_WORKER_DATABASE_URL", "postgresql://worker@db/postgres")
    monkeypatch.setenv("SIMULATION_V2_MODAL_ENVIRONMENT", "main")
    runtime = RecordingRuntime()
    built = []
    checked = []

    def recording_runtime(**identity):
        built.append(identity)
        return runtime

    def recording_check(**arguments):
        checked.append(arguments)
        return {"listed": 2, "restarted": 2}

    monkeypatch.setattr(observability, "init_process_observability", recording_runtime)
    monkeypatch.setattr(run_restart_check, "run_restart_check", recording_check)

    assert module.restart_unstarted_runs() == {"listed": 2, "restarted": 2}
    assert checked == [
        {
            "database_url": "postgresql://worker@db/postgres",
            "modal_environment": "main",
            "runtime": runtime,
        }
    ]
    assert built == [
        {
            "service_name": "policyengine-simulation-run-restart",
            "service_role": "v2_run_restart_check",
            "platform": "modal",
            "environment": "main",
        }
    ]
    assert runtime.shut_down
