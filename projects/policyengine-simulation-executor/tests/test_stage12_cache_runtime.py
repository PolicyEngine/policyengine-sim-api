"""Unit tests for independent Stage 12 cache identities and loading."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from policyengine.core import Simulation

from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.stage12_cache.keys import (
    collect_baseline_identity,
    collect_dataset_identity,
)
from policyengine_simulation_executor.stage12_cache.runtime import (
    CACHE_HIT,
    CACHE_INCOMPLETE,
    CACHE_MISS,
    Stage12CachedSimulation,
    qualifying_cache_identity,
)
from test_stage12_runtime import _planned_simulation


def _resolved() -> ResolvedStage12Bundle:
    dataset = Stage12Dataset(
        identity="populace_us_2024",
        uri="hf://policyengine/data/populace_us_2024.h5@revision",
        artifact_revision="revision",
        sha256="a" * 64,
        repo_type="dataset",
    )
    country = Stage12CountryBundle(
        country="us",
        country_package_name="policyengine-us",
        country_package_version="1.0.0",
        country_package_requirement="policyengine-us==1.0.0",
        data_package_name="policyengine-us-data",
        data_package_version="2.0.0",
        data_release_version="2.0.0",
        data_artifact_revision="revision",
        default_dataset=dataset.identity,
        default_dataset_uri=dataset.uri,
        datasets=(dataset,),
    )
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version="5.2.0",
            policyengine_requirement="policyengine==5.2.0",
            core_package_version="3.0.0",
            core_package_requirement="policyengine-core==3.0.0",
            countries=(country,),
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _strategy():
    from policyengine.core.scoping_strategy import (
        RegionGroupStrategy,
        RowFilterStrategy,
    )

    return RegionGroupStrategy(
        members=[RowFilterStrategy(variable_name="state_code", variable_value="CA")]
    )


def test_cache_writer_and_reader_share_full_identity() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={"options": {"region_group": ["state/ca"]}}
    )
    resolved = _resolved()
    strategy = _strategy()

    reader = qualifying_cache_identity(
        simulation,
        resolved=resolved,
        dataset_is_default=True,
        scoping_strategy=strategy,
    )
    writer = collect_baseline_identity(
        simulation,
        resolved=resolved,
        region_codes=("state/ca",),
        scope_key=strategy.cache_key,
    )

    assert reader == writer
    assert reader is not None
    assert reader.simulation_id.startswith("stage12-bl1-")
    assert reader.store_path.endswith(f"/{reader.simulation_id}.h5")


def test_dataset_and_baseline_inputs_rotate_cache_identity() -> None:
    resolved = _resolved()
    first = collect_dataset_identity(resolved, country="us", year=2026)
    later = collect_dataset_identity(resolved, country="us", year=2027)
    assert first.digest != later.digest

    simulation = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={"options": {"region_group": ["state/ca"]}}
    )
    ca = collect_baseline_identity(
        simulation,
        resolved=resolved,
        region_codes=("state/ca",),
        scope_key="state/ca",
    )
    ny = collect_baseline_identity(
        simulation,
        resolved=resolved,
        region_codes=("state/ny",),
        scope_key="state/ny",
    )
    first_entity = simulation.output_plan.entities[0]
    changed_plan = collect_baseline_identity(
        simulation.model_copy(
            update={
                "output_plan": simulation.output_plan.model_copy(
                    update={
                        "entities": (
                            first_entity.model_copy(
                                update={
                                    "materialized_variables": (
                                        *first_entity.materialized_variables,
                                        "new_cache_variable",
                                    )
                                }
                            ),
                            *simulation.output_plan.entities[1:],
                        )
                    }
                )
            }
        ),
        resolved=resolved,
        region_codes=("state/ca",),
        scope_key="state/ca",
    )
    assert ca.digest != ny.digest
    assert changed_plan.digest != ca.digest


def test_only_current_law_default_dataset_baseline_qualifies() -> None:
    baseline = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={"options": {"region_group": ["state/ca"]}}
    )
    kwargs = {
        "resolved": _resolved(),
        "dataset_is_default": True,
        "scoping_strategy": _strategy(),
    }
    assert qualifying_cache_identity(baseline, **kwargs) is not None
    assert (
        qualifying_cache_identity(
            baseline.model_copy(update={"role": SimulationRole.REFORM}), **kwargs
        )
        is None
    )
    assert (
        qualifying_cache_identity(
            baseline.model_copy(update={"policy": {"reform": {"2026": 1}}}),
            **kwargs,
        )
        is None
    )
    assert (
        qualifying_cache_identity(baseline, **{**kwargs, "dataset_is_default": False})
        is None
    )


def _cached_model() -> Stage12CachedSimulation:
    model = Stage12CachedSimulation.model_construct(
        id="stage12-bl1-test",
        output_dataset=None,
        dataset=SimpleNamespace(year=2026),
        tax_benefit_model_version=None,
        policy=None,
        scoping_strategy=None,
        extra_variables={},
        dynamic=None,
    )
    model.configure_stage12_cache(
        _planned_simulation(SimulationRole.BASELINE).output_plan
    )
    return model


def _output(complete: bool = True):
    household = {
        "household_id": pd.Series([1], dtype="int64"),
        "household_net_income": pd.Series([1.0], dtype="float32"),
    }
    if not complete:
        household.pop("household_net_income")
    return SimpleNamespace(
        data=SimpleNamespace(
            entity_data={
                "household": pd.DataFrame(household),
                "person": pd.DataFrame(
                    {
                        "age": [30],
                        "household_id": [1],
                        "person_id": [1],
                    }
                ),
            }
        )
    )


def test_cached_simulation_reports_hit_and_miss(monkeypatch) -> None:
    hit = _cached_model()
    monkeypatch.setattr(
        Simulation,
        "ensure",
        lambda self: setattr(self, "output_dataset", _output()),
    )
    hit.ensure()
    assert hit.stage12_cache_outcome == CACHE_HIT

    miss = _cached_model()

    def ensure_by_running(self):
        self.run()

    monkeypatch.setattr(Simulation, "ensure", ensure_by_running)
    monkeypatch.setattr(
        Simulation,
        "run",
        lambda self: setattr(self, "output_dataset", _output()),
    )
    miss.ensure()
    assert miss.stage12_cache_outcome == CACHE_MISS


def test_incomplete_cache_recalculates(monkeypatch) -> None:
    model = _cached_model()
    monkeypatch.setattr(
        Simulation,
        "ensure",
        lambda self: setattr(self, "output_dataset", _output(complete=False)),
    )
    monkeypatch.setattr(
        Simulation,
        "run",
        lambda self: setattr(self, "output_dataset", _output()),
    )
    monkeypatch.setattr(Simulation, "save", lambda self: None)

    model.ensure()

    assert model.stage12_cache_outcome == CACHE_INCOMPLETE
    assert "household_net_income" in model.output_dataset.data.entity_data["household"]
