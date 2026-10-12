"""Worker prevalidation against the installed canonical calculator contracts.

Enabled by the authenticated native qualification harness. These cases stop
before dataset loading or child submission and never run a population job.
"""

import os
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SPM_NATIVE_SMOKE_SOURCE"),
    reason="Requires the installed canonical SPM qualification environment",
)


@pytest.fixture
def forecast():
    from policyengine_simulation_executor.spm import _forecast, runtime_spm_capability

    return _forecast(runtime_spm_capability().defaults.forecast_content_sha256)


def _worker_entrypoint(params, monkeypatch):
    from policyengine_simulation_executor import simulation_runtime
    from src.modal import budget_window_batch

    if "start_year" in params:
        entrypoint = budget_window_batch.run_budget_window_batch_impl
        boundary = Mock(side_effect=AssertionError("Must reject before child setup"))
        monkeypatch.setattr(budget_window_batch, "build_batch_context", boundary)
    else:
        entrypoint = simulation_runtime._run_simulation_impl_core
        boundary = Mock(side_effect=AssertionError("Must reject before dataset lookup"))
        monkeypatch.setattr(simulation_runtime, "_resolve_dataset_reference", boundary)
    return entrypoint, boundary


@pytest.mark.parametrize(
    "period,unavailable_year",
    [
        ({"time_period": "2021"}, 2021),
        ({"time_period": "2036"}, 2036),
        ({"year": "2040"}, 2040),
        ({"start_year": "2021", "window_size": 1}, 2021),
        ({"start_year": "2036", "window_size": 1}, 2036),
        ({"start_year": "2035", "window_size": 2}, 2036),
    ],
)
def test_worker_year_boundary_matches_real_provider(
    forecast, period, unavailable_year, monkeypatch
):
    from policyengine_simulation_contract.spm import SPMInputError, spm_error_detail
    from spm_calculator.policyengine_adapter import PolicyEngineSPMProvider

    provider = PolicyEngineSPMProvider(forecast, geography_kind="national")
    with pytest.raises(ValueError) as provider_error:
        provider.year_metadata(unavailable_year)
    expected = spm_error_detail(provider_error.value)
    assert expected.code == "SPM_YEAR_UNAVAILABLE"
    assert provider.provenance()["years"] == {}
    params = {"country": "us", "spm": {"geography_kind": "national"}, **period}
    entrypoint, boundary = _worker_entrypoint(params, monkeypatch)
    with pytest.raises(SPMInputError) as error:
        entrypoint(params)
    assert error.value.to_dict() == expected.model_dump()
    boundary.assert_not_called()


@pytest.mark.parametrize(
    "period",
    [
        {"time_period": "2024"},
        {"start_year": "2024", "window_size": 2},
    ],
)
def test_worker_scenario_boundary_matches_real_calculator_contract(
    forecast, period, monkeypatch
):
    from policyengine_simulation_contract.spm import SPMInputError, spm_error_detail
    from spm_calculator.axiom_adapter import export_build_spec

    scenario = "scenario-not-in-the-pinned-forecast"
    with pytest.raises(ValueError) as adapter_error:
        export_build_spec(forecast, years=[2024], scenario=scenario)
    assert adapter_error.value.code == "SPM_SCENARIO_UNAVAILABLE"
    expected = spm_error_detail(adapter_error.value)
    assert expected is not None
    params = {
        "country": "us",
        "spm": {"geography_kind": "national", "scenario": scenario},
        **period,
    }
    entrypoint, boundary = _worker_entrypoint(params, monkeypatch)
    with pytest.raises(SPMInputError) as error:
        entrypoint(params)
    assert error.value.to_dict() == expected.model_dump()
    boundary.assert_not_called()


def test_valid_worker_prevalidation_does_not_evaluate_amounts(forecast, monkeypatch):
    from policyengine_simulation_executor.spm import normalize_runtime_spm
    from spm_calculator.policyengine_adapter import PolicyEngineSPMProvider

    calculate = Mock(side_effect=AssertionError("Prevalidation is not measurement"))
    monkeypatch.setattr(PolicyEngineSPMProvider, "calculate_unit", calculate)
    resolved = normalize_runtime_spm(
        {
            "country": "us",
            "start_year": "2034",
            "window_size": 2,
            "spm": {"geography_kind": "national"},
        }
    )
    assert resolved["geography_kind"] == "national"
    provider = PolicyEngineSPMProvider(
        forecast=forecast,
        geography_kind=resolved["geography_kind"],
        scenario=resolved["scenario"],
        as_of=resolved["as_of"],
    )
    assert provider.provenance()["years"] == {}
    calculate.assert_not_called()


@pytest.mark.parametrize(
    "selection",
    [{"as_of": "2020-01-01"}, {"county_vintage": "2010"}],
)
def test_other_invalid_settings_keep_the_settings_code(selection):
    from policyengine_simulation_contract.spm import SPMInputError
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    with pytest.raises(SPMInputError) as error:
        normalize_runtime_spm(
            {
                "country": "us",
                "time_period": "2024",
                "spm": {"geography_kind": "national", **selection},
            }
        )
    assert error.value.code == "SPM_SETTINGS_INVALID"
