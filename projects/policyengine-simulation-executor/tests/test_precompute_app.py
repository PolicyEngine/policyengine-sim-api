"""Modal-facing shape of the precompute app.

Only the declarations live in the app module (bodies are in
policyengine_simulation_executor.precompute and are tested in
test_precompute.py), so these tests assert the declarations — function
resources, secrets, the image's local-source mount — plus the wrapper
bodies' serialization boundary (validate dicts in, dump models out) and
the local entrypoint's wiring.
"""

import importlib
import sys

import pytest
from pydantic import ValidationError

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


def _sample_plan():
    from policyengine_simulation_executor.precompute_models import PrecomputePlan

    return PrecomputePlan.model_validate(
        {
            "datasets": [
                {
                    "year": 2026,
                    "digest": "d1",
                    "path": "datasets/us/d1/populace_year_2026.h5",
                    "filename": "populace_year_2026.h5",
                    "exists": False,
                }
            ],
            "baselines": [
                {
                    "year": 2026,
                    "group": ["state/ca"],
                    "region": "state/ca",
                    "digest": "b1",
                    "path": "baselines/us/b1/bl1-aaaa.h5",
                    "simulation_id": "bl1-aaaa",
                    "exists": False,
                }
            ],
            "receipt": {
                "policyengine_version": "4.22.0",
                "model_version": "1.0.0",
                "data_version": "1.2.3",
                "data_artifact_revision": "rev-abc",
                "default_dataset": "populace_cps",
            },
        }
    )


def test_wrappers_validate_dicts_in_and_dump_models_out(precompute_module, monkeypatch):
    """The wrappers ARE the Modal serialization boundary: dict in,
    validated model to the impl, model back, dict out."""
    from policyengine_simulation_executor import precompute as lib
    from policyengine_simulation_executor.precompute_models import (
        BaselineComputeResult,
        DatasetBuildResult,
        DeterminismVerdict,
    )

    plan = _sample_plan()
    seen = {}

    monkeypatch.setattr(lib, "plan_artifacts_impl", lambda bucket: plan)
    assert precompute_module.plan_artifacts("bucket-x") == plan.model_dump()

    dataset_result = DatasetBuildResult(
        year=2026, path="p", uploaded=True, build_seconds=1.0, size_bytes=3
    )

    def fake_build_dataset(bucket, expected):
        seen["dataset"] = (bucket, expected)
        return dataset_result

    monkeypatch.setattr(lib, "build_dataset_impl", fake_build_dataset)
    assert (
        precompute_module.build_dataset("bucket-x", plan.datasets[0].model_dump())
        == dataset_result.model_dump()
    )
    assert seen["dataset"] == ("bucket-x", plan.datasets[0])

    baseline_result = BaselineComputeResult(
        year=2026,
        group=["state/ca"],
        simulation_id="bl1-aaaa",
        outcome="miss",
        uploaded=True,
        compute_seconds=2.0,
        size_bytes=3,
    )

    def fake_compute_baseline(bucket, expected):
        seen["baseline"] = (bucket, expected)
        return baseline_result

    monkeypatch.setattr(lib, "compute_baseline_impl", fake_compute_baseline)
    assert (
        precompute_module.compute_baseline("bucket-x", plan.baselines[0].model_dump())
        == baseline_result.model_dump()
    )
    assert seen["baseline"] == ("bucket-x", plan.baselines[0])

    verdict = DeterminismVerdict(equal=True, differences=[])
    monkeypatch.setattr(
        lib, "verify_determinism_impl", lambda bucket, expected: verdict
    )
    assert (
        precompute_module.verify_determinism("bucket-x", plan.baselines[0].model_dump())
        == verdict.model_dump()
    )

    def fake_publish_manifest(bucket, validated):
        seen["publish"] = (bucket, validated)
        return "digest-abc"

    monkeypatch.setattr(lib, "publish_manifest_impl", fake_publish_manifest)
    # publish_manifest returns the bare digest string — no dump.
    assert (
        precompute_module.publish_manifest("bucket-x", plan.model_dump())
        == "digest-abc"
    )
    assert seen["publish"] == ("bucket-x", plan)


def test_wrappers_reject_malformed_input_at_the_edge(precompute_module):
    with pytest.raises(ValidationError):
        precompute_module.build_dataset("bucket-x", {"year": 2026})
    with pytest.raises(ValidationError):
        precompute_module.compute_baseline("bucket-x", {"unexpected": True})


def test_local_entrypoint_wires_bucket_force_and_handles(
    precompute_module, monkeypatch
):
    from policyengine_simulation_executor import artifact_store
    from policyengine_simulation_executor import precompute as lib

    captured = {}

    def fake_run_precompute(bucket, *, force, **handles):
        captured["bucket"] = bucket
        captured["force"] = force
        captured["handles"] = handles
        return "digest"

    monkeypatch.setattr(
        artifact_store, "resolve_bucket_name", lambda explicit=None: "bucket-env"
    )
    monkeypatch.setattr(lib, "run_precompute", fake_run_precompute)

    precompute_module.main(force=True)
    assert captured["bucket"] == "bucket-env"
    assert captured["force"] is True
    assert captured["handles"] == {
        "plan_artifacts": precompute_module.plan_artifacts,
        "build_dataset": precompute_module.build_dataset,
        "compute_baseline": precompute_module.compute_baseline,
        "verify_determinism": precompute_module.verify_determinism,
        "publish_manifest": precompute_module.publish_manifest,
    }
