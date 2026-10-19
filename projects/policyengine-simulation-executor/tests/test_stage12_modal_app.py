"""Declaration tests for the separate Stage 12 Modal application."""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import tomllib
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_executor.release_bundle import (
    get_bundled_package_version,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000001"


class RecordingRuntime:
    def __init__(self) -> None:
        self.contexts: list[dict] = []
        self.operations: list[dict] = []
        self.shutdown_calls = 0

    def set_context(self, **attributes) -> None:
        self.contexts.append(attributes)

    @contextmanager
    def operation(self, name, **kwargs):
        self.operations.append({"name": name, **kwargs})
        yield

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _load(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_app", None)
    return importlib.import_module("src.modal.v2_app")


def _observability_context() -> dict[str, str]:
    return {
        "captured_at": "2026-09-24T00:00:00Z",
        "observability_id": OBSERVABILITY_ID,
        "traceparent": "00-00000000000000000000000000000001-0000000000000001-01",
    }


def _invoke(
    monkeypatch,
    module,
    function_name: str,
    implementation,
):
    from policyengine_simulation_executor import stage12_runtime

    if function_name == "run_single_simulation_segment_us":

        class Result:
            def model_dump(self, *, mode):
                assert mode == "python"
                return implementation()

        monkeypatch.setattr(
            stage12_runtime, "calculate_segment", lambda *_, **__: Result()
        )
        return module.run_single_simulation_segment_us(
            {"simulation": {"role": "baseline"}, "segment_index": 0},
            observability_context=_observability_context(),
        )

    if function_name == "coordinate_report":
        monkeypatch.setattr(
            module.modal,
            "current_function_call_id",
            lambda: "call-1",
            raising=False,
        )
        monkeypatch.setattr(stage12_runtime, "coordinate_report", implementation)
        return module.coordinate_report(
            {},
            {},
            {},
            observability_context=_observability_context(),
        )

    monkeypatch.setattr(stage12_runtime, "run_single_simulation", implementation)
    return getattr(module, function_name)(
        {"role": "baseline"},
        {},
        observability_context=_observability_context(),
    )


def test_v2_app_name_and_images_are_separate_and_bundle_derived(monkeypatch) -> None:
    module = _load(monkeypatch)

    assert module.APP_NAME.startswith("policyengine-simulation-v2-py")
    assert not module.APP_NAME.startswith("policyengine-simulation-py")
    assert module.app.name == module.APP_NAME
    us_command = next(
        call for call in module.us_worker_image.calls if call[0] == "run_commands"
    )[1][0]
    uk_command = next(
        call for call in module.uk_worker_image.calls if call[0] == "run_commands"
    )[1][0]
    assert "--country us" in us_command
    assert "--country uk" not in us_command
    assert "--country uk" in uk_command
    assert "--country us" not in uk_command
    assert "--no-packages" in us_command
    assert "uvx" not in us_command
    assert "--venv" not in us_command
    assert module.gcp_secret["args"] == ("stage12-evaluation-gcp-credentials",)
    assert module.hf_secret == {
        "args": ("pe-uk-private-hf-read-token",),
        "kwargs": {"required_keys": ["HUGGING_FACE_TOKEN"]},
    }
    assert all(
        secret.get("args") != ("policyengine-data-credentials",)
        for secret in module.worker_secrets
    )
    assert all(
        secret.get("args") != ("policyengine-logfire",)
        for secret in module.worker_secrets
    )
    for image in (
        module.us_worker_image,
        module.uk_worker_image,
        module.coordinator_image,
    ):
        local_source = next(
            call for call in image.calls if call[0] == "add_local_python_source"
        )
        bundle_install = next(call for call in image.calls if call[0] == "run_commands")
        assert bundle_install[2]["secrets"] == [module.hf_secret]
        assert local_source[1] == (
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
            "policyengine_stage12_persistence",
        )
        static_runtime_files = next(
            call for call in image.calls if call[0] == "add_local_dir"
        )
        assert Path(static_runtime_files[1]) == (
            PROJECT_ROOT
            / "src"
            / "policyengine_simulation_executor"
            / "static_runtime_files"
        )
        assert static_runtime_files[2] == (
            "/root/policyengine_simulation_executor/static_runtime_files"
        )
        assert static_runtime_files[3] == {"copy": True}


def test_v2_image_retains_required_runtime_dependencies() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    group = project["dependency-groups"]["modal-simulation-image"]
    assert (
        f"policyengine[models]=={get_bundled_package_version('policyengine')}" in group
    )
    assert not any(isinstance(item, dict) for item in group)
    assert "sqlalchemy>=2,<3" in group
    assert "psycopg[binary]>=3.2,<4" in group


def test_v2_static_runtime_file_smoke_uses_exact_uk_worker_image(monkeypatch) -> None:
    module = _load(monkeypatch)
    sys.modules.pop("src.modal.v2_static_runtime_files_smoke", None)
    smoke = importlib.import_module("src.modal.v2_static_runtime_files_smoke")
    functions = {name: options for name, options in smoke.app.function_calls}

    assert functions["smoke_stage12_static_runtime_files"]["image"] is (
        module.uk_worker_image
    )
    monkeypatch.setattr(
        smoke,
        "validate_installed_uk_local_authority_dataset",
        lambda: "lad22",
    )
    assert smoke.smoke_stage12_static_runtime_files() == {
        "names": 378,
        "lad22_coordinates": 374,
        "lad23_coordinates": 361,
        "installed_dataset_boundary_version": "lad22",
    }


def test_only_us_worker_image_fetches_stage12_cache(monkeypatch) -> None:
    module = _load(monkeypatch)

    assert any(call[0] == "run_function" for call in module.us_worker_image.calls)
    assert not any(call[0] == "run_function" for call in module.uk_worker_image.calls)
    assert not any(call[0] == "run_function" for call in module.coordinator_image.calls)


def test_v2_app_declares_validation_workers_and_non_http_coordinator(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    functions = {name: options for name, options in module.app.function_calls}

    assert set(functions) == {
        "validate_worker_us",
        "validate_worker_uk",
        "run_single_simulation_segment_us",
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "coordinate_report",
        "calculate_household_us",
        "calculate_household_uk",
    }
    assert functions["run_single_simulation_us"]["image"] is module.us_worker_image
    assert functions["run_single_simulation_uk"]["image"] is module.uk_worker_image
    assert (
        functions["run_single_simulation_segment_us"]["image"] is module.us_worker_image
    )
    assert functions["run_single_simulation_segment_us"]["timeout"] == 3600
    assert functions["run_single_simulation_segment_us"]["max_containers"] == 300
    assert functions["run_single_simulation_us"]["timeout"] == 3900
    assert functions["run_single_simulation_uk"]["timeout"] == 3000
    assert functions["run_single_simulation_us"]["max_containers"] == 10
    assert functions["run_single_simulation_uk"]["max_containers"] == 10
    assert functions["coordinate_report"]["image"] is module.coordinator_image
    assert functions["coordinate_report"]["timeout"] == 5400
    assert functions["coordinate_report"]["max_containers"] == 10
    assert "asgi_app" not in vars(module)


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
        "coordinate_report",
    ],
)
def test_stage12_functions_propagate_identifier_and_close_runtime(
    monkeypatch,
    function_name,
) -> None:
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    result = _invoke(monkeypatch, module, function_name, lambda *_, **__: {"ok": True})

    assert result == {"ok": True}
    assert runtime.contexts == []
    assert runtime.operations[0]["remote_context"]["observability_id"] == (
        OBSERVABILITY_ID
    )
    assert runtime.shutdown_calls == 1


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
        "coordinate_report",
    ],
)
def test_stage12_functions_close_runtime_after_application_error(
    monkeypatch,
    function_name,
) -> None:
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    def fail(*_, **__):
        msg = "application failure"
        raise LookupError(msg)

    with pytest.raises(LookupError, match="application failure"):
        _invoke(monkeypatch, module, function_name, fail)

    assert runtime.shutdown_calls == 1


def test_household_functions_are_generated_for_every_bundle_country(
    monkeypatch,
) -> None:
    from policyengine_simulation_contract.household_execution import (
        household_callable_name,
    )

    module = _load(monkeypatch)
    functions = {name: options for name, options in module.app.function_calls}
    bundle_countries = [
        country.country for country in module.RESOLVED_BUNDLE.bundle.countries
    ]

    for country in bundle_countries:
        name = household_callable_name(country)
        options = functions[name]
        assert getattr(module, name).__qualname__ == name
        assert options["name"] == name
        assert options["image"] is module.household_image
        assert options["cpu"] == 2.0
        # Reserve 4 GiB; allow up to 16 GiB for large households calculated
        # with every variable at many axis points.
        assert options["memory"] == (4096, 16384)
        assert options["timeout"] == 900
        assert options["retries"] == 0
        assert options["max_containers"] == 20
        # Household calculations read no private data, Cloud Storage or
        # database, so they get no Modal secrets.
        assert options["secrets"] == []


def test_household_image_holds_no_datasets_or_cache(monkeypatch) -> None:
    module = _load(monkeypatch)
    calls = module.household_image.calls

    assert not any(call[0] == "run_commands" for call in calls)
    assert not any(call[0] == "run_function" for call in calls)
    local_source = next(call for call in calls if call[0] == "add_local_python_source")
    assert "policyengine_simulation_contract" in local_source[1]
    environment = next(call for call in calls if call[0] == "env")[1]
    assert environment["STAGE12_BUNDLE_MANIFEST_SHA256"] == (
        module.RESOLVED_BUNDLE.bundle_manifest_sha256
    )
    assert "POLICYENGINE_DATA_FOLDER" not in environment


def _household_payload(country: str) -> dict:
    from policyengine_simulation_executor.household_calculation import (
        installed_household_bundle,
    )

    return {
        "calculation_id": "00000000-0000-4000-8000-0000000000c1",
        "country": country,
        "household": {"people": {"you": {"age": {"2026": 40}}}},
        "policies": [{"policy_key": "baseline", "parameters": {}}],
        "bundle": installed_household_bundle(country).model_dump(mode="json"),
    }


@pytest.mark.parametrize("country", ["us", "uk"])
def test_household_function_calculates_its_country_and_closes_runtime(
    monkeypatch, country
) -> None:
    from policyengine_simulation_contract.household_execution import (
        HouseholdExecutionFailure,
        HouseholdExecutionRequest,
    )
    from policyengine_simulation_executor import household_calculation

    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    runtime.span = lambda name, *, attributes: runtime.operation(
        name, attributes=attributes
    )
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    calls = []

    def fake_calculate(request, *, worker, span):
        calls.append((request, worker))
        with span("v2_household_calculation", {"policy_key": "baseline"}):
            pass
        return HouseholdExecutionFailure(
            calculation_id=request.calculation_id,
            error={"code": "calculation_failed", "message": "recorded"},
            worker=worker,
        )

    monkeypatch.setattr(household_calculation, "calculate_household", fake_calculate)

    result = getattr(module, f"calculate_household_{country}")(
        _household_payload(country),
        observability_context=_observability_context(),
    )

    assert result["status"] == "failed"
    request, worker = calls[0]
    assert isinstance(request, HouseholdExecutionRequest)
    assert request.country == country
    assert worker.application_name == module.APP_NAME
    assert worker.function_name == f"calculate_household_{country}"
    assert runtime.operations[0]["name"] == "v2_household_execution"
    assert runtime.operations[0]["remote_context"]["observability_id"] == (
        OBSERVABILITY_ID
    )
    assert runtime.operations[1]["name"] == "v2_household_calculation"
    assert runtime.shutdown_calls == 1


def test_household_function_refuses_another_country(monkeypatch) -> None:
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    with pytest.raises(ValueError, match="calculates uk households"):
        module.calculate_household_uk(_household_payload("us"))

    assert runtime.shutdown_calls == 1


def test_external_package_values_are_assertions_only(monkeypatch) -> None:
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("STAGE12_EXPECT_US_VERSION", "not-the-bundled-version")
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(ValueError, match="does not match"):
        importlib.import_module("src.modal.v2_app")


def test_modal_app_name_cannot_override_bundle_selection(monkeypatch) -> None:
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "operator-selected")
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(RuntimeError, match="does not match"):
        importlib.import_module("src.modal.v2_app")
