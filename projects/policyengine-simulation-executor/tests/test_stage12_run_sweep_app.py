"""Declaration tests for the Modal app that runs the stuck-run sweep on a schedule."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_executor.stage12_runtime import (
    report_run_access as report_run_access_module,
)
from policyengine_simulation_executor.stage12_runtime import run_sweep

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


def test_the_executor_deployment_deploys_the_sweep_with_the_modal_tokens() -> None:
    workflow = yaml.safe_load(REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy_executor"]["steps"]
    step_names = [step.get("name") for step in steps]
    (sweep_step,) = [
        step for step in steps if "src/modal/run_sweep_app.py" in step.get("run", "")
    ]

    assert sweep_step["run"] == (
        'uv run --frozen modal deploy --env="${{ inputs.modal_environment }}" '
        "src/modal/run_sweep_app.py"
    )
    assert (
        sweep_step["working-directory"] == "projects/policyengine-simulation-executor"
    )
    # Only Modal's tokens: the database URL reaches the scheduled function from
    # its Modal secret, which must exist before this step can deploy it.
    assert sweep_step["env"] == {
        "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
        "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
    }
    assert step_names.index(sweep_step["name"]) > step_names.index(
        "Deploy versioned Modal executor"
    )


def test_the_sweep_runs_every_ten_minutes_with_the_worker_database_secret(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    ((name, options),) = module.app.function_calls

    assert module.app.name == "policyengine-simulation-run-sweep"
    assert name == "sweep_stuck_runs"
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
    # The app module imports src.modal at module level, in the container too.
    assert calls["add_local_python_source"][0] == (
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_stage12_persistence",
    )


def test_the_scheduled_function_sweeps_with_the_report_run_store(monkeypatch) -> None:
    module = _load(monkeypatch)
    store = object()
    swept = []
    monkeypatch.setattr(report_run_access_module, "report_run_store", lambda: store)

    def recording_sweep(received_store):
        swept.append(received_store)
        return run_sweep.SweepSummary(
            checked=3, failed=1, running=1, unknown=1, left=0, failed_for_age=2
        )

    monkeypatch.setattr(run_sweep, "sweep_stuck_runs", recording_sweep)

    assert module.sweep_stuck_runs() == {
        "checked": 3,
        "failed": 1,
        "running": 1,
        "unknown": 1,
        "left": 0,
        "failed_for_age": 2,
    }
    assert swept == [store]
