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

    assert len(cold.datasets) == 3 and len(cold.baselines) == 60
    assert warm.datasets == () and warm.baselines == ()


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
        plan_artifacts=planner,
        build_dataset=dataset,
        compute_baseline=baseline,
        verify_determinism=verify,
        publish_manifest=publish,
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
        plan_artifacts=planner,
        build_dataset=unused,
        compute_baseline=unused,
        verify_determinism=verify,
        publish_manifest=publish,
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

    with pytest.raises(SystemExit, match="person.age differs"):
        precompute.run_precompute(
            "cache-bucket",
            plan_artifacts=planner,
            build_dataset=dataset,
            compute_baseline=baseline,
            verify_determinism=verify,
            publish_manifest=publish,
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
            plan_artifacts=planner,
            build_dataset=unused,
            compute_baseline=unused,
            verify_determinism=verify,
            publish_manifest=publish,
            echo=lambda _line: None,
        )

    if passes:
        run()
        assert len(publish.remote_calls) == 1
    else:
        with pytest.raises(SystemExit, match="person.age"):
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


def test_planned_baseline_file_is_the_one_the_worker_saves_and_loads(
    monkeypatch,
) -> None:
    """Uses the real bundle, US model and .py naming: no stubbed ensure()."""
    from policyengine.core import Dataset
    from policyengine.tax_benefit_models.common.model_version import (
        output_dataset_filepath,
    )
    from policyengine_simulation_executor.stage12_cache.runtime import (
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
        dataset=Dataset(
            name="in-memory",
            description="Naming only",
            year=entry.year,
            filepath=dataset_path,
        ),
        dataset_selection=SimpleNamespace(is_default=True),
        country_module=country_module,
        policy=None,
        scoping_strategy=region.scoping_strategy,
        region_code=region.code,
        execution=planned,
    )

    assert isinstance(worker_model, Stage12CachedSimulation)
    assert worker_model.id == entry.simulation_id
    assert output_dataset_filepath(worker_model).name == entry.filename
    assert entry.path.endswith(f"/{entry.filename}")
    # A US simulation always resolves an SPM selection, which .py names.
    assert entry.filename != f"{entry.simulation_id}.h5"
    assert plan.determinism_verified is False
