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


def test_precompute_app_mounts_only_the_read_only_hugging_face_secret(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    read_only_secret = {
        "args": ("pe-uk-private-hf-read-token",),
        "kwargs": {"required_keys": ["HUGGING_FACE_TOKEN"]},
    }
    gcp_secret = {"args": ("stage12-evaluation-gcp-credentials",), "kwargs": {}}

    assert module.hf_secret == read_only_secret
    bundle_install = next(
        call for call in module.precompute_image.calls if call[0] == "run_commands"
    )
    assert bundle_install[2]["secrets"] == [read_only_secret]
    for function_name, declaration in module.app.function_calls:
        assert declaration["secrets"] == [gcp_secret, read_only_secret], function_name
