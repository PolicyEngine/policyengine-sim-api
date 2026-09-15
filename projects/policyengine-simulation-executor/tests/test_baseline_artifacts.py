"""Runtime read path of the artifact pipeline.

Covers the qualifying predicate (which sims get deterministic ids), the
validate-on-load guard (loads are trusted only when column-complete), and
the class selection in ``_build_simulation``. No model runs, no network:
model versions are SimpleNamespace fakes and the policyengine in-process
simulation cache is swapped per test.
"""

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from policyengine.core import Simulation
from pydantic import PrivateAttr

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
            artifact_kwargs=None,
            plain_kwargs=None,
            id=None,
            id_kwargs=None,
            capability=None,
        )

        class FakeArtifactSimulation:
            def __init__(self, **kwargs):
                recorded.artifact_kwargs = kwargs

        class FakePlainSimulation:
            def __init__(self, **kwargs):
                recorded.plain_kwargs = kwargs

        # Replacing the wrapper's ``Simulation`` is what makes the class
        # choice observable, but it is also the class the canonical
        # capability derivation inspects. Read the installed capability
        # first and serve that answer, so this fixture fakes only the class
        # choice and the resolved selection stays the certified one.
        from policyengine_simulation_executor import spm as executor_spm

        installed_capability = executor_spm.runtime_spm_capability()

        monkeypatch.setattr(ba, "ArtifactBaselineSimulation", FakeArtifactSimulation)
        monkeypatch.setattr(policyengine_core, "Simulation", FakePlainSimulation)
        monkeypatch.setattr(
            executor_spm, "runtime_spm_capability", lambda: installed_capability
        )
        recorded.capability = installed_capability
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
        # A canonical bundle resolves a certified selection for a request
        # that named none, and the simulation is built carrying it.
        assert wired.artifact_kwargs["spm"] == (wired.capability.defaults.model_dump())
        # The predicate must see the request's own facts. A wiring
        # regression (e.g. policy=None passed unconditionally) would hand
        # a REFORM simulation the baseline's deterministic id — and
        # ensure() would then serve the baseline artifact as the reform.
        assert wired.id_kwargs == {
            # The resolved selection is merged in before the id is derived,
            # so two selections cannot share one deterministic baseline id.
            "params": {
                "country": "us",
                "scope": "macro",
                "spm": wired.capability.defaults.model_dump(),
            },
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

    # ``MicrosimulationModelVersion`` declares this as a ClassVar and the
    # canonical implementation branches on it: the US arm reads and writes an
    # SPM receipt inside the h5. These cases are about the artifact guard's
    # hit/miss decisions on real files, so the duck stays a non-US model
    # version and that arm is the native lane's subject.
    country_code = ""

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


SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}


def _spm_receipt(selection=None, *, year="2026"):
    """One calculation receipt, the shape ``spm_provenance()`` returns.

    ``simulation_spm_result`` reads one receipt per simulation and pairs the
    baseline's with the reform's itself, so the wrapper hands back a flat
    receipt rather than a baseline/reform comparison.
    """
    selection = selection or SPM_SELECTION
    return {
        "forecast_id": "test-only",
        "forecast_sha256": selection["forecast_content_sha256"],
        "scenario": selection["scenario"],
        "geography_kind": selection["geography_kind"],
        "runtime_versions": {"policyengine-us": "test-only"},
        "years": {year: {"status": "forecast"}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }


def _installed_wrapper_supports_spm() -> bool:
    return "spm" in Simulation.model_fields and hasattr(Simulation, "spm_provenance")


_INSTALLED_WRAPPER_SUPPORTS_SPM = _installed_wrapper_supports_spm()


class SPMWrapperSimulation(Simulation):
    """Test double for the canonical wrapper's SPM surface.

    The wrapper the canonical bundle installs adds an ``spm`` field, an
    ``spm_config`` holding the selection an artifact was built under, and an
    ``spm_provenance()`` calculation receipt; it restores that metadata on a
    load or a cache hit. This project now pins that wrapper, so the surface
    is real -- ``test_the_double_matches_the_installed_wrapper_surface``
    below checks the double against it and no longer skips. The double
    survives the pin because these cases drive ``ensure()``'s decisions from
    fixtures rather than from a model: it is what lets a test say what a
    load or a cache hit restored, including states a correct wrapper never
    produces.

    This double reproduces that surface's *shape and restore timing*, which
    is all the guard depends on. It is deliberately more permissive than the
    real wrapper, whose own load rejects an artifact built under a different
    selection: the guard is written not to trust the wrapper, so it is
    exercised here as the independent check it is meant to be.

    It is evidence about ``ensure()``'s decisions, never about the wrapper.
    It cannot show that a real ``spm_provenance()`` returns a mapping
    ``SPMProvenance`` accepts, that a real load or cache hit restores the
    artifact's selection at all, or that a real ``storage_id`` separates two
    selections. Those three are the native suite's claims; hermetic green
    here is not wrapper conformance.

    ``storage_id`` here is settable rather than derived from the selection,
    so it stays put while a load rewrites ``spm`` — the double makes no
    claim about how the wrapper computes it, only that the artifact class
    keys the process cache on it. ``test_artifact_keys`` is where the
    derivation itself is checked against the installed wrapper.
    """

    spm: dict | None = None
    spm_receipt: dict | None = None
    # ``spm_config`` and ``storage_id`` are properties on the canonical
    # wrapper, and a property on a base class wins over a field declared
    # here. The double keeps them settable — it exists to control what a
    # load or a cache hit restores — so it overrides the properties rather
    # than shadowing them with fields.
    _spm_config: dict | None = PrivateAttr(default=None)
    _storage_id: str = PrivateAttr(default="")
    _provenance_reads: list = PrivateAttr(default_factory=list)

    @property
    def spm_config(self) -> dict | None:
        return self._spm_config

    @spm_config.setter
    def spm_config(self, value: dict | None) -> None:
        self._spm_config = value

    @property
    def storage_id(self) -> str:
        return self._storage_id

    @storage_id.setter
    def storage_id(self, value: str) -> None:
        self._storage_id = value

    def spm_provenance(self):
        self._provenance_reads.append(deepcopy(self.spm_config))
        return self.spm_receipt

    def ensure(self):
        from policyengine.core.simulation import _cache

        cache_key = self.storage_id or self.id
        cached = _cache.get(cache_key)
        if cached is not None:
            self.output_dataset = cached.output_dataset
            # The restore the guard exists to survive: a cached entry's
            # selection lands on this request's simulation, replacing the
            # one the request asked for.
            self.spm = deepcopy(getattr(cached, "spm", None))
            self.spm_config = deepcopy(getattr(cached, "spm_config", None))
            self.spm_receipt = deepcopy(getattr(cached, "spm_receipt", None))
            return
        try:
            self.tax_benefit_model_version.load(self)
        except Exception:
            self.run()
            self.save()
        # The wrapper files a snapshot, not the live object, so re-pointing
        # this simulation at another selection cannot rewrite the entry an
        # earlier key names.
        _cache.add(cache_key, self.model_copy(deep=False))


class CanonicalSPMSimulation(ba.ArtifactBaselineSimulation, SPMWrapperSimulation):
    """``ArtifactBaselineSimulation`` over a wrapper that supports SPM."""


class SPMModelVersion(FakeModelVersion):
    """Restores an artifact's stored SPM metadata on load, as the wrapper does."""

    def __init__(
        self, *, load_result="complete", stored_config=None, stored_receipt=None
    ):
        super().__init__(load_result=load_result)
        self.stored_config = stored_config
        self.stored_receipt = stored_receipt
        self.spm_at_run = []

    def load(self, simulation):
        super().load(simulation)
        # An artifact carries the selection it was built under, and the
        # wrapper restores it over the requested one.
        simulation.spm = deepcopy(self.stored_config)
        simulation.spm_config = deepcopy(self.stored_config)
        simulation.spm_receipt = deepcopy(self.stored_receipt)

    def run(self, simulation):
        super().run(simulation)
        # A real recompute measures SPM under whatever ``spm`` now holds.
        self.spm_at_run.append(deepcopy(simulation.spm))
        simulation.spm_config = deepcopy(simulation.spm)
        simulation.spm_receipt = _spm_receipt(simulation.spm)


def _make_spm_sim(
    model_version, *, spm=None, sim_id="bl1-spm", storage_id=None, year=2026
):
    selection = SPM_SELECTION if spm is None else spm
    simulation = CanonicalSPMSimulation.model_construct(
        id=sim_id,
        dataset=SimpleNamespace(year=year),
        tax_benefit_model_version=model_version,
        policy=None,
        dynamic=None,
        scoping_strategy=None,
        extra_variables={},
        output_dataset=None,
        spm=deepcopy(SPM_SELECTION) if spm is None else deepcopy(spm),
        spm_receipt=None,
    )
    simulation.storage_id = storage_id or f"{sim_id}-{selection['scenario']}"
    # The wrapper carries the selection it was configured with from
    # construction; a load or cache hit then overwrites it with whatever
    # the artifact was built under, which is exactly what the guard in
    # ``ensure()`` compares against the pre-load value.
    simulation.spm_config = deepcopy(SPM_SELECTION) if spm is None else deepcopy(spm)
    return simulation


class TestEnsureValidatesSPMReceipts:
    """``ensure()``'s receipt validation, driven by fixtures rather than a model.

    Only the opt-in native tests reached this block before, so hermetic CI
    could not tell a working guard from a dead one. These cases fix the
    guard's *decisions*; the native tests remain the only evidence that real
    wrapper receipts have the shape the decisions are made on.
    """

    def test_matching_receipt_loads_as_a_hit(self, fresh_cache):
        model = SPMModelVersion(
            stored_config=deepcopy(SPM_SELECTION), stored_receipt=_spm_receipt()
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_HIT
        assert model.calls == ["load"]

    def test_artifact_built_under_another_selection_recomputes(self, fresh_cache):
        other = {**SPM_SELECTION, "scenario": "zero_real"}
        model = SPMModelVersion(stored_config=other, stored_receipt=_spm_receipt(other))
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]
        # The requested selection is restored before the recompute, so the
        # new artifact is measured under the request, not the artifact.
        assert model.spm_at_run == [SPM_SELECTION]
        assert sim.spm_config == SPM_SELECTION

    def test_artifact_without_a_receipt_recomputes(self, fresh_cache):
        model = SPMModelVersion(
            stored_config=deepcopy(SPM_SELECTION), stored_receipt=None
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]

    def test_malformed_receipt_recomputes(self, fresh_cache):
        malformed = _spm_receipt()
        del malformed["composition_method"]
        model = SPMModelVersion(
            stored_config=deepcopy(SPM_SELECTION), stored_receipt=malformed
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]

    def test_receipt_from_another_forecast_recomputes(self, fresh_cache):
        stale = _spm_receipt()
        stale["forecast_sha256"] = "b" * 64
        model = SPMModelVersion(
            stored_config=deepcopy(SPM_SELECTION), stored_receipt=stale
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]

    def test_receipt_for_another_year_recomputes(self, fresh_cache):
        model = SPMModelVersion(
            stored_config=deepcopy(SPM_SELECTION),
            stored_receipt=_spm_receipt(year="2024"),
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["load", "run", "save"]

    def test_cached_entry_with_an_unusable_receipt_is_revalidated(self, fresh_cache):
        """A cache hit skips the load entirely, so the entry's receipt is
        the only thing standing between the request and someone else's
        output. A tax-only run leaves a receipt with no years under the same
        selection; the guard has to notice and recompute."""
        stale = _make_spm_sim(SPMModelVersion())
        stale.output_dataset = _output(_complete_frames())
        stale.spm_receipt = _spm_receipt(year="2024")
        fresh_cache.add(stale.storage_id, stale)

        model = SPMModelVersion()
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        # Cache hit short-circuits the load; the guard still forces the run.
        assert model.calls == ["run", "save"]

    def test_cached_selection_cannot_replace_the_request(self, fresh_cache):
        """Defense in depth: an entry whose selection disagrees with the key
        it is filed under. A correct wrapper never writes one — the key is
        derived from the selection — but a cache hit restores the entry's
        selection onto this request without revalidating, so the guard
        keeps the request's own selection and recomputes under it."""
        other = {**SPM_SELECTION, "scenario": "zero_real"}
        stale = _make_spm_sim(SPMModelVersion(), spm=other)
        stale.output_dataset = _output(_complete_frames())
        stale.spm_receipt = _spm_receipt(other)
        request = _make_spm_sim(SPMModelVersion())
        fresh_cache.add(request.storage_id, stale)

        model = SPMModelVersion()
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert model.calls == ["run", "save"]
        assert model.spm_at_run == [SPM_SELECTION]
        assert sim.spm_config == SPM_SELECTION

    def test_recompute_replaces_the_cache_with_this_request_s_selection(
        self, fresh_cache
    ):
        other = {**SPM_SELECTION, "scenario": "zero_real"}
        model = SPMModelVersion(stored_config=other, stored_receipt=_spm_receipt(other))
        sim = _make_spm_sim(model)
        sim.ensure()
        assert fresh_cache.get(sim.storage_id).spm_config == SPM_SELECTION

        # The next request in this container hits the replaced entry and
        # validates clean: one recompute for the container, not one each.
        later_model = SPMModelVersion(stored_config=other)
        later = _make_spm_sim(later_model)
        later.ensure()
        assert later.artifact_outcome == ba.OUTCOME_HIT
        assert later_model.calls == []

    def test_missing_columns_are_decided_before_the_receipt(self, fresh_cache):
        """A tax-only artifact can legitimately carry no usable receipt.
        Columns are checked first so the outcome is attributed to the gap
        that actually exists, and the receipt is never consulted."""
        model = SPMModelVersion(
            load_result="incomplete",
            stored_config=deepcopy(SPM_SELECTION),
            stored_receipt=None,
        )
        sim = _make_spm_sim(model)
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_INCOMPLETE
        assert sim._provenance_reads == []

    def test_a_request_without_a_selection_never_validates_receipts(self, fresh_cache):
        """The legacy path: no selection, so a complete artifact is a hit
        even though the wrapper surface exists and holds no receipt."""
        model = SPMModelVersion(stored_config=None, stored_receipt=None)
        sim = _make_spm_sim(model, sim_id="bl1-legacy")
        sim.spm = None
        sim.spm_config = None
        sim.ensure()
        assert sim.artifact_outcome == ba.OUTCOME_HIT
        assert model.calls == ["load"]
        assert sim._provenance_reads == []

    def test_the_cache_is_keyed_by_storage_id_not_simulation_id(self, fresh_cache):
        """One caller id must not serve two selections.

        The deterministic simulation id is shared by every selection; only
        the storage id separates them, and the artifact class evicts and
        replaces under it. Keyed on the id instead, a national recompute
        would answer the next county request from cache.
        """
        other = {**SPM_SELECTION, "scenario": "zero_real"}
        first_model = SPMModelVersion(
            stored_config=other, stored_receipt=_spm_receipt(other)
        )
        first = _make_spm_sim(first_model)
        first.ensure()
        assert first.artifact_outcome == ba.OUTCOME_INCOMPLETE

        second_model = SPMModelVersion(
            stored_config=other, stored_receipt=_spm_receipt(other)
        )
        second = _make_spm_sim(second_model, spm=other)
        assert second.id == first.id
        assert second.storage_id != first.storage_id
        second.ensure()
        # It read its own artifact rather than the entry the first request
        # left behind, and both entries survive side by side.
        assert second_model.calls == ["load"]
        assert second.artifact_outcome == ba.OUTCOME_HIT
        # The artifact class filed the recompute under the storage id, and
        # nothing was ever filed under the bare simulation id.
        assert fresh_cache.get(first.storage_id).spm_config == SPM_SELECTION
        assert fresh_cache.get(second.storage_id) is not None
        assert fresh_cache.get(first.id) is None

    def test_the_replaced_cache_entry_is_a_snapshot(self, fresh_cache):
        """The replacement is a copy so that re-pointing this simulation at
        another selection and running it again cannot overwrite the output
        the earlier key names."""
        other = {**SPM_SELECTION, "scenario": "zero_real"}
        model = SPMModelVersion(stored_config=other, stored_receipt=_spm_receipt(other))
        sim = _make_spm_sim(model)
        sim.ensure()
        cached = fresh_cache.get(sim.storage_id)
        assert cached is not sim

        # A later request in the same process re-points the live object.
        model.load(sim)
        assert sim.spm_config == other
        assert cached.spm_config == SPM_SELECTION

    @pytest.mark.skipif(
        not _INSTALLED_WRAPPER_SUPPORTS_SPM,
        reason=(
            "The pinned policyengine is pre-canonical; this checks the double "
            "above against the real surface as soon as an SPM-capable wrapper "
            "is pinned, at which point the double should be retired for it."
        ),
    )
    def test_the_double_matches_the_installed_wrapper_surface(self):
        from policyengine.core import Simulation

        assert "spm" in Simulation.model_fields
        assert callable(getattr(Simulation, "spm_provenance", None))
        assert isinstance(
            getattr(type(Simulation), "storage_id", None)
            or Simulation.__dict__.get("storage_id"),
            property,
        )
