"""Contract tests for the live synchronous simulation FastAPI app."""

from importlib import import_module
from pathlib import Path

from fastapi.testclient import TestClient

from fixtures.test_simulation_api_contracts import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_executor.main import app


PACKAGED_RUNTIME_MODULES = (
    "policyengine_simulation_executor.compat_models",
    "policyengine_simulation_executor.hf_dataset",
    "policyengine_simulation_executor.release_bundle",
    "policyengine_simulation_executor.simulation",
    "policyengine_simulation_executor.simulation_macro_output",
    "policyengine_simulation_executor.simulation_output_budget",
    "policyengine_simulation_executor.simulation_output_builder",
    "policyengine_simulation_executor.simulation_output_cliff",
    "policyengine_simulation_executor.simulation_output_common",
    "policyengine_simulation_executor.simulation_output_distribution",
    "policyengine_simulation_executor.simulation_output_geographic",
    "policyengine_simulation_executor.simulation_output_inequality",
    "policyengine_simulation_executor.simulation_output_labor",
    "policyengine_simulation_executor.simulation_output_poverty",
    "policyengine_simulation_executor.simulation_runtime",
    "policyengine_simulation_observability.telemetry",
)


def test_standalone_package_runtime_does_not_import_unpackaged_modal_source():
    for module_name in PACKAGED_RUNTIME_MODULES:
        module = import_module(module_name)
        source = Path(module.__file__).read_text(encoding="utf-8")

        assert "src.modal" not in source


def test_standalone_simulation_openapi_keeps_legacy_schema_names():
    spec = app.openapi()
    route = spec["paths"]["/simulate/economy/comparison"]["post"]

    assert route["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SimulationOptions"
    }
    assert route["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EconomyComparison"
    }
    assert (
        "telemetry"
        not in spec["components"]["schemas"]["SimulationOptions"]["properties"]
    )

def test_standalone_simulation_route_returns_legacy_macro_contract(monkeypatch):
    def fake_run_simulation_impl(params):
        assert params == {"country": "us", "reform": {}}
        return CURRENT_SINGLE_YEAR_MACRO_RESULT

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation.run_simulation_impl",
        fake_run_simulation_impl,
    )

    response = TestClient(app).post(
        "/simulate/economy/comparison",
        json={"country": "us", "reform": {}},
    )

    assert response.status_code == 200
    assert response.json() == CURRENT_SINGLE_YEAR_MACRO_RESULT


def test_standalone_simulation_route_forwards_include_cliffs(monkeypatch):
    def fake_run_simulation_impl(params):
        assert params == {
            "country": "us",
            "reform": {},
            "include_cliffs": True,
        }
        return CURRENT_SINGLE_YEAR_MACRO_RESULT

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation.run_simulation_impl",
        fake_run_simulation_impl,
    )

    response = TestClient(app).post(
        "/simulate/economy/comparison",
        json={"country": "us", "reform": {}, "include_cliffs": True},
    )

    assert response.status_code == 200
