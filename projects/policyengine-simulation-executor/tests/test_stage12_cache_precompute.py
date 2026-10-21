"""Unit tests for Stage 12 cache planning and orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_cache import precompute
from policyengine_simulation_executor.stage12_cache.models import (
    BaselinePlanEntry,
    CacheBundleIdentity,
    CacheObject,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
)
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from policyengine_simulation_executor.stage12_runtime.simulation import (
    SimulationCalculation,
)
from pydantic import ValidationError


def _bundle() -> CacheBundleIdentity:
    return CacheBundleIdentity(
        bundle_manifest_sha256="a" * 64,
        policyengine_version="5.2.0",
        core_package_version="3.0.0",
        country_package_version="1.0.0",
        data_package_version="2.0.0",
        data_artifact_revision="revision",
        default_dataset="populace_us_2024",
    )


def _plan(*, exists: bool = False, verified: bool | None = None) -> PrecomputePlan:
    datasets = tuple(
        DatasetPlanEntry(
            year=year,
            digest=f"dataset-{year}",
            path=f"datasets/us/dataset-{year}/populace_year_{year}.h5",
            filename=f"populace_year_{year}.h5",
            exists=exists,
        )
        for year in precompute.PRECOMPUTE_YEARS
    )
    baselines = tuple(
        BaselinePlanEntry(
            year=year,
            segment_index=index,
            region_codes=group,
            digest=f"baseline-{year}-{index}",
            path=f"baselines/us/baseline-{year}-{index}/stage12-bl1-{index}.h5",
            filename=f"stage12-bl1-{index}.h5",
            simulation_id=f"stage12-bl1-{index}",
            output_plan_sha256="b" * 64,
            exists=exists,
        )
        for year in precompute.PRECOMPUTE_YEARS
        for index, group in enumerate(US_REGION_GROUPS)
    )
    return PrecomputePlan(
        years=precompute.PRECOMPUTE_YEARS,
        datasets=datasets,
        baselines=baselines,
        bundle=_bundle(),
        partition_sha256="c" * 64,
        # A warm store came from a run whose determinism check passed.
        determinism_verified=exists if verified is None else verified,
    )


REGIONAL_DATASET = "populace_us_2024_acs_local"


def _state_entry(year: int, state_code: str, *, exists: bool) -> StateDatasetPlanEntry:
    digest = f"state-{year}-{state_code}"
    return StateDatasetPlanEntry(
        year=year,
        state_code=state_code,
        region=f"state/{state_code.lower()}",
        dataset=REGIONAL_DATASET,
        digest=digest,
        path=f"state-datasets/us/{digest}/{REGIONAL_DATASET}_year_{year}.h5",
        filename=f"{REGIONAL_DATASET}_year_{year}.h5",
        folder=f"state-datasets/{REGIONAL_DATASET}/{state_code}",
        exists=exists,
    )


def _plan_with_state_datasets(*, exists: bool = False) -> PrecomputePlan:
    return _plan(exists=exists).model_copy(
        update={
            "state_datasets": tuple(
                _state_entry(year, state_code, exists=exists)
                for year in precompute.PRECOMPUTE_YEARS
                for state_code in ("UT", "WY")
            )
        }
    )


class _FakeBlob:
    """A GCS blob over a dict, honouring create-only uploads."""

    def __init__(self, objects: dict, path: str):
        self.objects = objects
        self.path = path
        self.metadata = None

    def exists(self) -> bool:
        return self.path in self.objects

    def reload(self) -> None:
        if self.path not in self.objects:
            raise NotFound(self.path)
        self.metadata = self.objects[self.path]["metadata"]

    def upload_from_string(self, payload, *, content_type, if_generation_match):
        assert if_generation_match == 0
        if self.path in self.objects:
            raise PreconditionFailed(self.path)
        self.objects[self.path] = {"payload": payload, "metadata": self.metadata}

    def download_as_bytes(self) -> bytes:
        if self.path not in self.objects:
            raise NotFound(self.path)
        return self.objects[self.path]["payload"]


class _FakeClient:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def bucket(self, _name: str):
        return SimpleNamespace(blob=lambda path: _FakeBlob(self.objects, path))


def _use_store(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(
        precompute,
        "Stage12CacheStore",
        lambda bucket: Stage12CacheStore(bucket, client=client),
    )


def _store_every_planned_file(client: _FakeClient, plan: PrecomputePlan) -> None:
    for entry in (*plan.datasets, *plan.baselines):
        client.objects[entry.path] = {
            "payload": b"h5",
            "metadata": {"sha256": "f" * 64, "size_bytes": "2"},
        }


def test_plan_shape_matches_v1_year_window_and_twenty_groups() -> None:
    plan = _plan()

    assert plan.years == (2026, 2027, 2025)
    assert len(plan.datasets) == 3
    assert len(plan.baselines) == 60
    assert [entry.year for entry in plan.datasets] == [2026, 2027, 2025]
    assert {
        entry.segment_index for entry in plan.baselines if entry.year == 2026
    } == set(range(20))


def test_strict_plan_rejects_unknown_modal_boundary_fields() -> None:
    payload = _plan().model_dump()
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        PrecomputePlan.model_validate(payload)


def test_work_selection_computes_only_missing_files() -> None:
    cold = precompute.select_work(_plan())
    warm = precompute.select_work(_plan(exists=True))

    assert len(cold.datasets) == 3
    assert len(cold.baselines) == 60
    assert warm.datasets == ()
    assert warm.baselines == ()


def test_work_selection_builds_only_missing_state_datasets() -> None:
    plan = _plan_with_state_datasets()
    stored = plan.state_datasets[0].model_copy(update={"exists": True})
    plan = plan.model_copy(
        update={"state_datasets": (stored, *plan.state_datasets[1:])}
    )

    work = precompute.select_work(plan)

    assert [(entry.year, entry.state_code) for entry in work.state_datasets] == [
        (2026, "WY"),
        (2027, "UT"),
        (2027, "WY"),
        (2025, "UT"),
        (2025, "WY"),
    ]
    assert precompute.select_work(_plan()).state_datasets == ()


def test_plan_with_state_datasets_round_trips_the_modal_boundary() -> None:
    plan = _plan_with_state_datasets()

    assert PrecomputePlan.model_validate(plan.model_dump()) == plan


@pytest.fixture
def stubbed_planning(monkeypatch):
    """Stub everything plan_artifacts_impl reads except the state targets."""
    client = _FakeClient()
    _use_store(monkeypatch, client)
    monkeypatch.setattr(precompute, "load_stage12_bundle", lambda: "resolved")
    monkeypatch.setattr(precompute, "_bundle_identity", _bundle)
    monkeypatch.setattr(
        precompute,
        "collect_dataset_identity",
        lambda _resolved, *, country, year: SimpleNamespace(
            digest=f"dataset-{year}",
            store_path=f"datasets/{country}/dataset-{year}/populace_year_{year}.h5",
            filename=f"populace_year_{year}.h5",
        ),
    )
    monkeypatch.setattr(precompute, "US_REGION_GROUPS", (("state/ca",),))
    monkeypatch.setattr(
        precompute,
        "_baseline_identity",
        lambda year, _group: (
            SimpleNamespace(output_plan=None),
            SimpleNamespace(
                digest=f"baseline-{year}",
                store_path=f"baselines/us/baseline-{year}/stage12-bl1-{year}.h5",
                filename=f"stage12-bl1-{year}.h5",
                simulation_id=f"stage12-bl1-{year}",
            ),
        ),
    )
    monkeypatch.setattr(precompute, "stage12_output_plan_sha256", lambda _: "b" * 64)

    def fake_state_identity(resolved, *, country, year, dataset, **state_filter):
        assert resolved == "resolved"
        digest = (
            f"state-{year}-{state_filter['state_code']}-"
            f"{state_filter['state_fips']}-{state_filter['filter_variable']}"
        )
        return SimpleNamespace(
            digest=digest,
            store_path=f"state-datasets/{country}/{digest}/{dataset}_year_{year}.h5",
            filename=f"{dataset}_year_{year}.h5",
        )

    monkeypatch.setattr(
        precompute, "collect_state_dataset_identity", fake_state_identity
    )
    targets = [
        precompute.StateDatasetTarget(
            region="state/ut",
            state_code="UT",
            state_fips=49,
            filter_variable="state_fips",
            dataset=REGIONAL_DATASET,
        ),
        precompute.StateDatasetTarget(
            region="state/wy",
            state_code="WY",
            state_fips=56,
            filter_variable="state_fips",
            dataset=REGIONAL_DATASET,
        ),
    ]
    monkeypatch.setattr(precompute, "state_dataset_targets", lambda: tuple(targets))
    return SimpleNamespace(client=client, targets=targets)


def test_plan_lists_every_state_dataset_year_major_with_presence(
    stubbed_planning,
) -> None:
    stubbed_planning.client.objects[
        f"state-datasets/us/state-2026-UT-49-state_fips/{REGIONAL_DATASET}_year_2026.h5"
    ] = {"payload": b"h5", "metadata": {"sha256": "f" * 64, "size_bytes": "2"}}

    plan = precompute.plan_artifacts_impl("cache-bucket")

    assert [(entry.year, entry.state_code) for entry in plan.state_datasets] == [
        (2026, "UT"),
        (2026, "WY"),
        (2027, "UT"),
        (2027, "WY"),
        (2025, "UT"),
        (2025, "WY"),
    ]
    assert [entry.exists for entry in plan.state_datasets] == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    first = plan.state_datasets[0]
    assert first.region == "state/ut"
    assert first.dataset == REGIONAL_DATASET
    assert first.folder == f"state-datasets/{REGIONAL_DATASET}/UT"
    assert first.filename == f"{REGIONAL_DATASET}_year_2026.h5"
    assert len(plan.datasets) == 3


def test_states_on_the_default_dataset_add_no_state_datasets(
    stubbed_planning, monkeypatch
) -> None:
    monkeypatch.setattr(precompute, "state_dataset_targets", tuple)

    assert precompute.plan_artifacts_impl("cache-bucket").state_datasets == ()


def test_state_targets_follow_how_a_request_for_the_state_resolves(
    monkeypatch,
) -> None:
    from policyengine.core.scoping_strategy import RowFilterStrategy
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle("us")

    def region(state_code, fips, dataset_path):
        return SimpleNamespace(
            code=f"state/{state_code.lower()}",
            dataset_path=dataset_path,
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=fips
            ),
            parent_code="us",
            state_code=state_code,
        )

    regions = [
        region("WY", 56, bundle.dataset_uris[REGIONAL_DATASET]),
        region("UT", 49, bundle.dataset_uris[REGIONAL_DATASET]),
        region("ID", 16, bundle.default_dataset_uri),
    ]
    by_code = {item.code: item for item in regions}
    country_module = SimpleNamespace(
        model=SimpleNamespace(
            get_region=by_code.get,
            region_registry=SimpleNamespace(
                get_by_type=lambda kind: list(regions) if kind == "state" else []
            ),
        )
    )
    monkeypatch.setattr(precompute, "_country_module", lambda _country: country_module)

    targets = precompute.state_dataset_targets()

    assert [(target.region, target.state_fips) for target in targets] == [
        ("state/ut", 49),
        ("state/wy", 56),
    ]
    assert {target.dataset for target in targets} == {REGIONAL_DATASET}
    assert targets[0].scoping_strategy is by_code["state/ut"].scoping_strategy


class _Call:
    def __init__(self, result, events, label):
        self.result = result
        self.events = events
        self.label = label

    def get(self):
        self.events.append(f"get:{self.label}")
        return self.result


class _Function:
    def __init__(self, callback, events, label):
        self.callback = callback
        self.events = events
        self.label = label
        self.spawn_calls = []
        self.remote_calls = []

    def spawn(self, *args):
        self.spawn_calls.append(args)
        self.events.append(f"spawn:{self.label}")
        return _Call(self.callback(*args), self.events, self.label)

    def remote(self, *args):
        self.remote_calls.append(args)
        self.events.append(f"remote:{self.label}")
        return self.callback(*args)


def test_precompute_finishes_dataset_wave_before_baseline_wave() -> None:
    events = []
    plan = _plan()
    planner = _Function(lambda _: plan.model_dump(), events, "plan")
    dataset = _Function(
        lambda _, entry: {
            "path": entry["path"],
            "content_sha256": "d" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "uploaded": True,
            "build_seconds": 1.0,
        },
        events,
        "dataset",
    )
    baseline = _Function(
        lambda _, entry: {
            "path": entry["path"],
            "content_sha256": "e" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "segment_index": entry["segment_index"],
            "simulation_id": entry["simulation_id"],
            "cache_outcome": "miss",
            "uploaded": True,
            "compute_seconds": 1.0,
        },
        events,
        "baseline",
    )
    verify = _Function(lambda *_: {"equal": True, "differences": []}, events, "verify")
    publish = _Function(lambda *_: "manifest-digest", events, "publish")
    lines = []

    digest = precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(
            plan_artifacts=planner,
            build_dataset=dataset,
            compute_baseline=baseline,
            verify_determinism=verify,
            publish_manifest=publish,
        ),
        echo=lines.append,
    )

    first_baseline_spawn = events.index("spawn:baseline")
    assert all(
        index < first_baseline_spawn
        for index, event in enumerate(events)
        if event == "get:dataset"
    )
    assert len(dataset.spawn_calls) == 3
    assert len(baseline.spawn_calls) == 60
    assert verify.remote_calls == [
        ("cache-bucket", plan.model_dump(), plan.baselines[0].model_dump())
    ]
    assert digest == "manifest-digest"
    assert lines[-1] == "STAGE12_CACHE_MANIFEST_DIGEST=manifest-digest"


def test_warm_verified_precompute_skips_builds_and_determinism_check() -> None:
    events = []
    plan = _plan(exists=True)
    planner = _Function(lambda _: plan.model_dump(), events, "plan")
    unused = _Function(lambda *_: pytest.fail("unexpected build"), events, "build")
    verify = _Function(
        lambda *_: pytest.fail("unexpected validation"), events, "verify"
    )
    publish = _Function(lambda *_: "manifest-digest", events, "publish")

    precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(
            plan_artifacts=planner,
            build_dataset=unused,
            compute_baseline=unused,
            verify_determinism=verify,
            publish_manifest=publish,
        ),
    )

    assert unused.spawn_calls == []
    assert verify.remote_calls == []


def test_failed_determinism_validation_prevents_manifest_publication() -> None:
    events = []
    plan = _plan()
    planner = _Function(lambda _: plan.model_dump(), events, "plan")
    dataset = _Function(
        lambda _, entry: {
            "path": entry["path"],
            "content_sha256": "d" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "uploaded": True,
            "build_seconds": 1.0,
        },
        events,
        "dataset",
    )
    baseline = _Function(
        lambda _, entry: {
            "path": entry["path"],
            "content_sha256": "e" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "segment_index": entry["segment_index"],
            "simulation_id": entry["simulation_id"],
            "cache_outcome": "miss",
            "uploaded": True,
            "compute_seconds": 1.0,
        },
        events,
        "baseline",
    )
    verify = _Function(
        lambda *_: {"equal": False, "differences": ["person.age differs"]},
        events,
        "verify",
    )
    publish = _Function(lambda *_: "unexpected", events, "publish")

    with pytest.raises(SystemExit, match=r"person\.age differs"):
        precompute.run_precompute(
            "cache-bucket",
            functions=precompute.CachePrecomputeFunctions(
                plan_artifacts=planner,
                build_dataset=dataset,
                compute_baseline=baseline,
                verify_determinism=verify,
                publish_manifest=publish,
            ),
        )

    assert publish.remote_calls == []


def test_manifest_records_content_hashes_and_sizes() -> None:
    plan = _plan(exists=True)
    store = SimpleNamespace(
        describe=lambda path: CacheObject(
            path=path,
            content_sha256="f" * 64,
            size_bytes=123,
        )
    )

    manifest = precompute.build_manifest(plan, store)

    assert len(manifest.artifacts) == 63
    assert all(item.content_sha256 == "f" * 64 for item in manifest.artifacts)
    assert all(item.size_bytes == 123 for item in manifest.artifacts)


@pytest.mark.parametrize("passes", [True, False])
def test_a_run_with_no_new_baselines_still_checks_unverified_ones(
    *,
    passes: bool,
) -> None:
    events = []
    plan = _plan(exists=True, verified=False)
    planner = _Function(lambda _: plan.model_dump(), events, "plan")
    unused = _Function(lambda *_: pytest.fail("unexpected build"), events, "build")
    verify = _Function(
        lambda *_: {
            "equal": passes,
            "differences": [] if passes else ["person.age: values differ"],
        },
        events,
        "verify",
    )
    publish = _Function(lambda *_: "manifest-digest", events, "publish")

    def run() -> None:
        precompute.run_precompute(
            "cache-bucket",
            functions=precompute.CachePrecomputeFunctions(
                plan_artifacts=planner,
                build_dataset=unused,
                compute_baseline=unused,
                verify_determinism=verify,
                publish_manifest=publish,
            ),
            echo=lambda _line: None,
        )

    if passes:
        run()
        assert len(publish.remote_calls) == 1
    else:
        with pytest.raises(SystemExit, match=r"person\.age"):
            run()
        assert publish.remote_calls == []
    assert verify.remote_calls == [
        ("cache-bucket", plan.model_dump(), plan.baselines[0].model_dump())
    ]


def test_publish_requires_a_recorded_determinism_pass(monkeypatch) -> None:
    client = _FakeClient()
    _use_store(monkeypatch, client)
    plan = _plan(exists=True, verified=False)
    _store_every_planned_file(client, plan)

    with pytest.raises(RuntimeError, match="have not passed determinism"):
        precompute.publish_manifest_impl("cache-bucket", plan)
    assert not any(path.startswith("manifests/") for path in client.objects)

    Stage12CacheStore("cache-bucket", client=client).write_json(
        precompute.determinism_record_path(plan.baselines),
        precompute.determinism_record(plan.baselines),
    )
    digest = precompute.publish_manifest_impl("cache-bucket", plan)

    assert f"manifests/{digest}.json" in client.objects


def _frames(age: int) -> dict[str, pd.DataFrame]:
    return {"person": pd.DataFrame({"person_id": [1], "age": [age]})}


@pytest.mark.parametrize(
    ("cached_outcome", "fresh_age", "expected"),
    [
        ("hit", 30, "recorded"),
        ("hit", 31, "not recorded"),
        ("miss", 30, "rejected"),
    ],
)
def test_determinism_check_records_only_a_pass_that_loaded_the_stored_file(
    monkeypatch, tmp_path, cached_outcome: str, fresh_age: int, expected: str
) -> None:
    client = _FakeClient()
    _use_store(monkeypatch, client)
    plan = _plan(exists=True, verified=False)
    probe = plan.baselines[0]
    (tmp_path / probe.filename).write_bytes(b"h5")
    monkeypatch.setattr(simulation_runtime, "resolve_data_folder", lambda: tmp_path)
    monkeypatch.setattr(precompute, "_ensure_dataset_file", lambda *_: None)
    monkeypatch.setattr(
        precompute,
        "_scoped_planned_simulation",
        lambda _year, _group, *, reform=False: "reform" if reform else "baseline",
    )
    calculations = {
        "baseline": SimulationCalculation(
            frames=_frames(30), cache_outcome=cached_outcome
        ),
        "reform": SimulationCalculation(frames=_frames(fresh_age)),
    }
    monkeypatch.setattr(
        simulation_module,
        "calculate_simulation_frames",
        lambda planned: calculations[planned],
    )
    record_path = precompute.determinism_record_path(plan.baselines)

    if expected == "rejected":
        with pytest.raises(RuntimeError, match="did not load the stored baseline"):
            precompute.verify_determinism_impl("cache-bucket", plan, probe)
    else:
        verdict = precompute.verify_determinism_impl("cache-bucket", plan, probe)
        assert verdict.equal is (expected == "recorded")
    assert (record_path in client.objects) is (expected == "recorded")


def _use_determinism_calculations(monkeypatch, tmp_path, cached, fresh) -> None:
    monkeypatch.setattr(simulation_runtime, "resolve_data_folder", lambda: tmp_path)
    monkeypatch.setattr(precompute, "_ensure_dataset_file", lambda *_: None)
    monkeypatch.setattr(
        precompute,
        "_scoped_planned_simulation",
        lambda _year, _group, *, reform=False: "reform" if reform else "baseline",
    )
    calculations = {
        "baseline": SimulationCalculation(frames=cached, cache_outcome="hit"),
        "reform": SimulationCalculation(frames=fresh),
    }
    monkeypatch.setattr(
        simulation_module,
        "calculate_simulation_frames",
        lambda planned: calculations[planned],
    )


def test_determinism_check_reports_each_kind_of_frame_difference(
    monkeypatch, tmp_path
) -> None:
    client = _FakeClient()
    _use_store(monkeypatch, client)
    plan = _plan(exists=True, verified=False)
    probe = plan.baselines[0]
    (tmp_path / probe.filename).write_bytes(b"h5")
    cached = {
        "person": pd.DataFrame({"person_id": [1], "age": [30], "income": [1.0]}),
        "tax_unit": pd.DataFrame({"tax_unit_id": [1]}),
    }
    fresh = {
        "household": pd.DataFrame({"household_id": [1]}),
        "person": pd.DataFrame({"person_id": [2], "age": [30.0]}),
    }
    _use_determinism_calculations(monkeypatch, tmp_path, cached, fresh)

    verdict = precompute.verify_determinism_impl("cache-bucket", plan, probe)

    assert verdict.equal is False
    assert verdict.differences == (
        "household: missing on one side",
        "person.age: dtype differs",
        "person.income: only on one side",
        "person.person_id: values differ",
        "tax_unit: missing on one side",
    )
    assert precompute.determinism_record_path(plan.baselines) not in client.objects


def test_determinism_check_rejects_a_probe_that_is_not_a_planned_baseline() -> None:
    plan = _plan(exists=True, verified=False)
    unplanned = plan.baselines[0].model_copy(update={"simulation_id": "unplanned"})

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 determinism probe is not a planned baseline$"
    ):
        precompute.verify_determinism_impl("cache-bucket", plan, unplanned)


@pytest.mark.parametrize("already_local", [True, False])
def test_determinism_check_downloads_the_stored_baseline_only_when_missing(
    monkeypatch, tmp_path, *, already_local
) -> None:
    downloads = []

    class RecordingStore:
        def __init__(self, _bucket):
            pass

        def download_file(self, path, local_path):
            downloads.append((path, local_path))

        def write_json(self, *_):
            pass

    monkeypatch.setattr(precompute, "Stage12CacheStore", RecordingStore)
    plan = _plan(exists=True, verified=False)
    probe = plan.baselines[0]
    if already_local:
        (tmp_path / probe.filename).write_bytes(b"h5")
    _use_determinism_calculations(monkeypatch, tmp_path, _frames(30), _frames(30))

    verdict = precompute.verify_determinism_impl("cache-bucket", plan, probe)

    assert verdict.equal is True
    expected_downloads = (
        [] if already_local else [(probe.path, tmp_path / probe.filename)]
    )
    assert downloads == expected_downloads


def test_installed_bundle_plans_a_regional_file_per_state_and_year(
    monkeypatch,
) -> None:
    """Uses the real bundle and US region registry."""
    client = _FakeClient()
    _use_store(monkeypatch, client)

    plan = precompute.plan_artifacts_impl("cache-bucket")

    assert len(plan.state_datasets) == 51 * len(precompute.PRECOMPUTE_YEARS)
    assert {entry.dataset for entry in plan.state_datasets} == {REGIONAL_DATASET}
    utah = next(
        entry
        for entry in plan.state_datasets
        if entry.year == 2026 and entry.state_code == "UT"
    )
    assert utah.folder == f"state-datasets/{REGIONAL_DATASET}/UT"
    assert utah.filename == f"{REGIONAL_DATASET}_year_2026.h5"


def test_planned_baseline_file_is_the_one_the_worker_saves_and_loads(
    monkeypatch,
) -> None:
    """Uses the real bundle, US model and .py naming: no stubbed ensure()."""
    from policyengine.core import Dataset
    from policyengine.tax_benefit_models.common.model_version import (
        output_dataset_filepath,
    )
    from policyengine_simulation_executor.stage12_runtime.cached_simulation import (
        Stage12CachedSimulation,
    )

    client = _FakeClient()
    _use_store(monkeypatch, client)
    plan = precompute.plan_artifacts_impl("cache-bucket")
    entry = plan.baselines[0]
    planned = precompute._scoped_planned_simulation(entry.year, entry.region_codes)
    params = simulation_module.simulation_runtime_params(planned)
    country_module = simulation_runtime._country_module("us")
    region = simulation_runtime._resolve_region(
        country_module=country_module, country="us", params=params
    )
    dataset_path = "/opt/policyengine/stage12-data/populace_year_2026.h5"

    worker_model = simulation_module.build_stage12_simulation(
        params,
        resolved=simulation_module.ResolvedSimulationInputs(
            country_module=country_module,
            region=region,
            dataset_selection=SimpleNamespace(is_default=True),
            dataset=Dataset(
                name="in-memory",
                description="Naming only",
                year=entry.year,
                filepath=dataset_path,
            ),
        ),
        policy=None,
        execution=planned,
    )

    assert isinstance(worker_model, Stage12CachedSimulation)
    assert worker_model.id == entry.simulation_id
    assert output_dataset_filepath(worker_model).name == entry.filename
    assert entry.path.endswith(f"/{entry.filename}")
    # A US simulation always resolves an SPM selection, which .py names.
    assert entry.filename != f"{entry.simulation_id}.h5"
    assert plan.determinism_verified is False
