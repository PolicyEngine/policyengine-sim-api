"""The steps the simulation workers run before their snapshot and after each restore."""

import importlib
import logging
import random
import sys

import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_executor import spm
from src.modal import country_model_preload


@pytest.fixture
def recorded_preload(monkeypatch):
    """Replace the module imports and the SPM check with recorders."""
    imported_modules: list[str] = []
    spm_checks: list[str] = []
    monkeypatch.setattr(country_model_preload, "import_module", imported_modules.append)
    monkeypatch.setattr(
        spm, "runtime_spm_capability", lambda: spm_checks.append("checked")
    )
    return imported_modules, spm_checks


def test_preload_imports_both_countries_models_then_fills_the_spm_cache(
    recorded_preload,
):
    imported_modules, spm_checks = recorded_preload

    country_model_preload.preload_country_models()

    # Importing the country packages builds each country's tax-benefit
    # system; policyengine.py's model modules wrap those systems.
    assert imported_modules == [
        "policyengine_us",
        "policyengine.tax_benefit_models.us",
        "policyengine_uk",
        "policyengine.tax_benefit_models.uk",
    ]
    assert spm_checks == ["checked"]


def test_a_country_that_fails_to_import_is_left_to_its_own_calls(monkeypatch, caplog):
    imported_modules: list[str] = []
    spm_checks: list[str] = []

    def import_module(module_name):
        if module_name == "policyengine_uk":
            msg = "the UK data release failed its compatibility check"
            raise ValueError(msg)
        imported_modules.append(module_name)

    monkeypatch.setattr(country_model_preload, "import_module", import_module)
    monkeypatch.setattr(
        spm, "runtime_spm_capability", lambda: spm_checks.append("checked")
    )

    with caplog.at_level(logging.WARNING, logger=country_model_preload.__name__):
        country_model_preload.preload_country_models()

    # The US models still load into the snapshot. Each UK call imports the UK
    # models itself and fails as it did before the preload; US calls don't.
    assert imported_modules == [
        "policyengine_us",
        "policyengine.tax_benefit_models.us",
    ]
    assert spm_checks == ["checked"]
    assert "policyengine_uk" in caplog.text
    assert "the UK data release failed its compatibility check" in caplog.text


def test_an_unexpected_spm_failure_is_left_to_each_call(monkeypatch, caplog):
    monkeypatch.setattr(country_model_preload, "import_module", lambda _name: None)

    def broken_forecast():
        msg = "forecast has no years"
        raise IndexError(msg)

    monkeypatch.setattr(spm, "runtime_spm_capability", broken_forecast)

    with caplog.at_level(logging.WARNING, logger=country_model_preload.__name__):
        country_model_preload.preload_country_models()

    assert "forecast has no years" in caplog.text


def test_an_unusable_spm_setup_is_left_to_each_call(monkeypatch, caplog):
    monkeypatch.setattr(country_model_preload, "import_module", lambda _name: None)

    def unusable_spm_setup():
        error_code = "SPM_CONFIGURATION_UNAVAILABLE"
        message = "The installed SPM bundle configuration is unavailable"
        raise SPMInputError(error_code, message)

    monkeypatch.setattr(spm, "runtime_spm_capability", unusable_spm_setup)

    with caplog.at_level(logging.WARNING, logger=country_model_preload.__name__):
        country_model_preload.preload_country_models()

    # The container still starts. The error isn't cached, so every request
    # that needs SPM fails with it, as it did before the preload existed.
    assert "SPM_CONFIGURATION_UNAVAILABLE" in caplog.text


def test_the_snapshot_hook_preloads_without_configuring_observability(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    preloads: list[str] = []
    monkeypatch.setattr(app, "preload_country_models", lambda: preloads.append("ran"))

    def observability_before_the_snapshot(*_args, **_kwargs):
        # A process identity configured here would be shared by every
        # container restored from the snapshot.
        msg = "observability must be configured per call, after the restore"
        raise AssertionError(msg)

    monkeypatch.setattr(
        app, "_configure_modal_observability", observability_before_the_snapshot
    )
    monkeypatch.setattr(
        app, "init_process_observability", observability_before_the_snapshot
    )

    app.SimulationWorker().load_country_models()
    app.SimulationSegmentWorker().load_country_models()

    assert preloads == ["ran", "ran"]
    sys.modules.pop("src.modal.app", None)


def test_each_restored_container_draws_its_own_random_numbers(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    # Every container restored from one snapshot starts from the random
    # state the snapshot captured, and Python reseeds only after a fork.
    snapshot_state = random.getstate()

    app.SimulationWorker().reseed_random_after_restore()
    first_container_span_id = random.getrandbits(64)
    random.setstate(snapshot_state)
    app.SimulationSegmentWorker().reseed_random_after_restore()
    second_container_span_id = random.getrandbits(64)

    # OpenTelemetry draws span IDs this way: without the reseed, the segment
    # children of one run would reuse each other's span IDs in one trace.
    assert first_container_span_id != second_container_span_id
    sys.modules.pop("src.modal.app", None)
