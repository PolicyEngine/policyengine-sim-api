"""Modal-facing shape of the precompute app.

Only the declarations live in the app module (bodies are in
policyengine_simulation_executor.precompute and are tested in
test_precompute.py), so these tests assert the declarations: function
resources, secrets, and the image's local-source mount.
"""

import importlib
import sys

import pytest

from fixtures.fake_modal import install_fake_modal

_WORKER_FUNCTIONS = (
    "plan_artifacts",
    "build_dataset",
    "compute_baseline",
    "verify_determinism",
    "publish_manifest",
)


@pytest.fixture
def precompute_module(monkeypatch):
    install_fake_modal(monkeypatch)
    for env in (
        "POLICYENGINE_VERSION",
        "POLICYENGINE_CORE_VERSION",
        "POLICYENGINE_US_VERSION",
        "POLICYENGINE_UK_VERSION",
    ):
        monkeypatch.setenv(env, "0.0.0-test")
    for module in ("src.modal.app", "src.modal.precompute_app"):
        sys.modules.pop(module, None)
    module = importlib.import_module("src.modal.precompute_app")
    yield module
    for name in ("src.modal.app", "src.modal.precompute_app"):
        sys.modules.pop(name, None)


def _function_kwargs(module, name):
    return dict(module.app.function_calls)[name]


def test_declares_exactly_the_worker_functions(precompute_module):
    assert [name for name, _ in precompute_module.app.function_calls] == list(
        _WORKER_FUNCTIONS
    )


@pytest.mark.parametrize("name", _WORKER_FUNCTIONS)
def test_every_worker_function_carries_gcp_secret(precompute_module, name):
    secrets = _function_kwargs(precompute_module, name)["secrets"]
    assert {
        "args": ("gcp-credentials",),
        "kwargs": {"environment_name": "main"},
    } in secrets


def test_compute_baseline_matches_segment_worker_shape(precompute_module):
    kwargs = _function_kwargs(precompute_module, "compute_baseline")
    assert kwargs["cpu"] == 8.0
    assert kwargs["memory"] == 32768
    assert kwargs["timeout"] == 3600


def test_dataset_builder_gets_prebuild_scale_resources(precompute_module):
    kwargs = _function_kwargs(precompute_module, "build_dataset")
    assert kwargs["cpu"] == 8.0
    assert kwargs["memory"] == 65536


def test_image_includes_local_source(precompute_module):
    calls = precompute_module.precompute_image.calls
    local_source = [call for call in calls if call[0] == "add_local_python_source"]
    assert local_source, "precompute image must mount executor source"
    assert set(local_source[-1][1]) == {
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
    }
