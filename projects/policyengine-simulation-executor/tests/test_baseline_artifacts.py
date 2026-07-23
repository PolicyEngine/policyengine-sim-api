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

    def test_stale_cache_entry_is_replaced_after_recompute(self, fresh_cache):
        """After a recompute that started from a cache hit on a DIFFERENT
        (stale) object, the cache must hold the recomputed simulation.
        LRUCache.add alone keeps the old value for an existing key, which
        would make this request's second ensure() clobber the fresh output
        and run the full model twice."""
        stale = _make_sim(FakeModelVersion())
        stale.output_dataset = _output(_incomplete_frames())
        fresh_cache.add("bl1-test", stale)

        model = FakeModelVersion()
        sim = _make_sim(model)
        sim.ensure()
        assert fresh_cache.get("bl1-test") is sim

        # The request's second ensure() self-hits the completed entry:
        # no second full run.
        sim.ensure()
        assert model.calls == ["run", "save"]
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE

    def test_container_converges_after_stale_recompute(self, fresh_cache):
        stale = _make_sim(FakeModelVersion())
        stale.output_dataset = _output(_incomplete_frames())
        fresh_cache.add("bl1-test", stale)

        _make_sim(FakeModelVersion()).ensure()

        # A later request hits the enriched entry: zero model work.
        later_model = FakeModelVersion()
        later = _make_sim(later_model)
        later.ensure()
        assert later.artifact_outcome == ba.OUTCOME_HIT
        assert later_model.calls == []

    def test_loaded_output_without_data_recomputes(self, fresh_cache):
        model = FakeModelVersion()
        model.load = lambda simulation: setattr(  # type: ignore[method-assign]
            simulation, "output_dataset", SimpleNamespace(data=None)
        )
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE

    def test_second_ensure_keeps_a_genuine_miss(self, fresh_cache):
        """Every request ensures the baseline twice (analysis + deciles).
        The second call self-hits the in-process cache with a complete
        column set — it must not overwrite the miss the request actually
        experienced (the rollout metric depends on it)."""
        model = FakeModelVersion(load_result="absent")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_MISS
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_MISS
        # The second ensure() was a pure cache hit: no extra model work.
        assert model.calls == ["load", "run", "save"]

    def test_second_ensure_keeps_an_incomplete_outcome(self, fresh_cache):
        model = FakeModelVersion(load_result="incomplete")
        sim = _make_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]


class TestBuildSimulationSelection:
    @pytest.fixture
    def wired(self, monkeypatch):
        from policyengine import core as policyengine_core

        from policyengine_simulation_executor import simulation_runtime as sr

        recorded = SimpleNamespace(
            artifact_kwargs=None, plain_kwargs=None, id=None, id_kwargs=None
        )

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

        def fake_deterministic_id(params, **kwargs):
            recorded.id_kwargs = {"params": params, **kwargs}
            return recorded.id

        monkeypatch.setattr(ba, "deterministic_baseline_id", fake_deterministic_id)
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
        # The predicate must see the request's own facts. A wiring
        # regression (e.g. policy=None passed unconditionally) would hand
        # a REFORM simulation the baseline's deterministic id — and
        # ensure() would then serve the baseline artifact as the reform.
        assert wired.id_kwargs == {
            "params": {"country": "us", "scope": "macro"},
            "country": "us",
            "policy": None,
            "region_code": "us",
            "scoping_strategy": None,
            "year": sr.DEFAULT_YEAR,
        }

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
        # The reform's own policy reaches the predicate (which returns a
        # random id for it) — not a hardwired None.
        assert wired.id_kwargs["policy"] == {"gov.x": 1}


class TestIdentityErrorContract:
    """The stated split: the writer path fails loud, the reader degrades."""

    def _kwargs(self):
        return dict(
            params={"scope": "macro"},
            country="us",
            policy=None,
            region_code="us",
            scoping_strategy=None,
            year=2026,
        )

    def test_qualifying_identity_propagates_collection_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("receipt unreadable")

        monkeypatch.setattr(ak, "collect_baseline_identity", boom)
        with pytest.raises(RuntimeError, match="receipt unreadable"):
            ba.qualifying_baseline_identity(**self._kwargs())

    def test_deterministic_id_swallows_collection_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("receipt unreadable")

        monkeypatch.setattr(ak, "collect_baseline_identity", boom)
        assert ba.deterministic_baseline_id(**self._kwargs()) is None


def _year_data(person_columns=None):
    """Minimal real USYearData — six entities, weights included."""
    from microdf import MicroDataFrame

    from policyengine.tax_benefit_models.us.datasets import USYearData

    person = {
        "person_id": [1, 2],
        "person_weight": [1.5, 2.5],
        "age": [30.0, 40.0],
        "employment_income": [1000.0, 2000.0],
    }
    if person_columns is not None:
        person = {k: v for k, v in person.items() if k in person_columns}

    def frame(entity, extra=None):
        data = {f"{entity}_id": [1, 2], f"{entity}_weight": [1.0, 1.0], **(extra or {})}
        return MicroDataFrame(pd.DataFrame(data), weights=f"{entity}_weight")

    return USYearData(
        person=MicroDataFrame(pd.DataFrame(person), weights="person_weight"),
        marital_unit=frame("marital_unit"),
        family=frame("family"),
        spm_unit=frame("spm_unit"),
        tax_unit=frame("tax_unit"),
        household=frame("household", {"household_net_income": [900.0, 1800.0]}),
    )


class DiskModelVersion:
    """Duck model version reusing the REAL load/save implementations, so
    these tests exercise genuine h5 files on disk — including the exception
    type a missing artifact raises — without loading the US tax system."""

    def __init__(self):
        from policyengine.tax_benefit_models.us.datasets import PolicyEngineUSDataset

        self._dataset_class = PolicyEngineUSDataset
        self.run_calls = 0

    def resolve_entity_variables(self, simulation):
        return {
            "person": ["age", "employment_income"],
            "household": ["household_net_income"],
        }

    def load(self, simulation):
        from policyengine.tax_benefit_models.common.model_version import (
            MicrosimulationModelVersion,
        )

        MicrosimulationModelVersion.load(self, simulation)

    def save(self, simulation):
        from policyengine.tax_benefit_models.common.model_version import (
            MicrosimulationModelVersion,
        )

        MicrosimulationModelVersion.save(self, simulation)

    def run(self, simulation):
        from policyengine.tax_benefit_models.common.model_version import (
            output_dataset_filepath,
        )

        self.run_calls += 1
        simulation.output_dataset = self._dataset_class(
            id=simulation.id,
            name="output",
            description="output",
            filepath=str(output_dataset_filepath(simulation)),
            year=simulation.dataset.year,
            is_output_dataset=True,
            data=_year_data(),
        )


def _make_disk_sim(model, tmp_path, sim_id):
    dataset = model._dataset_class(
        name="input",
        description="input",
        filepath=str(tmp_path / "populace_year_2026.h5"),
        year=2026,
        data=_year_data(),
    )
    return ba.ArtifactBaselineSimulation.model_construct(
        id=sim_id,
        dataset=dataset,
        tax_benefit_model_version=model,
        policy=None,
        dynamic=None,
        scoping_strategy=None,
        extra_variables={},
        output_dataset=None,
    )


class TestArtifactDiskRoundTrip:
    """The guard against real h5 files, through the real load/save code.

    Locks in what was only verified by hand before: a missing artifact
    raises FileNotFoundError end-to-end (the clean miss branch, no
    catch-all warning), a saved artifact loads as a hit with weight
    columns intact, and an on-disk artifact missing a requested column
    triggers the recompute-and-overwrite path.
    """

    def test_missing_artifact_is_a_clean_miss_that_saves(self, fresh_cache, tmp_path):
        model = DiskModelVersion()
        sim = _make_disk_sim(model, tmp_path, "bl1-roundtrip-miss")
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_MISS
        assert model.run_calls == 1
        assert (tmp_path / "bl1-roundtrip-miss.h5").exists()

    def test_saved_artifact_loads_as_hit_with_weights(self, fresh_cache, tmp_path):
        model = DiskModelVersion()
        model._dataset_class(
            name="artifact",
            description="artifact",
            filepath=str(tmp_path / "bl1-roundtrip-hit.h5"),
            year=2026,
            data=_year_data(),
        ).save()

        sim = _make_disk_sim(model, tmp_path, "bl1-roundtrip-hit")
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_HIT
        assert model.run_calls == 0
        person = sim.output_dataset.data.person
        assert list(person["age"]) == [30.0, 40.0]
        assert "person_weight" in person.columns

    def test_incomplete_disk_artifact_recomputes_and_overwrites(
        self, fresh_cache, tmp_path
    ):
        model = DiskModelVersion()
        model._dataset_class(
            name="artifact",
            description="artifact",
            filepath=str(tmp_path / "bl1-roundtrip-gap.h5"),
            year=2026,
            data=_year_data(person_columns={"person_id", "person_weight", "age"}),
        ).save()

        sim = _make_disk_sim(model, tmp_path, "bl1-roundtrip-gap")
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.run_calls == 1
        reloaded = model._dataset_class(
            name="check",
            description="check",
            filepath=str(tmp_path / "bl1-roundtrip-gap.h5"),
            year=2026,
        )
        assert "employment_income" in reloaded.data.person.columns
