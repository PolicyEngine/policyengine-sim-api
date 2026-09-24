"""Declaration tests for the separate Stage 12 Modal application."""

from __future__ import annotations

import importlib
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from fixtures.fake_modal import install_fake_modal

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

    if function_name == "coordinate_report":
        monkeypatch.setattr(
            module.modal,
            "current_function_call_id",
            lambda: "call-1",
            raising=False,
        )
        monkeypatch.setattr(stage12_runtime, "coordinate_report", implementation)
        return module.coordinate_report({}, {}, {}, _observability_context())

    monkeypatch.setattr(stage12_runtime, "run_single_simulation", implementation)
    return getattr(module, function_name)(
        {"role": "baseline"},
        {},
        _observability_context(),
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
    assert module.RESOLVED_BUNDLE.bundle.policyengine_requirement in us_command
    assert module.gcp_secret["args"] == ("stage12-evaluation-gcp-credentials",)
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
        assert local_source[1] == (
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
            "policyengine_stage12_persistence",
        )


def test_v2_image_retains_required_runtime_dependencies() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert (
        "spm-calculator==0.3.1"
        in project["dependency-groups"]["modal-simulation-image"]
    )
    assert "sqlalchemy>=2,<3" in project["dependency-groups"]["modal-simulation-image"]
    assert (
        "psycopg[binary]>=3.2,<4"
        in project["dependency-groups"]["modal-simulation-image"]
    )


def test_v2_app_declares_validation_workers_and_non_http_coordinator(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    functions = {name: options for name, options in module.app.function_calls}

    assert set(functions) == {
        "validate_worker_us",
        "validate_worker_uk",
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "coordinate_report",
    }
    assert functions["run_single_simulation_us"]["image"] is module.us_worker_image
    assert functions["run_single_simulation_uk"]["image"] is module.uk_worker_image
    assert functions["run_single_simulation_us"]["timeout"] == 3000
    assert functions["run_single_simulation_uk"]["timeout"] == 3000
    assert functions["run_single_simulation_us"]["max_containers"] == 10
    assert functions["run_single_simulation_uk"]["max_containers"] == 10
    assert functions["coordinate_report"]["image"] is module.coordinator_image
    assert functions["coordinate_report"]["timeout"] == 4500
    assert functions["coordinate_report"]["max_containers"] == 10
    assert "asgi_app" not in vars(module)


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
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
    assert runtime.contexts == [{"observability_id": OBSERVABILITY_ID}]
    assert runtime.operations[0]["remote_context"]["observability_id"] == (
        OBSERVABILITY_ID
    )
    assert runtime.shutdown_calls == 1


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
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
        raise LookupError("application failure")

    with pytest.raises(LookupError, match="application failure"):
        _invoke(monkeypatch, module, function_name, fail)

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
