"""Declaration tests for the independent Stage 12 precompute app."""

from __future__ import annotations

import importlib
import sys

from fixtures.fake_modal import install_fake_modal


def _load(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.stage12_precompute_app", None)
    return importlib.import_module("src.modal.stage12_precompute_app")


def test_precompute_app_declares_independent_worker_resources(monkeypatch) -> None:
    module = _load(monkeypatch)
    functions = dict(module.app.function_calls)

    assert set(functions) == {
        "plan_artifacts",
        "build_dataset",
        "compute_baseline",
        "verify_determinism",
        "publish_manifest",
    }
    assert functions["build_dataset"]["cpu"] == 8.0
    assert functions["build_dataset"]["memory"] == 65536
    assert functions["build_dataset"]["timeout"] == 7200
    assert functions["compute_baseline"]["cpu"] == 8.0
    assert functions["compute_baseline"]["memory"] == 32768
    assert functions["compute_baseline"]["timeout"] == 3600
    assert module.app.name == "policyengine-stage12-cache-precompute"


def test_precompute_image_installs_only_the_us_bundle(monkeypatch) -> None:
    module = _load(monkeypatch)
    command = next(
        call for call in module.precompute_image.calls if call[0] == "run_commands"
    )[1][0]

    assert "--country us" in command
    assert "--country uk" not in command
    local_source = next(
        call
        for call in module.precompute_image.calls
        if call[0] == "add_local_python_source"
    )
    assert "policyengine_simulation_executor" in local_source[1]
    assert "policyengine_stage12_persistence" in local_source[1]
