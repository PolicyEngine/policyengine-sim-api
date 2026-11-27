"""Declaration tests for the separate Stage 12 Modal application."""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest
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

    def capture_context(self) -> dict:
        # The coordinator passes it on to start a budget window's next years.
        return {}

    @contextmanager
    def operation(self, name, **kwargs):
        self.operations.append({"name": name, **kwargs})
        yield

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _load(monkeypatch):
    install_fake_modal(monkeypatch)
    # The app builds its images in src/modal/v2_images.py.
    sys.modules.pop("src.modal.v2_images", None)
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

    # The US and UK workers pass their own Modal call ID to the runtime, so the
    # stand-in ``modal`` module, which answers None, needs one; a test that set
    # its own keeps it.
    if module.modal.current_function_call_id() is None:
        monkeypatch.setattr(
            module.modal,
            "current_function_call_id",
            lambda: "call-1",
            raising=False,
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
        module.household_image,
    ):
        local_source = next(
            call for call in image.calls if call[0] == "add_local_python_source"
        )
        # The Stage 12 library's stores use the service library's table models.
        assert local_source[1] == (
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
            "policyengine_simulation_persistence",
            "policyengine_stage12_persistence",
        )
        if image is module.household_image:
            continue
        bundle_install = next(call for call in image.calls if call[0] == "run_commands")
        assert bundle_install[2]["secrets"] == [module.hf_secret]
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


def test_v2_images_refresh_ca_certificates_before_installing_packages(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)

    for image in (
        module.us_worker_image,
        module.uk_worker_image,
        module.coordinator_image,
    ):
        assert image.calls[0][0] == "debian_slim"
        assert image.calls[1] == ("apt_install", ("ca-certificates",))
        assert image.calls[2] == (
            "env",
            {"GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": "/etc/ssl/certs/ca-certificates.crt"},
        )
        assert image.calls[3][0] == "uv_sync"


def test_v2_image_retains_required_runtime_dependencies() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    group = project["dependency-groups"]["modal-simulation-image"]
    assert (
        f"policyengine[models]=={get_bundled_package_version('policyengine')}" in group
    )
    assert not any(isinstance(item, dict) for item in group)
    # The Stage 12 stores need the database packages; their ranges are the
    # shared table-model library's, which test_database_dependency_ranges
    # compares, so they aren't repeated here.
    installed_packages = {
        re.split(r"[\[<>=!~;\s]", requirement, maxsplit=1)[0].lower()
        for requirement in group
    }
    assert {"sqlalchemy", "psycopg"} <= installed_packages


def test_v2_static_runtime_file_smoke_uses_exact_uk_worker_image(monkeypatch) -> None:
    module = _load(monkeypatch)
    sys.modules.pop("src.modal.v2_static_runtime_files_smoke", None)
    smoke = importlib.import_module("src.modal.v2_static_runtime_files_smoke")
    functions = dict(smoke.app.function_calls)

    assert functions["smoke_stage12_static_runtime_files"]["image"] is (
        module.uk_worker_image
    )
    # A routed area dataset is private: checking it needs the Hugging Face token.
    assert functions["smoke_stage12_static_runtime_files"]["secrets"] == [
        module.hf_secret
    ]
    monkeypatch.setattr(
        smoke,
        "validate_installed_uk_local_authority_dataset",
        lambda: "lad22",
    )
    assert smoke.smoke_stage12_static_runtime_files() == {
        "names": 378,
        "lad22_codes": 374,
        "lad23_codes": 361,
        "installed_dataset_boundary_version": "lad22",
    }


def test_v2_static_runtime_file_smoke_checks_the_routed_area_dataset(
    monkeypatch,
) -> None:
    from policyengine_simulation_contract.uk_geography import (
        UKLocalAuthorityBoundaryVersion,
        UKLocalAuthorityMetadata,
    )

    _load(monkeypatch)
    sys.modules.pop("src.modal.v2_static_runtime_files_smoke", None)
    smoke = importlib.import_module("src.modal.v2_static_runtime_files_smoke")
    installed = "/opt/policyengine/stage12-data/microcosm_uk_2024_25.h5"
    routed = "/opt/policyengine/stage12-data/microcosm_uk_2024_25_dense.h5"
    checked: list[str] = []
    monkeypatch.setattr(
        smoke, "resolve_local_bundle_dataset_path", lambda _country, _data: installed
    )
    monkeypatch.setattr(
        smoke,
        "uk_area_hdf_dataset_path",
        lambda path: routed if path == installed else pytest.fail(path),
    )
    monkeypatch.setattr(
        smoke,
        "detect_uk_area_metadata_from_hdf",
        lambda path: (
            checked.append(path)
            or UKLocalAuthorityMetadata(
                boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
            )
        ),
    )

    assert smoke.validate_installed_uk_local_authority_dataset() == "lad23"
    assert checked == [routed]


@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_v2_static_runtime_file_smoke_requires_every_area_output_column(
    monkeypatch, tmp_path, missing
) -> None:
    import pandas as pd

    _load(monkeypatch)
    sys.modules.pop("src.modal.v2_static_runtime_files_smoke", None)
    smoke = importlib.import_module("src.modal.v2_static_runtime_files_smoke")
    installed = str(tmp_path / "microcosm_uk_2024_25.h5")
    routed = str(tmp_path / "microcosm_uk_2024_25_dense.h5")
    household = {
        "household_id": [1, 2],
        "constituency_code_oa": ["E14001063", "W07000112"],
        "la_code_oa": ["E06000063", "W06000001"],
        "region_code_oa": ["SOUTH_EAST", "WALES"],
        "source_household_id": [1, 2],
    }
    del household[missing]
    pd.DataFrame(household).to_hdf(
        routed, key="household", format="table", data_columns=True
    )
    monkeypatch.setattr(
        smoke, "resolve_local_bundle_dataset_path", lambda _country, _data: installed
    )
    monkeypatch.setattr(
        smoke,
        "uk_area_hdf_dataset_path",
        lambda path: routed if path == installed else pytest.fail(path),
    )

    with pytest.raises(ValueError, match=f"has no {missing} column"):
        smoke.validate_installed_uk_local_authority_dataset()


def test_only_us_worker_image_fetches_stage12_cache(monkeypatch) -> None:
    module = _load(monkeypatch)

    assert any(call[0] == "run_function" for call in module.us_worker_image.calls)
    assert not any(call[0] == "run_function" for call in module.uk_worker_image.calls)
    assert not any(call[0] == "run_function" for call in module.coordinator_image.calls)


def test_stage12_cache_fetch_has_room_for_the_state_datasets(monkeypatch) -> None:
    module = _load(monkeypatch)

    (fetch,) = [
        call for call in module.us_worker_image.calls if call[0] == "run_function"
    ]
    # The 63 national files plus a file per state for each precomputed year,
    # downloaded and hashed one after another; the main path allows the same.
    assert fetch[2]["timeout"] == 3600


def test_v2_app_declares_validation_workers_and_non_http_coordinator(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    functions = dict(module.app.function_calls)

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
    assert functions["run_single_simulation_segment_us"]["max_containers"] == 1000
    assert functions["run_single_simulation_us"]["timeout"] == 3900
    assert functions["run_single_simulation_uk"]["timeout"] == 3000
    assert functions["run_single_simulation_us"]["max_containers"] == 50
    assert functions["run_single_simulation_uk"]["max_containers"] == 50
    assert functions["coordinate_report"]["image"] is module.coordinator_image
    assert functions["coordinate_report"]["timeout"] == 5400
    assert functions["coordinate_report"]["max_containers"] == 25
    assert "asgi_app" not in vars(module)


def test_only_report_run_writers_receive_the_modal_worker_database_secret(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    functions = dict(module.app.function_calls)
    worker_database_secret = {
        "args": ("policyengine-modal-worker-database",),
        "kwargs": {"required_keys": ["MODAL_WORKER_DATABASE_URL"]},
    }

    assert module.modal_worker_database_secret == worker_database_secret
    for writer in (
        "coordinate_report",
        "run_single_simulation_us",
        "run_single_simulation_uk",
    ):
        assert functions[writer]["secrets"] == [
            *module.worker_secrets,
            worker_database_secret,
        ]
    # Segments return their result to their worker, and validation records
    # nothing, so neither holds the database credentials.
    for other in (
        "run_single_simulation_segment_us",
        "validate_worker_us",
        "validate_worker_uk",
    ):
        assert functions[other]["secrets"] == module.worker_secrets


@pytest.mark.parametrize(
    "function_name", ["run_single_simulation_us", "run_single_simulation_uk"]
)
def test_simulation_workers_pass_their_own_modal_call_id(
    monkeypatch, function_name
) -> None:
    module = _load(monkeypatch)
    monkeypatch.setattr(
        module.modal,
        "current_function_call_id",
        lambda: "fc-simulation-worker",
        raising=False,
    )
    received = {}

    def implementation(*_args, **kwargs):
        received.update(kwargs)
        return {"role": "baseline"}

    _invoke(monkeypatch, module, function_name, implementation)

    assert received["modal_call_id"] == "fc-simulation-worker"


@pytest.mark.parametrize(
    ("arguments", "expected_parent"),
    [
        # A detailed-save call names its report run and passes no record.
        (({"report": 1}, {"context": 1}), None),
        # A comparison-path call passes its evaluation record.
        (({"report": 1}, {"context": 1}, {"parent": 1}), {"parent": 1}),
    ],
    ids=["report-run", "comparison"],
)
def test_the_coordinator_takes_an_evaluation_record_only_on_the_comparison_path(
    monkeypatch, arguments, expected_parent
) -> None:
    from policyengine_simulation_executor import stage12_runtime

    module = _load(monkeypatch)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "call-1", raising=False
    )
    received = []

    def implementation(payload, context, parent, **_kwargs):
        received.append((payload, context, parent))
        return {"report_run_id": "run", "status": "succeeded"}

    monkeypatch.setattr(stage12_runtime, "coordinate_report", implementation)

    module.coordinate_report(*arguments, observability_context=None)

    assert received == [({"report": 1}, {"context": 1}, expected_parent)]


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
    # A simulation worker's call sets only its container start and call number
    # (tests/test_v2_container_start.py); the plain functions have no snapshot.
    assert runtime.contexts == (
        []
        if function_name == "coordinate_report"
        else [{"container_start": "no_snapshot", "container_call_number": 1}]
    )
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


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
        "coordinate_report",
    ],
)
def test_stage12_functions_return_the_result_from_the_real_operation_scope(
    monkeypatch,
    real_observability_runtime,
    function_name,
) -> None:
    module = _load(monkeypatch)
    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: real_observability_runtime
    )

    result = _invoke(monkeypatch, module, function_name, lambda *_, **__: {"ok": True})

    assert result == {"ok": True}


@pytest.mark.parametrize(
    "function_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
        "coordinate_report",
    ],
)
def test_stage12_functions_raise_the_application_error_from_the_real_operation_scope(
    monkeypatch,
    real_observability_runtime,
    function_name,
) -> None:
    module = _load(monkeypatch)
    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: real_observability_runtime
    )

    def fail(*_, **__):
        msg = "application failure"
        raise LookupError(msg)

    with pytest.raises(LookupError, match="application failure"):
        _invoke(monkeypatch, module, function_name, fail)


def test_household_functions_run_without_datasets_with_only_the_database_secret(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    functions = dict(module.app.function_calls)
    image_calls = module.household_image.calls

    for country in ("us", "uk"):
        declaration = functions[f"calculate_household_{country}"]
        assert declaration["image"] is module.household_image
        # Only the Modal worker role's database URL, to record the household
        # job a call names; no bucket, Hugging Face or comparison secret.
        assert declaration["secrets"] == [
            {
                "args": ("policyengine-modal-worker-database",),
                "kwargs": {"required_keys": ["MODAL_WORKER_DATABASE_URL"]},
            }
        ]
        assert declaration["cpu"] == 2.0
        # 4 GiB reserved, up to 16 GiB, for large households with axes.
        assert declaration["memory"] == (4096, 16384)
        assert declaration["timeout"] == 900
        assert declaration["retries"] == 0
        assert declaration["max_containers"] == 20
    # The image installs the locked packages and the source, but downloads no
    # dataset or cache, so building it needs no secret.
    assert [call[0] for call in image_calls[:4]] == [
        "debian_slim",
        "apt_install",
        "env",
        "uv_sync",
    ]
    assert not any(call[0] in {"run_commands", "run_function"} for call in image_calls)
    assert not any(
        isinstance(argument, dict) and "secrets" in argument
        for call in image_calls
        for argument in call[1:]
    )
    environment = {
        name: value
        for call in image_calls
        if call[0] == "env"
        for name, value in call[1].items()
    }
    assert "POLICYENGINE_DATA_FOLDER" not in environment
    assert "STAGE12_CACHE_MANIFEST_DIGEST" not in environment
    assert "OTEL_TRACES_EXPORTER" in environment
    local_source = next(
        call for call in image_calls if call[0] == "add_local_python_source"
    )
    assert "policyengine_simulation_executor" in local_source[1]


def _household_payload(country: str) -> dict:
    return {
        "calculation_id": "00000000-0000-4000-8000-0000000000c1",
        "country": country,
        "year": "2026",
        "household": {"people": {"you": {"age": {"2026": 40}}}},
        "policy": {"parameters": {}},
        "bundle": {
            "policyengine_version": "6.7.5",
            "core_package_version": "3.0.0",
            "country_package_name": f"policyengine-{country}",
            "country_package_version": "1.0.0",
            "bundle_manifest_sha256": "a" * 64,
        },
    }


@pytest.mark.parametrize("country", ["us", "uk"])
def test_household_functions_calculate_their_own_country_with_telemetry(
    monkeypatch, country
) -> None:
    from policyengine_simulation_executor import household_calculation

    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    calls: list[dict] = []

    class Outcome:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {"status": "succeeded"}

    def calculate(request, **keywords):
        calls.append({"request": request, **keywords})
        return Outcome()

    monkeypatch.setattr(household_calculation, "calculate_household", calculate)

    result = getattr(module, f"calculate_household_{country}")(
        _household_payload(country),
        observability_context=_observability_context(),
    )

    assert result == {"status": "succeeded"}
    (call,) = calls
    assert call["request"].country == country
    assert call["required_country"] == country
    assert call["worker"].application_name == module.APP_NAME
    assert call["worker"].function_name == f"calculate_household_{country}"
    assert call["dependencies"].runtime is runtime
    (operation,) = runtime.operations
    assert operation["name"] == "v2_household_execution"
    assert operation["attributes"] == {"country": country}
    assert operation["remote_context"]["observability_id"] == OBSERVABILITY_ID
    assert runtime.shutdown_calls == 1


def _refuse_household_jobs(monkeypatch) -> None:
    """Fail the test if a household function records a household job."""
    from policyengine_simulation_executor import stage12_runtime

    def refuse(*_args, **_keywords):
        msg = "a household function given no job recorded one"
        raise AssertionError(msg)

    monkeypatch.setattr(stage12_runtime, "run_household_job", refuse)


@pytest.mark.parametrize("country", ["us", "uk"])
def test_household_functions_record_the_job_their_call_names(
    monkeypatch, country
) -> None:
    from policyengine_simulation_executor import (
        household_calculation,
        stage12_runtime,
    )

    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal,
        "current_function_call_id",
        lambda: "fc-household",
        raising=False,
    )
    calculated: list[dict] = []
    recorded: list[dict] = []
    outcome = object()

    def calculate(request, **keywords):
        calculated.append({"request": request, **keywords})
        return outcome

    def run_household_job(household_job_id, calculate_job, **keywords):
        recorded.append({"household_job_id": household_job_id, **keywords})
        assert calculate_job() is outcome
        return {"status": "succeeded", "recorded": True}

    monkeypatch.setattr(household_calculation, "calculate_household", calculate)
    monkeypatch.setattr(stage12_runtime, "run_household_job", run_household_job)

    result = getattr(module, f"calculate_household_{country}")(
        _household_payload(country),
        observability_context=_observability_context(),
        household_job_id="00000000-0000-4000-8000-00000000a001",
    )

    assert result == {"status": "succeeded", "recorded": True}
    assert recorded == [
        {
            "household_job_id": UUID("00000000-0000-4000-8000-00000000a001"),
            "modal_call_id": "fc-household",
            "runtime": runtime,
        }
    ]
    (call,) = calculated
    assert call["request"].country == country
    assert call["required_country"] == country
    assert call["worker"].function_name == f"calculate_household_{country}"
    # The claim, the calculation and the record are one operation.
    (operation,) = runtime.operations
    assert operation["name"] == "v2_household_execution"
    assert runtime.shutdown_calls == 1


def _household_report_payload(country: str) -> dict:
    """Return a household report run's calculation: a baseline and a reform."""
    reform = {
        **_household_payload(country),
        "calculation_id": "00000000-0000-4000-8000-0000000000c2",
    }
    return {"baseline": _household_payload(country), "reform": reform}


@pytest.mark.parametrize("country", ["us", "uk"])
def test_household_functions_record_the_report_run_their_call_names(
    monkeypatch, country
) -> None:
    from policyengine_simulation_contract.household_report_execution import (
        HouseholdReportRunCalculation,
    )
    from policyengine_simulation_executor import (
        household_calculation,
        stage12_runtime,
    )

    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal,
        "current_function_call_id",
        lambda: "fc-household-report",
        raising=False,
    )
    _refuse_household_jobs(monkeypatch)
    calculated: list[dict] = []
    recorded: list[dict] = []

    def calculate(request, **keywords):
        calculated.append({"request": request, **keywords})
        return request.calculation_id

    def run_household_report(report_run_id, payload, calculate_side, **keywords):
        recorded.append(
            {"report_run_id": report_run_id, "payload": payload, **keywords}
        )
        calculation = HouseholdReportRunCalculation.model_validate(payload)
        outcomes = [calculate_side(request) for _, request in calculation.sides()]
        return {"status": "succeeded", "outcomes": [str(item) for item in outcomes]}

    monkeypatch.setattr(household_calculation, "calculate_household", calculate)
    monkeypatch.setattr(stage12_runtime, "run_household_report", run_household_report)

    result = getattr(module, f"calculate_household_{country}")(
        _household_report_payload(country),
        observability_context=_observability_context(),
        household_report_run_id="00000000-0000-4000-8000-00000000a002",
    )

    assert result == {
        "status": "succeeded",
        "outcomes": [
            "00000000-0000-4000-8000-0000000000c1",
            "00000000-0000-4000-8000-0000000000c2",
        ],
    }
    (report,) = recorded
    assert report["report_run_id"] == UUID("00000000-0000-4000-8000-00000000a002")
    # The payload reaches the recording code as sent, which validates it once
    # the call holds the report run.
    assert report["payload"] == _household_report_payload(country)
    assert report["modal_call_id"] == "fc-household-report"
    assert report["runtime"] is runtime
    assert [call["required_country"] for call in calculated] == [country, country]
    assert all(
        call["worker"].function_name == f"calculate_household_{country}"
        for call in calculated
    )
    # The claims, both calculations and the records are one operation.
    (operation,) = runtime.operations
    assert operation["name"] == "v2_household_execution"
    assert runtime.shutdown_calls == 1


def test_a_household_function_refuses_a_job_and_a_report_run_together(
    monkeypatch,
) -> None:
    from policyengine_simulation_executor import stage12_runtime

    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    _refuse_household_jobs(monkeypatch)

    def refuse(*_args, **_keywords):
        msg = "a call naming a job and a report run recorded one"
        raise AssertionError(msg)

    monkeypatch.setattr(stage12_runtime, "run_household_report", refuse)

    with pytest.raises(ValueError, match="either a household job or a report run"):
        module.calculate_household_us(
            _household_report_payload("us"),
            household_job_id="00000000-0000-4000-8000-00000000a001",
            household_report_run_id="00000000-0000-4000-8000-00000000a002",
        )
    assert runtime.shutdown_calls == 1


def test_household_functions_given_no_job_record_none(monkeypatch) -> None:
    # Release validation calls them with validation households, and no job.
    from policyengine_simulation_executor import household_calculation

    module = _load(monkeypatch)
    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: RecordingRuntime()
    )
    _refuse_household_jobs(monkeypatch)

    class Outcome:
        def model_dump(self, *, mode):
            return {"status": "succeeded"}

    monkeypatch.setattr(
        household_calculation, "calculate_household", lambda *_, **__: Outcome()
    )

    assert module.calculate_household_us(_household_payload("us")) == {
        "status": "succeeded"
    }


def test_household_functions_close_runtime_after_an_error(monkeypatch) -> None:
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    with pytest.raises(ValueError, match="validation error"):
        module.calculate_household_us({"country": "us"})

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
def test_stage12_functions_record_a_preemption_at_warning(
    monkeypatch,
    recorded_observability_runtime,
    function_name,
) -> None:
    # Modal preempts a container by raising KeyboardInterrupt in the running
    # function, then runs the same input again in a new container.
    runtime, written_records = recorded_observability_runtime
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    def preempted(*_, **__):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _invoke(monkeypatch, module, function_name, preempted)

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    assert completion["severity"] == "WARNING"
    assert completion["outcome"] == "interrupted"
    assert [record for record in written_records if record["severity"] == "ERROR"] == []


def test_external_package_values_are_assertions_only(monkeypatch) -> None:
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("STAGE12_EXPECT_US_VERSION", "not-the-bundled-version")
    # src/modal/v2_images.py checks them, next to the bundle it resolves.
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(ValueError, match="does not match"):
        importlib.import_module("src.modal.v2_app")


def test_modal_app_name_cannot_override_bundle_selection(monkeypatch) -> None:
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "operator-selected")
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(RuntimeError, match="does not match"):
        importlib.import_module("src.modal.v2_app")


SIMULATION_RUN_ID = "00000000-0000-4000-8000-00000000b001"
REPORT_RUN_ID = "00000000-0000-4000-8000-00000000b002"
HOUSEHOLD_JOB_ID = "00000000-0000-4000-8000-00000000a001"
HOUSEHOLD_REPORT_RUN_ID = "00000000-0000-4000-8000-00000000a002"
CLIFF_RESULT_ID = "00000000-0000-4000-8000-00000000c001"
OPERATION_RECORD_KEYS = ("database_table", "database_row_id", "modal_call_id")


def _report_run_context() -> dict:
    """Return a detailed-save coordinator call's context naming its report run."""
    return {
        "report_run": {
            "report_run_id": REPORT_RUN_ID,
            "baseline_simulation_run_id": "00000000-0000-4000-8000-00000000b003",
            "reform_simulation_run_id": "00000000-0000-4000-8000-00000000b004",
        }
    }


def _invoke_with_record(
    monkeypatch, module, call_name: str, *, names_record: bool
) -> dict:
    """Call one v2 Modal function, naming its database row when ``names_record``.

    Every application function is replaced with one returning ``{"ok": True}``.
    """
    from policyengine_simulation_executor import household_calculation, stage12_runtime

    def succeed(*_arguments, **_keywords) -> dict:
        return {"ok": True}

    function_name = call_name.removesuffix(":cliffs")
    if function_name in ("run_single_simulation_us", "run_single_simulation_uk"):
        monkeypatch.setattr(stage12_runtime, "run_single_simulation", succeed)
        if call_name.endswith(":cliffs"):
            context = {"cliff_result_id": CLIFF_RESULT_ID}
        else:
            context = {"simulation_run_id": SIMULATION_RUN_ID} if names_record else {}
        return getattr(module, function_name)(
            {"role": "baseline"}, context, observability_context=None
        )
    if call_name == "run_single_simulation_segment_us":

        class SegmentResult:
            def model_dump(self, *, mode):
                return {"ok": True}

        monkeypatch.setattr(
            stage12_runtime, "calculate_segment", lambda *_, **__: SegmentResult()
        )
        keywords = {"simulation_run_id": SIMULATION_RUN_ID} if names_record else {}
        return module.run_single_simulation_segment_us(
            {"simulation": {"role": "baseline"}, "segment_index": 0},
            observability_context=None,
            **keywords,
        )
    if call_name == "coordinate_report":
        monkeypatch.setattr(stage12_runtime, "coordinate_report", succeed)
        monkeypatch.setattr(
            stage12_runtime,
            "coordinate_window_year",
            lambda coordinate, **_keywords: coordinate(),
        )
        context = _report_run_context() if names_record else {}
        return module.coordinate_report({}, context, observability_context=None)

    function_name, record_kind = call_name.split(":")
    country = function_name.rsplit("_", 1)[1]
    monkeypatch.setattr(stage12_runtime, "run_household_job", succeed)
    monkeypatch.setattr(stage12_runtime, "run_household_report", succeed)

    class HouseholdOutcome:
        def model_dump(self, *, mode):
            return {"ok": True}

    monkeypatch.setattr(
        household_calculation,
        "calculate_household",
        lambda *_, **__: HouseholdOutcome(),
    )
    if not names_record:
        return getattr(module, function_name)(
            _household_payload(country), observability_context=None
        )
    if record_kind == "job":
        return getattr(module, function_name)(
            _household_payload(country),
            observability_context=None,
            household_job_id=HOUSEHOLD_JOB_ID,
        )
    return getattr(module, function_name)(
        _household_report_payload(country),
        observability_context=None,
        household_report_run_id=HOUSEHOLD_REPORT_RUN_ID,
    )


def _capture_metric_attributes(monkeypatch) -> list[dict]:
    """Return the list every metric's attributes are appended to, after filtering."""
    from policyengine_observability import otel

    captured: list[dict] = []
    filter_metric_attributes = otel.metric_attributes

    def record_metric_attributes(values, config):
        filtered = filter_metric_attributes(values, config)
        captured.append(filtered)
        return filtered

    monkeypatch.setattr(otel, "metric_attributes", record_metric_attributes)
    return captured


@pytest.mark.parametrize(
    ("context", "expected_row"),
    [
        ({"cliff_result_id": CLIFF_RESULT_ID}, ("cliff_impacts", CLIFF_RESULT_ID)),
        (
            {"simulation_run_id": SIMULATION_RUN_ID},
            ("simulation_runs", SIMULATION_RUN_ID),
        ),
        ({"cliff_result_id": "not-a-uuid"}, None),
        ({}, None),
        ("not-a-context", None),
    ],
    ids=["cliff-result", "simulation-run", "malformed-id", "no-row", "malformed"],
)
def test_a_simulation_workers_record_is_the_row_its_context_names(
    context, expected_row
) -> None:
    from policyengine_simulation_executor import stage12_runtime

    attributes = stage12_runtime.simulation_worker_record_attributes(
        context, "fc-operation"
    )

    assert attributes.pop("modal_call_id") == "fc-operation"
    assert attributes == (
        {}
        if expected_row is None
        else {"database_table": expected_row[0], "database_row_id": expected_row[1]}
    )


@pytest.mark.parametrize(
    ("call_name", "expected_table", "expected_row_id"),
    [
        ("run_single_simulation_us", "simulation_runs", SIMULATION_RUN_ID),
        ("run_single_simulation_uk", "simulation_runs", SIMULATION_RUN_ID),
        ("run_single_simulation_us:cliffs", "cliff_impacts", CLIFF_RESULT_ID),
        ("run_single_simulation_uk:cliffs", "cliff_impacts", CLIFF_RESULT_ID),
        ("run_single_simulation_segment_us", "simulation_runs", SIMULATION_RUN_ID),
        ("coordinate_report", "report_runs", REPORT_RUN_ID),
        ("calculate_household_us:job", "household_jobs", HOUSEHOLD_JOB_ID),
        ("calculate_household_uk:job", "household_jobs", HOUSEHOLD_JOB_ID),
        ("calculate_household_us:report", "report_runs", HOUSEHOLD_REPORT_RUN_ID),
        ("calculate_household_uk:report", "report_runs", HOUSEHOLD_REPORT_RUN_ID),
    ],
)
def test_v2_functions_name_their_database_row_and_modal_call_in_the_operation_record(
    monkeypatch,
    recorded_observability_runtime,
    call_name,
    expected_table,
    expected_row_id,
) -> None:
    # H7 · 1 links an interrupted operation in production's logs to its row.
    runtime, written_records = recorded_observability_runtime
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "fc-operation"
    )
    metric_attributes = _capture_metric_attributes(monkeypatch)

    assert _invoke_with_record(monkeypatch, module, call_name, names_record=True) == {
        "ok": True
    }

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    # Read after the package's attribute filter, which drops a key naming a
    # household, a reform or a payload, among others.
    assert completion["attributes"]["database_table"] == expected_table
    assert completion["attributes"]["database_row_id"] == expected_row_id
    assert completion["attributes"]["modal_call_id"] == "fc-operation"
    # Each distinct ID would make a separate metric series.
    assert metric_attributes
    for attributes in metric_attributes:
        assert not set(OPERATION_RECORD_KEYS) & set(attributes)


@pytest.mark.parametrize(
    "call_name",
    [
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
        "coordinate_report",
        "calculate_household_us:none",
        "calculate_household_uk:none",
    ],
)
def test_v2_functions_naming_no_row_name_only_their_modal_call(
    monkeypatch, recorded_observability_runtime, call_name
) -> None:
    # A comparison-path call, a standalone simulation, or a release
    # validation's household calculation works on no detailed-save row.
    runtime, written_records = recorded_observability_runtime
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "fc-operation"
    )

    _invoke_with_record(monkeypatch, module, call_name, names_record=False)

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    assert completion["attributes"]["modal_call_id"] == "fc-operation"
    assert "database_table" not in completion["attributes"]
    assert "database_row_id" not in completion["attributes"]


def test_a_malformed_simulation_run_id_is_left_out_of_the_operation_record(
    monkeypatch, recorded_observability_runtime
) -> None:
    # The worker refuses the context itself; telemetry names only a UUID.
    from policyengine_simulation_executor import stage12_runtime

    runtime, written_records = recorded_observability_runtime
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "fc-operation"
    )
    monkeypatch.setattr(
        stage12_runtime, "run_single_simulation", lambda *_, **__: {"ok": True}
    )

    module.run_single_simulation_us(
        {"role": "baseline"},
        {"simulation_run_id": "not a run ID"},
        observability_context=None,
    )

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    assert "database_row_id" not in completion["attributes"]
    assert "database_table" not in completion["attributes"]


@pytest.mark.parametrize(
    ("context", "expected_simulation_run_id"),
    [
        ({"simulation_run_id": SIMULATION_RUN_ID}, UUID(SIMULATION_RUN_ID)),
        # A segment's record names simulation runs, so a cliff result isn't one.
        ({"cliff_result_id": CLIFF_RESULT_ID}, None),
    ],
    ids=["simulation-run", "cliff-result"],
)
def test_the_us_simulation_worker_passes_its_simulation_run_to_its_segments(
    monkeypatch, context, expected_simulation_run_id
) -> None:
    from policyengine_simulation_executor import stage12_runtime

    module = _load(monkeypatch)
    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: RecordingRuntime()
    )
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "fc-simulation-worker"
    )
    segmented_runs: list[dict] = []

    def run_segmented_simulation(simulation, **keywords):
        segmented_runs.append({"simulation": simulation, **keywords})
        return "calculated"

    def run_single_simulation(payload, context, **keywords):
        planned_simulation = object()
        return keywords["dependencies"].segmented_calculator(
            planned_simulation, lambda: False
        )

    monkeypatch.setattr(
        stage12_runtime, "run_segmented_simulation", run_segmented_simulation
    )
    monkeypatch.setattr(stage12_runtime, "run_single_simulation", run_single_simulation)

    module.run_single_simulation_us(
        {"role": "baseline"}, context, observability_context=None
    )

    (segmented_run,) = segmented_runs
    assert segmented_run["simulation_run_id"] == expected_simulation_run_id


@pytest.mark.parametrize(
    "function_name", ["run_single_simulation_us", "run_single_simulation_uk"]
)
def test_simulation_workers_close_runtime_when_their_modal_call_id_is_unreadable(
    monkeypatch, function_name
) -> None:
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)

    def unreadable_call_id():
        msg = "Modal call ID is unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(module.modal, "current_function_call_id", unreadable_call_id)

    with pytest.raises(RuntimeError, match="unreadable"):
        getattr(module, function_name)(
            {"role": "baseline"}, {}, observability_context=None
        )

    assert runtime.shutdown_calls == 1
