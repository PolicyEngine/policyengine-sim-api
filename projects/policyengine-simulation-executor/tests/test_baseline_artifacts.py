"""Runtime read path of the artifact pipeline.

Covers the qualifying predicate (which sims get deterministic ids), the
validate-on-load guard (loads are trusted only when column-complete), and
the class selection in ``_build_simulation``. No model runs, no network:
model versions are SimpleNamespace fakes and the policyengine in-process
simulation cache is swapped per test.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from policyengine_simulation_executor import artifact_keys as ak
from policyengine_simulation_executor import baseline_artifacts as ba


def _region_group():
    from policyengine.core.scoping_strategy import (
        RegionGroupStrategy,
        RowFilterStrategy,
    )

    return RegionGroupStrategy(
        members=[
            RowFilterStrategy(variable_name="state_code", variable_value="CA"),
            RowFilterStrategy(variable_name="state_code", variable_value="WV"),
        ]
    )


class TestDeterministicBaselineId:
    @pytest.fixture
    def collected(self, monkeypatch):
        calls = []

        def fake_collect(country, year, *, region, scope_key):
            calls.append(
                {
                    "country": country,
                    "year": year,
                    "region": region,
                    "scope_key": scope_key,
                }
            )
            return SimpleNamespace(simulation_id="bl1-deadbeefdeadbeef")

        monkeypatch.setattr(ak, "collect_baseline_identity", fake_collect)
        return calls

    def _id(self, collected, **overrides):
        kwargs = dict(
            params={"scope": "macro"},
            country="us",
            policy=None,
            region_code="us",
            scoping_strategy=None,
            year=2026,
        )
        kwargs.update(overrides)
        return ba.deterministic_baseline_id(kwargs.pop("params"), **kwargs)

    def test_national_baseline_qualifies(self, collected):
        assert self._id(collected) == "bl1-deadbeefdeadbeef"
        assert collected == [
            {"country": "us", "year": 2026, "region": "national", "scope_key": None}
        ]

    def test_region_group_qualifies_with_cache_key(self, collected):
        group = _region_group()
        sim_id = self._id(
            collected,
            region_code="region_group/state/ca+state/wv",
            scoping_strategy=group,
        )
        assert sim_id == "bl1-deadbeefdeadbeef"
        assert collected[0]["region"] == "region_group/state/ca+state/wv"
        assert collected[0]["scope_key"] == group.cache_key

    def test_reform_policy_disqualifies(self, collected):
        assert self._id(collected, policy={"gov.x": 1}) is None

    @pytest.mark.parametrize("scope", [None, "", "household"])
    def test_non_macro_scope_disqualifies(self, collected, scope):
        params = {} if scope is None else {"scope": scope}
        assert self._id(collected, params=params) is None

    @pytest.mark.parametrize(
        "params",
        [
            {"scope": "macro", "data": "other_dataset"},
            {"scope": "macro", "data_version": "1.2.3"},
        ],
    )
    def test_custom_data_disqualifies(self, collected, params):
        assert self._id(collected, params=params) is None

    def test_missing_region_code_disqualifies(self, collected):
        assert self._id(collected, region_code=None) is None

    def test_unscoped_non_national_region_disqualifies(self, collected):
        assert self._id(collected, region_code="state/ca") is None

    def test_single_state_scoping_disqualifies(self, collected):
        from policyengine.core.scoping_strategy import RowFilterStrategy

        strategy = RowFilterStrategy(variable_name="state_code", variable_value="CA")
        assert (
            self._id(collected, region_code="state/ca", scoping_strategy=strategy)
            is None
        )

    def test_identity_collection_failure_degrades_to_random_id(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("manifest unavailable")

        monkeypatch.setattr(ak, "collect_baseline_identity", boom)
        assert (
            ba.deterministic_baseline_id(
                {"scope": "macro"},
                country="us",
                policy=None,
                region_code="us",
                scoping_strategy=None,
                year=2026,
            )
            is None
        )


def _complete_frames():
    return {"person": pd.DataFrame({"age": [30.0], "income": [1000.0]})}


def _incomplete_frames():
    return {"person": pd.DataFrame({"age": [30.0]})}


def _output(frames):
    return SimpleNamespace(data=SimpleNamespace(entity_data=frames))


class FakeModelVersion:
    """Duck-typed stand-in for a MicrosimulationModelVersion."""

    def __init__(self, *, load_result="complete"):
        self.load_result = load_result
        self.calls = []

    def resolve_entity_variables(self, simulation):
        return {"person": ["age", "income"]}

    def load(self, simulation):
        self.calls.append("load")
        if self.load_result == "absent":
            raise FileNotFoundError("no artifact")
        if self.load_result == "broken":
            raise OSError("corrupt h5")
        frames = (
            _complete_frames()
            if self.load_result == "complete"
            else _incomplete_frames()
        )
        simulation.output_dataset = _output(frames)

    def run(self, simulation):
        self.calls.append("run")
        simulation.output_dataset = _output(_complete_frames())

    def save(self, simulation):
        self.calls.append("save")


@pytest.fixture
def fresh_cache(monkeypatch):
    from policyengine.core import simulation as simulation_module
    from policyengine.core.cache import LRUCache

    cache = LRUCache(max_size=10)
    monkeypatch.setattr(simulation_module, "_cache", cache)
    return cache


def _make_sim(model_version, sim_id="bl1-test"):
    return ba.ArtifactBaselineSimulation.model_construct(
        id=sim_id,
        dataset=None,
        tax_benefit_model_version=model_version,
        policy=None,
        dynamic=None,
        scoping_strategy=None,
        extra_variables={},
        output_dataset=None,
    )


class TestArtifactBaselineSimulation:
    def test_complete_load_is_a_hit(self, fresh_cache):
        model = FakeModelVersion(load_result="complete")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_HIT
        assert model.calls == ["load"]

    def test_absent_artifact_is_a_miss_that_computes(self, fresh_cache):
        model = FakeModelVersion(load_result="absent")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_MISS
        assert model.calls == ["load", "run", "save"]

    def test_broken_load_degrades_to_miss(self, fresh_cache):
        model = FakeModelVersion(load_result="broken")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_MISS
        assert model.calls == ["load", "run", "save"]

    def test_incomplete_load_recomputes(self, fresh_cache):
        model = FakeModelVersion(load_result="incomplete")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]
        # The guard's recompute produced the full column set.
        assert "income" in sim.output_dataset.data.entity_data["person"].columns

    def test_recompute_replaces_cache_entry(self, fresh_cache):
        model = FakeModelVersion(load_result="incomplete")
        first = _make_sim(model)
        first.ensure()
        assert first.artifact_outcome == ba.OUTCOME_INCOMPLETE

        # A second request in the same process must reuse the completed
        # output from the cache — one recompute, not one per request.
        second = _make_sim(model)
        second.ensure()
        assert second.artifact_outcome == ba.OUTCOME_HIT
        assert model.calls == ["load", "run", "save"]

    def test_cache_hit_is_validated_too(self, fresh_cache):
        stale = _make_sim(FakeModelVersion())
        stale.output_dataset = _output(_incomplete_frames())
        fresh_cache.add("bl1-test", stale)

        model = FakeModelVersion()
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        # Cache hit short-circuits load; the guard still forces the run.
        assert model.calls == ["run", "save"]

    def test_loaded_output_without_data_recomputes(self, fresh_cache):
        model = FakeModelVersion()
        model.load = lambda simulation: setattr(  # type: ignore[method-assign]
            simulation, "output_dataset", SimpleNamespace(data=None)
        )
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE


class TestBuildSimulationSelection:
    @pytest.fixture
    def wired(self, monkeypatch):
        from policyengine import core as policyengine_core

        from policyengine_simulation_executor import simulation_runtime as sr

        recorded = SimpleNamespace(artifact_kwargs=None, plain_kwargs=None, id=None)

        class FakeArtifactSimulation:
            def __init__(self, **kwargs):
                recorded.artifact_kwargs = kwargs

        class FakePlainSimulation:
            def __init__(self, **kwargs):
                recorded.plain_kwargs = kwargs

        monkeypatch.setattr(ba, "ArtifactBaselineSimulation", FakeArtifactSimulation)
        monkeypatch.setattr(policyengine_core, "Simulation", FakePlainSimulation)
        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda country: SimpleNamespace(model=SimpleNamespace(id="us-model")),
        )
        monkeypatch.setattr(
            ba,
            "deterministic_baseline_id",
            lambda params, **kwargs: recorded.id,
        )
        return recorded

    def test_qualifying_baseline_uses_artifact_class(self, wired):
        from policyengine_simulation_executor import simulation_runtime as sr

        wired.id = "bl1-deadbeefdeadbeef"
        sr._build_simulation(
            {"country": "us", "scope": "macro"},
            dataset="dataset",
            policy=None,
            scoping_strategy=None,
            region_code="us",
        )
        assert wired.plain_kwargs is None
        assert wired.artifact_kwargs["id"] == "bl1-deadbeefdeadbeef"
        assert wired.artifact_kwargs["dataset"] == "dataset"

    def test_non_qualifying_uses_plain_simulation(self, wired):
        from policyengine_simulation_executor import simulation_runtime as sr

        wired.id = None
        sr._build_simulation(
            {"country": "us", "scope": "macro"},
            dataset="dataset",
            policy={"gov.x": 1},
            scoping_strategy=None,
            region_code="us",
        )
        assert wired.artifact_kwargs is None
        assert wired.plain_kwargs["policy"] == {"gov.x": 1}
        assert "id" not in wired.plain_kwargs
