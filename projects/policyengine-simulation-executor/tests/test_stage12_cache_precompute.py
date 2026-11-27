"""Unit tests for Stage 12 cache planning and orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_cache import (
    precompute,
    precompute_baselines,
    precompute_datasets,
    precompute_planning,
)
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

# The US handler settings' years, in submission order.
PLANNED_YEARS = (2026, 2027, 2025)
NATIONAL_FOLDER = "prebuilt/us/populace_us_2024/all"


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
            country="us",
            year=year,
            digest=f"dataset-{year}",
            path=f"datasets/us/dataset-{year}/populace_year_{year}.h5",
            filename=f"populace_year_{year}.h5",
            folder=NATIONAL_FOLDER,
            exists=exists,
        )
        for year in PLANNED_YEARS
    )
    baselines = tuple(
        BaselinePlanEntry(
            country="us",
            year=year,
            segment_index=index,
            region_codes=group,
            digest=f"baseline-{year}-{index}",
            path=f"baselines/us/baseline-{year}-{index}/stage12-bl1-{index}.h5",
            filename=f"stage12-bl1-{index}.h5",
            folder=NATIONAL_FOLDER,
            simulation_id=f"stage12-bl1-{index}",
            output_plan_sha256="b" * 64,
            exists=exists,
        )
        for year in PLANNED_YEARS
        for index, group in enumerate(US_REGION_GROUPS)
    )
    return PrecomputePlan(
        years=PLANNED_YEARS,
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
        country="us",
        year=year,
        state_code=state_code,
        region=f"state/{state_code.lower()}",
        dataset=REGIONAL_DATASET,
        digest=digest,
        path=f"state-datasets/us/{digest}/{REGIONAL_DATASET}_year_{year}.h5",
        filename=f"{REGIONAL_DATASET}_year_{year}.h5",
        folder=f"prebuilt/us/{REGIONAL_DATASET}/{state_code}",
        exists=exists,
    )


def _plan_with_state_datasets(*, exists: bool = False) -> PrecomputePlan:
    return _plan(exists=exists).model_copy(
        update={
            "state_datasets": tuple(
                _state_entry(year, state_code, exists=exists)
                for year in PLANNED_YEARS
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

    def upload_from_filename(self, filename, *, if_generation_match):
        assert if_generation_match == 0
        if self.path in self.objects:
            raise PreconditionFailed(self.path)
        with open(filename, "rb") as source:  # noqa: PTH123  # mirrors the GCS client's own file read
            payload = source.read()
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
    # Every precompute module that opens the cache store.
    for module in (
        precompute,
        precompute_planning,
        precompute_datasets,
        precompute_baselines,
    ):
        monkeypatch.setattr(
            module,
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
            build_state_datasets=_Function(lambda *_: [], events, "state"),
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


def _state_run_handles(events, plan):
    def baseline_result(_bucket, entry):
        return {
            "path": entry["path"],
            "content_sha256": "e" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "segment_index": entry["segment_index"],
            "simulation_id": entry["simulation_id"],
            "cache_outcome": "miss",
            "uploaded": True,
            "compute_seconds": 1.0,
        }

    def dataset_result(_bucket, entry):
        return {
            "path": entry["path"],
            "content_sha256": "d" * 64,
            "size_bytes": 1,
            "year": entry["year"],
            "uploaded": True,
            "build_seconds": 1.0,
        }

    def state_results(_bucket, year, entries):
        return [
            {
                "path": entry["path"],
                "content_sha256": "c" * 64,
                "size_bytes": 100,
                "year": year,
                "state_code": entry["state_code"],
                "uploaded": entry["state_code"] != "WY",
                "build_seconds": 12.5 if entry["state_code"] == "WY" else 6.0,
            }
            for entry in entries
        ]

    return {
        "plan_artifacts": _Function(lambda _: plan.model_dump(), events, "plan"),
        "build_dataset": _Function(dataset_result, events, "dataset"),
        "build_state_datasets": _Function(state_results, events, "state"),
        "compute_baseline": _Function(baseline_result, events, "baseline"),
        "verify_determinism": _Function(
            lambda *_: {"equal": True, "differences": []}, events, "verify"
        ),
        "publish_manifest": _Function(lambda *_: "manifest-digest", events, "publish"),
    }


def test_precompute_builds_each_year_of_state_datasets_in_one_call() -> None:
    events = []
    plan = _plan_with_state_datasets()
    handles = _state_run_handles(events, plan)

    precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(**handles),
        echo=lambda _line: None,
    )

    assert handles["build_state_datasets"].spawn_calls == [
        (
            "cache-bucket",
            year,
            [entry.model_dump() for entry in plan.state_datasets if entry.year == year],
        )
        for year in PLANNED_YEARS
    ]


def test_precompute_waits_for_state_datasets_only_before_publishing() -> None:
    events = []
    handles = _state_run_handles(events, _plan_with_state_datasets())

    precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(**handles),
        echo=lambda _line: None,
    )

    first_state_get = events.index("get:state")
    assert events.index("spawn:state") < events.index("spawn:baseline")
    assert events.index("remote:verify") < first_state_get
    assert first_state_get < events.index("remote:publish")


def test_precompute_reports_each_builds_uploads_bytes_and_time() -> None:
    events = []
    lines = []
    handles = _state_run_handles(events, _plan_with_state_datasets())

    precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(**handles),
        echo=lines.append,
    )

    for year in PLANNED_YEARS:
        assert (
            f"State datasets {year} {REGIONAL_DATASET}: 2 built, 1 uploaded "
            "(100 bytes) in 12.5 s"
        ) in lines
    assert lines[-1] == "STAGE12_CACHE_MANIFEST_DIGEST=manifest-digest"


def test_precompute_builds_each_year_and_dataset_in_its_own_call() -> None:
    events = []
    plan = _plan_with_state_datasets()
    other = _state_entry(2026, "ID", exists=False).model_copy(
        update={"dataset": "populace_other", "folder": "prebuilt/us/populace_other/ID"}
    )
    plan = plan.model_copy(update={"state_datasets": (*plan.state_datasets, other)})
    handles = _state_run_handles(events, plan)

    precompute.run_precompute(
        "cache-bucket",
        functions=precompute.CachePrecomputeFunctions(**handles),
        echo=lambda _line: None,
    )

    assert [
        (call[1], {entry["dataset"] for entry in call[2]})
        for call in handles["build_state_datasets"].spawn_calls
    ] == [
        (2026, {REGIONAL_DATASET}),
        (2027, {REGIONAL_DATASET}),
        (2025, {REGIONAL_DATASET}),
        (2026, {"populace_other"}),
    ]


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
            build_state_datasets=unused,
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
                build_state_datasets=dataset,
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


def test_manifest_lists_state_datasets_after_the_others_in_plan_order() -> None:
    plan = _plan_with_state_datasets(exists=True)
    store = SimpleNamespace(
        describe=lambda path: CacheObject(
            path=path, content_sha256="f" * 64, size_bytes=123
        )
    )

    manifest = precompute.build_manifest(plan, store)

    assert len(manifest.artifacts) == 63 + len(plan.state_datasets)
    state_artifacts = [
        artifact.model_dump()
        for artifact in manifest.artifacts
        if artifact.type == "state_dataset"
    ]
    assert state_artifacts == [
        {
            "type": "state_dataset",
            "path": entry.path,
            "filename": entry.filename,
            "folder": entry.folder,
            "state_code": entry.state_code,
            "year": entry.year,
            "identity_digest": entry.digest,
            "content_sha256": "f" * 64,
            "size_bytes": 123,
        }
        for entry in plan.state_datasets
    ]
    assert [artifact.type for artifact in manifest.artifacts[:63]] == (
        ["dataset"] * 3 + ["baseline"] * 60
    )


def test_manifest_records_the_folder_of_every_year_file_and_baseline() -> None:
    from policyengine_simulation_executor.stage12_cache.models import CacheManifest

    store = SimpleNamespace(
        describe=lambda path: CacheObject(
            path=path, content_sha256="f" * 64, size_bytes=123
        )
    )
    payload = precompute.build_manifest(_plan(exists=True), store).canonical_payload()

    assert {item["folder"] for item in payload["artifacts"]} == {NATIONAL_FOLDER}
    assert CacheManifest.model_validate(payload).canonical_payload() == payload


def test_manifest_with_state_datasets_round_trips_its_wire_shape() -> None:
    from policyengine_simulation_executor.stage12_cache.models import CacheManifest

    store = SimpleNamespace(
        describe=lambda path: CacheObject(
            path=path, content_sha256="f" * 64, size_bytes=123
        )
    )
    payload = precompute.build_manifest(
        _plan_with_state_datasets(exists=True), store
    ).canonical_payload()

    assert CacheManifest.model_validate(payload).canonical_payload() == payload


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
                build_state_datasets=unused,
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
    (tmp_path / probe.folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / probe.folder / probe.filename).write_bytes(b"h5")
    monkeypatch.setattr(simulation_runtime, "resolve_data_folder", lambda: tmp_path)
    monkeypatch.setattr(precompute_baselines, "_ensure_year_file", lambda *_: None)
    monkeypatch.setattr(
        precompute_baselines,
        "scoped_planned_simulation",
        lambda _year, _group, *, reform=False, country: (
            "reform" if reform else "baseline"
        ),
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
    monkeypatch.setattr(precompute_baselines, "_ensure_year_file", lambda *_: None)
    monkeypatch.setattr(
        precompute_baselines,
        "scoped_planned_simulation",
        lambda _year, _group, *, reform=False, country: (
            "reform" if reform else "baseline"
        ),
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
    (tmp_path / probe.folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / probe.folder / probe.filename).write_bytes(b"h5")
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

    monkeypatch.setattr(precompute_baselines, "Stage12CacheStore", RecordingStore)
    plan = _plan(exists=True, verified=False)
    probe = plan.baselines[0]
    if already_local:
        (tmp_path / probe.folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / probe.folder / probe.filename).write_bytes(b"h5")
    _use_determinism_calculations(monkeypatch, tmp_path, _frames(30), _frames(30))

    verdict = precompute.verify_determinism_impl("cache-bucket", plan, probe)

    assert verdict.equal is True
    expected_downloads = (
        []
        if already_local
        else [(probe.path, tmp_path / probe.folder / probe.filename)]
    )
    assert downloads == expected_downloads


def test_installed_bundle_plans_a_regional_file_per_state_and_year(
    monkeypatch,
) -> None:
    """Uses the real bundle and US region registry."""
    client = _FakeClient()
    _use_store(monkeypatch, client)

    plan = precompute.plan_artifacts_impl("cache-bucket")

    assert len(plan.state_datasets) == 51 * len(PLANNED_YEARS)
    assert {entry.dataset for entry in plan.state_datasets} == {REGIONAL_DATASET}
    utah = next(
        entry
        for entry in plan.state_datasets
        if entry.year == 2026 and entry.state_code == "UT"
    )
    assert utah.folder == f"prebuilt/us/{REGIONAL_DATASET}/UT"
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
    planned = precompute_planning.scoped_planned_simulation(
        entry.year, entry.region_codes
    )
    params = simulation_module.simulation_runtime_params(planned)
    country_module = simulation_runtime._country_module("us")
    region = simulation_runtime.resolve_region(
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


class TestBuildStateDatasetsImpl:
    """Building, checking and uploading one year's Stage 12 state datasets."""

    YEAR = 2026

    @pytest.fixture
    def state_build(self, monkeypatch, tmp_path):
        from fixtures import state_dataset_sources as sources
        from policyengine.core.scoping_strategy import RowFilterStrategy
        from policyengine.tax_benefit_models import us as us_module
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            DatasetLevel,
            SubsetFilter,
            YearFileTarget,
            prebuilt_folder,
        )

        client = _FakeClient()
        _use_store(monkeypatch, client)
        state = SimpleNamespace(
            client=client,
            ensure_calls=[],
            entity_frames=sources.source_entity_frames(),
            work_folder=tmp_path / "work",
            sources=sources,
        )

        def target(year, state_code, fips):
            return YearFileTarget(
                country="us",
                level=DatasetLevel.STATE,
                dataset=sources.REGIONAL_DATASET,
                year=year,
                subset=SubsetFilter(
                    region=f"state/{state_code.lower()}",
                    code=state_code,
                    variable="state_fips",
                    value=fips,
                    scoping_strategy=RowFilterStrategy(
                        variable_name="state_fips", variable_value=fips
                    ),
                ),
                folder=prebuilt_folder("us", sources.REGIONAL_DATASET, state_code),
            )

        state.targets = [("UT", sources.UTAH_FIPS), ("WY", sources.WYOMING_FIPS)]
        monkeypatch.setattr(
            levels_us.UsStateHandler,
            "year_file_targets",
            lambda _handler, year: [
                target(year, state_code, fips) for state_code, fips in state.targets
            ],
        )
        monkeypatch.setattr(
            precompute_datasets, "load_stage12_bundle", lambda: "resolved"
        )

        def fake_state_identity(resolved, *, country, year, dataset, **state_filter):
            digest = f"state-{year}-{state_filter['state_code']}"
            return SimpleNamespace(
                digest=digest,
                store_path=f"state-datasets/{country}/{digest}/{dataset}_year_{year}.h5",
                filename=f"{dataset}_year_{year}.h5",
            )

        monkeypatch.setattr(
            precompute_datasets, "collect_state_dataset_identity", fake_state_identity
        )

        def fake_ensure_datasets(*, datasets, years, data_folder):
            state.ensure_calls.append((datasets, years, data_folder))
            folder = Path(data_folder)
            folder.mkdir(parents=True, exist_ok=True)
            for year in years:
                sources.write_year_file(
                    folder, year=year, entity_frames=state.entity_frames
                )
            return us_module.load_datasets(
                datasets=datasets, years=years, data_folder=data_folder
            )

        districts = [
            SimpleNamespace(
                scoping_strategy=RowFilterStrategy(
                    variable_name="congressional_district_geoid",
                    variable_value=geoid,
                )
            )
            for geoid in (4901, 4902, 4903, 4904, 5600)
        ]
        monkeypatch.setattr(
            simulation_runtime,
            "_country_module",
            lambda _country: SimpleNamespace(
                ensure_datasets=fake_ensure_datasets,
                load_datasets=us_module.load_datasets,
                model=SimpleNamespace(
                    region_registry=SimpleNamespace(
                        get_by_type=lambda kind: (
                            districts if kind == "congressional_district" else []
                        )
                    )
                ),
            ),
        )
        state.identity_for = fake_state_identity
        return state

    def _entry(self, state_build, state_code):
        dataset = state_build.sources.REGIONAL_DATASET
        identity = state_build.identity_for(
            "resolved",
            country="us",
            year=self.YEAR,
            dataset=dataset,
            state_code=state_code,
        )
        return StateDatasetPlanEntry(
            country="us",
            year=self.YEAR,
            state_code=state_code,
            region=f"state/{state_code.lower()}",
            dataset=dataset,
            digest=identity.digest,
            path=identity.store_path,
            filename=identity.filename,
            folder=f"prebuilt/us/{dataset}/{state_code}",
            exists=False,
        )

    def _build(self, state_build, entries):
        return precompute.build_state_datasets_impl(
            "cache-bucket", self.YEAR, entries, work_folder=state_build.work_folder
        )

    def test_uploads_each_state_file_with_only_its_households(self, state_build):
        entries = [self._entry(state_build, "UT"), self._entry(state_build, "WY")]

        results = self._build(state_build, entries)

        sources = state_build.sources
        utah = state_build.work_folder / entries[0].folder / entries[0].filename
        wyoming = state_build.work_folder / entries[1].folder / entries[1].filename
        assert sources.stored_ids(utah, "household") == [1, 2]
        assert sources.stored_ids(utah, "person") == [1, 2, 3]
        assert sources.stored_weights(utah, "person") == [10.0, 11.0, 20.0]
        assert sources.stored_ids(wyoming, "household") == [3]
        assert [result.path for result in results] == [entry.path for entry in entries]
        assert all(result.uploaded for result in results)
        assert all(result.size_bytes > 0 for result in results)
        assert state_build.client.objects[entries[0].path]["payload"] == (
            utah.read_bytes()
        )

    def test_dotpy_reuses_a_state_file_without_rebuilding(
        self, state_build, monkeypatch
    ):
        from policyengine.tax_benefit_models.us import datasets as us_datasets

        entry = self._entry(state_build, "UT")
        self._build(state_build, [entry])

        def refuse_to_rebuild(**_kwargs):
            msg = "ensure_datasets rebuilt a baked state file"
            raise AssertionError(msg)

        monkeypatch.setattr(us_datasets, "create_datasets", refuse_to_rebuild)
        (dataset,) = us_datasets.ensure_datasets(
            datasets=[state_build.sources.REGIONAL_DATASET],
            years=[self.YEAR],
            data_folder=str(state_build.work_folder / entry.folder),
        ).values()

        assert sorted(dataset.data.household["household_id"]) == [1, 2]

    def test_prepares_the_full_country_year_file_once(self, state_build):
        self._build(
            state_build,
            [self._entry(state_build, "UT"), self._entry(state_build, "WY")],
        )

        assert len(state_build.ensure_calls) == 1
        datasets, years, _folder = state_build.ensure_calls[0]
        assert datasets == [state_build.sources.REGIONAL_DATASET]
        assert years == [self.YEAR]

    def test_refuses_a_district_that_lies_outside_its_state(self, state_build):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=4903
        )

        with pytest.raises(RuntimeError, match="congressional district"):
            self._build(state_build, [self._entry(state_build, "UT")])
        assert state_build.client.objects == {}

    def test_a_household_without_a_district_code_does_not_stop_the_build(
        self, state_build
    ):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=0
        )

        results = self._build(
            state_build,
            [self._entry(state_build, "UT"), self._entry(state_build, "WY")],
        )

        assert [result.state_code for result in results] == ["UT", "WY"]

    def test_reports_how_long_the_years_build_had_run(self, state_build):
        results = self._build(
            state_build,
            [self._entry(state_build, "UT"), self._entry(state_build, "WY")],
        )

        assert 0 <= results[0].build_seconds <= results[1].build_seconds

    def test_refuses_a_plan_entry_whose_key_differs_in_the_container(self, state_build):
        entry = self._entry(state_build, "UT").model_copy(
            update={"path": "state-datasets/us/other/x.h5"}
        )

        with pytest.raises(RuntimeError, match="identities differ"):
            self._build(state_build, [entry])
        assert state_build.ensure_calls == []

    def test_refuses_a_state_the_container_does_not_target(self, state_build):
        state_build.targets = state_build.targets[:1]

        with pytest.raises(RuntimeError, match="state/wy"):
            self._build(state_build, [self._entry(state_build, "WY")])
        assert state_build.ensure_calls == []

    def test_refuses_entries_for_another_year(self, state_build):
        with pytest.raises(ValueError, match="2027"):
            precompute.build_state_datasets_impl(
                "cache-bucket",
                2027,
                [self._entry(state_build, "UT")],
                work_folder=state_build.work_folder,
            )
