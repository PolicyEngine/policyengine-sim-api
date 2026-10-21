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
        "build_state_datasets",
        "compute_baseline",
        "verify_determinism",
        "publish_manifest",
    }
    assert functions["build_dataset"]["cpu"] == 8.0
    assert functions["build_dataset"]["memory"] == 65536
    assert functions["build_dataset"]["timeout"] == 7200
    assert functions["build_state_datasets"]["cpu"] == 8.0
    # Preparing the regional dataset's full-country year file has used about
    # 51 GB; the 51 filtered copies need room on top of that.
    assert functions["build_state_datasets"]["memory"] == 98304
    assert functions["build_state_datasets"]["timeout"] == 7200
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
    # Packages come from the locked environment; the uv environment has no pip.
    assert "--no-packages" in command
    assert "--venv" not in command
    local_source = next(
        call
        for call in module.precompute_image.calls
        if call[0] == "add_local_python_source"
    )
    assert "policyengine_simulation_executor" in local_source[1]
    assert "policyengine_stage12_persistence" in local_source[1]


def test_precompute_image_refreshes_ca_certificates_before_installing_packages(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    calls = module.precompute_image.calls

    assert calls[0][0] == "debian_slim"
    assert calls[1] == ("apt_install", ("ca-certificates",))
    assert calls[2] == (
        "env",
        {"GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": "/etc/ssl/certs/ca-certificates.crt"},
    )
    assert calls[3][0] == "uv_sync"


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


def test_state_dataset_wrapper_validates_entries_in_and_dumps_results_out(
    monkeypatch,
) -> None:
    import pytest
    from policyengine_simulation_executor.stage12_cache.models import (
        StateDatasetBuildResult,
        StateDatasetPlanEntry,
    )
    from pydantic import ValidationError

    module = _load(monkeypatch)
    entry = StateDatasetPlanEntry(
        year=2026,
        state_code="UT",
        region="state/ut",
        dataset="populace_us_2024_acs_local",
        digest="s1",
        path="state-datasets/us/s1/populace_us_2024_acs_local_year_2026.h5",
        filename="populace_us_2024_acs_local_year_2026.h5",
        folder="state-datasets/populace_us_2024_acs_local/UT",
        exists=False,
    )
    result = StateDatasetBuildResult(
        path=entry.path,
        content_sha256="c" * 64,
        size_bytes=5,
        year=2026,
        state_code="UT",
        uploaded=True,
        build_seconds=1.0,
    )
    seen = {}

    def fake_build(bucket, year, expected):
        seen["call"] = (bucket, year, expected)
        return (result,)

    monkeypatch.setattr(module, "build_state_datasets_impl", fake_build)

    assert module.build_state_datasets("cache-bucket", 2026, [entry.model_dump()]) == [
        result.model_dump()
    ]
    assert seen["call"] == ("cache-bucket", 2026, (entry,))
    with pytest.raises(ValidationError):
        module.build_state_datasets("cache-bucket", 2026, [{"year": 2026}])
