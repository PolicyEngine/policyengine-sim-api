"""Tests for how a Stage 12 simulation loads its dataset and region."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationRole,
)
from stage12_fake_stores import simulation_frames
from stage12_runtime_support import (
    bundle_provenance,
    planned_simulation,
    run_until_dataset_loaded,
)


def test_calculator_uses_the_current_dataset_selection_contract(monkeypatch) -> None:
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        RegionResolution,
    )
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    country_module = object()
    region = RegionResolution(code="us")
    selection = DatasetSelection(
        name="populace_us_2024", uri=bundle_provenance().dataset.uri, is_default=True
    )
    dataset = object()
    received: dict[str, object] = {}

    class ModelVersion:
        def resolve_entity_variables(self, _simulation):
            return {
                entity: list(frame.columns)
                for entity, frame in simulation_frames().items()
            }

    class Model:
        spm_config = None
        extra_variables: ClassVar[dict[str, list[str]]] = {}
        tax_benefit_model_version = ModelVersion()
        output_dataset = type(
            "OutputDataset",
            (),
            {"data": type("OutputData", (), {"entity_data": simulation_frames()})()},
        )()

        def ensure(self) -> None:
            received["ensured"] = True
            received["extras_at_ensure"] = self.extra_variables.copy()

    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(
        simulation_runtime,
        "setup_gcp_credentials",
        nullcontext,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "resolve_region",
        lambda **_kwargs: region,
    )

    def resolve_dataset(params, *, region_resolution):
        received["params"] = params
        received["region"] = region_resolution
        return selection

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        received["load_selection"] = selection
        received["load_country_module"] = country_module
        received["load_data_folder"] = data_folder
        return dataset

    def build_simulation(_params, *, resolved, **_kwargs):
        received["build_dataset"] = resolved.dataset
        received["build_selection"] = resolved.dataset_selection
        return Model()

    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        resolve_dataset,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(worker, "build_stage12_simulation", build_simulation)

    result = worker.calculate_simulation_frames(
        planned_simulation(SimulationRole.BASELINE)
    )

    assert "data" not in received["params"]
    assert "data_version" not in received["params"]
    assert received["region"] is region
    assert received["load_selection"] is selection
    assert received["load_country_module"] is country_module
    assert received["load_data_folder"] is None
    assert received["build_dataset"] is dataset
    assert received["build_selection"] is selection
    assert received["ensured"] is True
    assert received["extras_at_ensure"] == {"household": [], "person": []}
    assert set(result.frames) == {"household", "person"}


_UK_LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"
_UK_LOCAL_AREA_URI = "hf://policyengine/uk/microcosm_uk_2024_25_dense.h5@revision"


def _uk_area_planned_simulation(region_code: str) -> PlannedSimulationExecutionInput:
    """Return a planned UK area simulation whose provenance names the dense file."""
    payload = planned_simulation(SimulationRole.BASELINE).model_dump(mode="json")
    payload["geography"] = {"country": "uk", "region": region_code}
    payload["output_plan"]["country"] = "uk"
    payload["bundle"]["country_package_name"] = "policyengine-uk"
    payload["bundle"]["country_package_version"] = "2.132.6"
    payload["bundle"]["dataset"]["identity"] = _UK_LOCAL_AREA_DATASET
    payload["bundle"]["dataset"]["uri"] = _UK_LOCAL_AREA_URI
    payload["population"]["artifact"]["uri"] = _UK_LOCAL_AREA_URI
    return PlannedSimulationExecutionInput.model_validate(payload)


def _uk_area_household(*, without: str) -> pd.DataFrame:
    household = {
        "constituency_code_oa": ["E14001063", "W07000112"],
        "la_code_oa": ["E06000063", "W06000001"],
        "region_code_oa": ["SOUTH_EAST", "WALES"],
        "source_household_id": [1, 2],
    }
    del household[without]
    return pd.DataFrame(household)


@pytest.mark.parametrize(
    "region_code", ["constituency/E14001063", "local_authority/E06000063"]
)
@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_stage12_area_run_needs_every_column_its_outputs_read_before_simulating(
    monkeypatch, region_code, missing
) -> None:
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        RegionResolution,
    )
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    selection = DatasetSelection(
        name=_UK_LOCAL_AREA_DATASET, uri=_UK_LOCAL_AREA_URI, is_default=False
    )
    dataset = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": _uk_area_household(without=missing)}
        )
    )
    for target, value in {
        "uk_area_regions_routed": lambda: True,
        "routed_uk_area_dataset_name": lambda: _UK_LOCAL_AREA_DATASET,
    }.items():
        monkeypatch.setattr(
            f"policyengine_simulation_executor.uk_local_authority_metadata.{target}",
            value,
        )
    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _country: object()
    )
    monkeypatch.setattr(
        simulation_runtime,
        "resolve_region",
        lambda **_: RegionResolution(
            code=region_code, dataset_reference=_UK_LOCAL_AREA_DATASET
        ),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_load_dataset",
        lambda _params, *, selection, country_module, data_folder=None: dataset,
    )
    monkeypatch.setattr(
        worker,
        "build_stage12_simulation",
        lambda *_args, **_kwargs: pytest.fail("simulated a dataset it cannot use"),
    )

    with pytest.raises(
        ValueError,
        match=rf"'{_UK_LOCAL_AREA_DATASET}', which the bundle routes .* has no "
        rf"{missing} column",
    ):
        worker.calculate_simulation_frames(_uk_area_planned_simulation(region_code))


def _regional_selection():
    from policyengine_simulation_executor.simulation_runtime import DatasetSelection

    return DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev",
        is_default=False,
    )


@pytest.mark.parametrize("region_code", ["state/ut", "congressional_district/UT-01"])
def test_stage12_state_and_district_runs_read_their_states_baked_folder(
    monkeypatch, region_code
) -> None:
    from policyengine_simulation_executor.simulation_runtime import (
        RegionResolution,
        resolve_data_folder,
    )

    loaded = run_until_dataset_loaded(
        monkeypatch,
        region=RegionResolution(code=region_code, state_code="UT"),
        selection=_regional_selection(),
        baked_folder="/opt/policyengine/stage12-data/state-datasets/acs/UT",
    )

    assert loaded.loads == [
        {
            "selection": _regional_selection(),
            "data_folder": "/opt/policyengine/stage12-data/state-datasets/acs/UT",
        }
    ]
    assert loaded.lookups == [
        {
            "country": "us",
            "selection": _regional_selection(),
            "state_code": "UT",
            "year": loaded.year,
            "data_folder": resolve_data_folder(),
        }
    ]


def test_stage12_state_run_without_a_baked_file_loads_on_demand(monkeypatch) -> None:
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

    loaded = run_until_dataset_loaded(
        monkeypatch,
        region=RegionResolution(code="state/ut", state_code="UT"),
        selection=_regional_selection(),
        baked_folder=None,
    )

    assert loaded.loads[0]["data_folder"] is None


def _runtime_region_type(country: str, region: str | None) -> str | None:
    """Return the region type the runtime's own region normalisation implies."""
    from policyengine_simulation_executor.region_resolution import (
        _normalise_region_code,
    )

    code = _normalise_region_code(country, region)
    if code == country:
        return "national"
    return code.split("/", 1)[0] if "/" in code else None


@pytest.mark.parametrize(
    ("country", "region"),
    [
        ("us", None),
        ("us", ""),
        ("us", " US "),
        ("us", "uk"),
        ("us", "state/ut"),
        ("us", "State/UT"),
        ("us", "ut"),
        ("us", " ut "),
        ("us", "congressional_district/ut-01"),
        ("us", "place/UT-67000"),
        ("us", "utah"),
        ("uk", "uk"),
        ("uk", "us"),
        ("uk", "country/scotland"),
        ("uk", "scotland"),
        ("uk", "Northern Ireland"),
        ("uk", "constituency/Aldershot"),
        ("uk", "local_authority/E06000001"),
    ],
)
def test_the_contract_reads_region_codes_as_the_runtime_does(country, region) -> None:
    from policyengine_simulation_contract.stage12_bundle import region_type_of

    assert region_type_of(country, region) == _runtime_region_type(country, region)
