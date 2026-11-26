"""The precompute run for one country: which builds it starts, and in what order.

``run_precompute`` takes the six Modal function handles as plain fakes here.
Each fake records its calls in one shared event list, so the tests can check
that a baseline starts only after the build of the year file it reads, and
that publishing waits for every build.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from policyengine_simulation_executor import precompute
from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    PrecomputePlan,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.stage12_cache.levels import DatasetLevel

BUCKET = "bucket-run"


def _year_file(year, *, level=DatasetLevel.NATIONAL, code=None, exists=False):
    dataset = "populace_cps" if code is None else "populace_cps_local"
    folder = f"prebuilt/us/{dataset}/{code or 'all'}"
    return YearFilePlanEntry(
        country="us",
        level=level,
        year=year,
        dataset=dataset,
        region=None if code is None else f"state/{code.lower()}",
        subset_code=code,
        digest=f"yf-{year}-{code or 'all'}",
        path=f"year-files/us/yf-{year}-{code or 'all'}/{dataset}_year_{year}.h5",
        filename=f"{dataset}_year_{year}.h5",
        folder=folder,
        exists=exists,
    )


def _baseline(year_file, *, level=DatasetLevel.STATE_GROUP, label="group 1 of 20"):
    simulation_id = (
        "bl3-" + hashlib.sha256(f"{year_file.path} {label}".encode()).hexdigest()[:16]
    )
    return BaselinePlanEntry(
        country="us",
        level=level,
        year=year_file.year,
        region="region_group/state/ca",
        label=label,
        request={"country": "us", "scope": "macro", "time_period": year_file.year},
        year_file_path=year_file.path,
        year_file_filename=year_file.filename,
        folder=year_file.folder,
        digest=f"bl-{simulation_id}",
        path=f"baselines/us/bl-{simulation_id}/{simulation_id}.h5",
        simulation_id=simulation_id,
        storage_id=simulation_id,
        exists=False,
    )


def _plan(year_files, baselines, country="us"):
    return PrecomputePlan(
        country=country,
        year_files=year_files,
        baselines=baselines,
        receipt=BundleVersionIdentity(
            policyengine_version="4.22.0",
            policyengine_core_version="3.33.3",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        ),
    )


@dataclass
class _Handle:
    name: str
    events: list[tuple[str, ...]]
    result: Any

    def get(self):
        self.events.append(("get", self.name))
        return self.result


@dataclass
class _RecordingFunction:
    name: str
    events: list[tuple[str, ...]]
    respond: Any
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def remote(self, *arguments):
        self.calls.append(arguments)
        self.events.append(("remote", self.name))
        return self.respond(*arguments)

    def spawn(self, *arguments):
        self.calls.append(arguments)
        label = f"{self.name}#{len(self.calls)}"
        self.events.append(("spawn", label))
        return _Handle(label, self.events, self.respond(*arguments))


def _year_file_result(entry):
    return {
        "year": entry["year"],
        "path": entry["path"],
        "uploaded": True,
        "size_bytes": 10,
        "build_seconds": 1.0,
    }


def _functions(plan, *, determinism_equal=True):
    events: list[tuple[str, ...]] = []
    functions = precompute.PrecomputeFunctions(
        plan_artifacts=_RecordingFunction(
            "plan_artifacts", events, lambda _bucket, _country: plan.model_dump()
        ),
        build_dataset=_RecordingFunction(
            "build_dataset",
            events,
            lambda _bucket, entry: _year_file_result(entry),
        ),
        build_state_datasets=_RecordingFunction(
            "build_state_datasets",
            events,
            lambda _bucket, _year, entries: [
                _year_file_result(entry) for entry in entries
            ],
        ),
        compute_baseline=_RecordingFunction(
            "compute_baseline",
            events,
            lambda _bucket, entry: {
                "year": entry["year"],
                "label": entry["label"],
                "simulation_id": entry["simulation_id"],
                "outcome": "miss",
                "uploaded": True,
                "compute_seconds": 2.0,
                "size_bytes": 20,
            },
        ),
        verify_determinism=_RecordingFunction(
            "verify_determinism",
            events,
            lambda _bucket, _entry: {
                "equal": determinism_equal,
                "differences": []
                if determinism_equal
                else ["person.age: values differ"],
            },
        ),
        publish_manifest=_RecordingFunction(
            "publish_manifest", events, lambda _bucket, _plan: "manifest-digest"
        ),
    )
    return functions, events


def _run(plan, **keyword_arguments):
    functions, events = _functions(plan, **keyword_arguments)
    echoed: list[str] = []
    digest = precompute.run_precompute(
        BUCKET,
        country=plan.country,
        force=False,
        functions=functions,
        echo=echoed.append,
    )
    return functions, events, echoed, digest


def test_plans_for_the_requested_country():
    functions, _events, _echoed, _digest = _run(_plan([], [], country="uk"))

    assert functions.plan_artifacts.calls == [(BUCKET, "uk")]


def test_builds_only_missing_files_and_groups_subset_builds():
    national_2026 = _year_file(2026, exists=True)
    national_2027 = _year_file(2027)
    states = [
        _year_file(year, level=DatasetLevel.STATE, code=code)
        for year in (2026, 2027)
        for code in ("UT", "WY")
    ]
    functions, _events, _echoed, _digest = _run(
        _plan([national_2026, national_2027, *states], [])
    )

    assert [call[1]["path"] for call in functions.build_dataset.calls] == [
        national_2027.path
    ]
    assert [
        (call[1], [entry["subset_code"] for entry in call[2]])
        for call in functions.build_state_datasets.calls
    ] == [(2026, ["UT", "WY"]), (2027, ["UT", "WY"])]


def test_baselines_wait_for_whole_dataset_builds_and_publishing_waits_for_all():
    national = _year_file(2026)
    utah = _year_file(2026, level=DatasetLevel.STATE, code="UT")
    group = _baseline(national)
    _functions_used, events, echoed, digest = _run(_plan([national, utah], [group]))

    def position(event):
        return events.index(event)

    assert position(("get", "build_dataset#1")) < position(
        ("spawn", "compute_baseline#1")
    )
    assert position(("get", "build_state_datasets#1")) < position(
        ("remote", "publish_manifest")
    )
    assert position(("spawn", "build_state_datasets#1")) < position(
        ("get", "build_dataset#1")
    )
    assert digest == "manifest-digest"
    assert echoed[-1] == "MANIFEST_DIGEST=manifest-digest"


def test_a_baseline_on_a_subset_file_waits_for_that_build():
    utah = _year_file(2026, level=DatasetLevel.STATE, code="UT")
    state_baseline = _baseline(utah, level=DatasetLevel.STATE, label="state/ut")
    _functions_used, events, _echoed, _digest = _run(_plan([utah], [state_baseline]))

    assert events.index(("get", "build_state_datasets#1")) < events.index(
        ("spawn", "compute_baseline#1")
    )


def test_one_determinism_check_per_level_that_computed_baselines():
    national = _year_file(2026, exists=True)
    utah = _year_file(2026, level=DatasetLevel.STATE, code="UT", exists=True)
    groups = [_baseline(national, label=f"group {index} of 20") for index in (1, 2)]
    state_baseline = _baseline(utah, level=DatasetLevel.STATE, label="state/ut")
    functions, _events, _echoed, _digest = _run(
        _plan([national, utah], [*groups, state_baseline])
    )

    assert [call[1]["label"] for call in functions.verify_determinism.calls] == [
        "group 1 of 20",
        "state/ut",
    ]


def test_a_run_with_nothing_missing_starts_nothing():
    national = _year_file(2026, exists=True)
    stored_group = _baseline(national).model_copy(update={"exists": True})
    functions, _events, _echoed, digest = _run(_plan([national], [stored_group]))

    assert functions.build_dataset.calls == []
    assert functions.build_state_datasets.calls == []
    assert functions.compute_baseline.calls == []
    assert functions.verify_determinism.calls == []
    assert digest == "manifest-digest"


def test_a_failed_determinism_check_stops_before_publishing():
    national = _year_file(2026)
    functions, events = _functions(
        _plan([national], [_baseline(national)]), determinism_equal=False
    )

    with pytest.raises(SystemExit, match=r"person\.age"):
        precompute.run_precompute(
            BUCKET, country="us", force=False, functions=functions, echo=lambda _: None
        )
    assert ("remote", "publish_manifest") not in events


def test_a_plan_with_source_files_to_copy_stops_before_any_build():
    """Nothing copies source files yet, so a plan that needs one fails first.

    Otherwise the run would build every file and then fail to publish a
    manifest listing a source file that was never stored.
    """
    from policyengine_simulation_executor.precompute_models import SourceFilePlanEntry

    national = _year_file(2026)
    source = SourceFilePlanEntry(
        country="us",
        level=DatasetLevel.NATIONAL,
        dataset="populace_cps_local",
        digest="sf1-digest",
        sha256="ab" * 32,
        path=f"sources/us/{'ab' * 32}/populace_cps_local.h5",
        filename="populace_cps_local.h5",
        folder="prebuilt/us/populace_cps_local/all",
        exists=False,
    )
    plan = _plan([national], [_baseline(national)]).model_copy(
        update={"source_files": [source]}
    )
    functions, events = _functions(plan)

    with pytest.raises(SystemExit, match="source file"):
        precompute.run_precompute(
            BUCKET, country="us", force=False, functions=functions, echo=lambda _: None
        )
    assert events == [("remote", "plan_artifacts")]
