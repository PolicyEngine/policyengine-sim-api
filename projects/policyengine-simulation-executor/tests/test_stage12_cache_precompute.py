"""Unit tests for Stage 12 cache planning and orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from policyengine_simulation_executor.stage12_cache import precompute
from policyengine_simulation_executor.stage12_cache.models import (
    BaselinePlanEntry,
    CacheBundleIdentity,
    CacheObject,
    DatasetPlanEntry,
    PrecomputePlan,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS


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


def _plan(*, exists: bool = False) -> PrecomputePlan:
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
    )


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


def test_work_selection_is_idempotent_and_force_selects_everything() -> None:
    cold = precompute.select_work(_plan(), force=False)
    warm = precompute.select_work(_plan(exists=True), force=False)
    forced = precompute.select_work(_plan(exists=True), force=True)

    assert len(cold.datasets) == 3 and len(cold.baselines) == 60
    assert warm.datasets == () and warm.baselines == ()
    assert len(forced.datasets) == 3 and len(forced.baselines) == 60


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
        force=False,
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
    assert len(verify.remote_calls) == 1
    assert digest == "manifest-digest"
    assert lines[-1] == "STAGE12_CACHE_MANIFEST_DIGEST=manifest-digest"


def test_warm_precompute_skips_builds_and_determinism_validation() -> None:
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
        force=False,
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
            force=False,
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
