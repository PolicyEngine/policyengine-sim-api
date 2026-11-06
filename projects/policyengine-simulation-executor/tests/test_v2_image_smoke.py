"""Tests for the pre-merge smoke of every v2 Modal image."""

from __future__ import annotations

import importlib
import sys

import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionResult,
)


@pytest.fixture
def v2_modules(monkeypatch):
    """Import the v2 application and its image smoke under the fake Modal."""
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_app", None)
    sys.modules.pop("src.modal.v2_image_smoke", None)
    v2_app = importlib.import_module("src.modal.v2_app")
    return v2_app, importlib.import_module("src.modal.v2_image_smoke")


def test_the_smoke_builds_every_v2_image(v2_modules) -> None:
    v2_app, smoke = v2_modules
    functions = dict(smoke.app.function_calls)

    assert functions["smoke_v2_coordinator_image"]["image"] is v2_app.coordinator_image
    assert functions["smoke_v2_uk_worker_image"]["image"] is v2_app.uk_worker_image
    assert functions["smoke_v2_household_image"]["image"] is v2_app.household_image
    # The US worker image without its last data layer, the Stage 12 cache,
    # which only a deployment can fetch: the deployment's precompute writes it
    # with the deployment's Cloud Storage credential.
    us_image = functions["smoke_v2_us_worker_image"]["image"]
    data_install = next(call for call in us_image.calls if call[0] == "run_commands")
    assert "--country us" in data_install[1][0]
    assert "--country uk" not in data_install[1][0]
    assert not any(call[0] == "run_function" for call in us_image.calls)
    assert [call[0] for call in us_image.calls] == [
        call[0] for call in v2_app.us_worker_image.calls if call[0] != "run_function"
    ]
    assert all(
        declaration.get("secrets") in (None, []) for declaration in functions.values()
    )


@pytest.mark.parametrize(
    "function_name",
    [
        "smoke_v2_coordinator_image",
        "smoke_v2_uk_worker_image",
        "smoke_v2_us_worker_image",
    ],
)
def test_report_image_smokes_import_the_v2_entry_points(
    v2_modules, function_name
) -> None:
    _, smoke = v2_modules

    report = getattr(smoke, function_name)()

    assert report["modules"] == list(smoke.REPORT_ENTRY_POINT_MODULES)
    assert {
        "src.modal.v2_app",
        "policyengine_simulation_executor.stage12_runtime",
        "policyengine_simulation_executor.stage12_worker_validation",
        # Imported only while handling a request.
        "policyengine_simulation_executor.simulation_runtime",
        "microdf",
    } <= set(report["modules"])
    assert report["policyengine_version"]


def test_household_image_smoke_calculates_each_validation_household(
    monkeypatch, v2_modules
) -> None:
    from policyengine_simulation_executor import household_calculation

    _, smoke = v2_modules
    calculated: list[str] = []

    def calculate(request, *, worker, required_country):
        assert required_country == request.country
        calculated.append(request.country)
        situation = request.household.to_situation()
        net_income = situation["households"]["your household"]["household_net_income"]
        for period in net_income:
            net_income[period] = 20_000.0
        return HouseholdExecutionResult.model_validate(
            {
                "calculation_id": request.calculation_id,
                "output": {
                    "country": request.country,
                    "bundle": request.bundle,
                    "results": [{"policy_key": "baseline", "household": situation}],
                },
                "worker": worker,
            }
        )

    monkeypatch.setattr(household_calculation, "calculate_household", calculate)

    report = smoke.smoke_v2_household_image()

    assert calculated == ["us", "uk"]
    assert report["household_net_income"] == {"us": 20_000.0, "uk": 20_000.0}
    assert report["modules"] == [
        "src.modal.v2_app",
        "policyengine_simulation_executor.household_calculation",
        "policyengine_simulation_executor.household_validation",
    ]


def test_the_household_image_smoke_fails_when_a_calculation_fails(
    monkeypatch, v2_modules
) -> None:
    from policyengine_simulation_executor import household_calculation

    _, smoke = v2_modules

    def fail(request, *, worker, required_country):
        msg = "model exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(household_calculation, "calculate_household", fail)

    with pytest.raises(RuntimeError, match="model exploded"):
        smoke.smoke_v2_household_image()
