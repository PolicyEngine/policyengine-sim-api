"""Tests for the precompute library's planning, manifests and orchestration.

They also pin the writer==reader identity contract. No Modal involved —
the library takes function handles as parameters, so the orchestration runs
against plain fakes here. Everything structured is a strict model from
precompute_models; the fakes speak dicts, exactly like Modal's
serialization boundary does.
"""

import pytest
from policyengine_simulation_executor import precompute
from policyengine_simulation_executor.artifact_keys import canonical_digest
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
)
from pydantic import ValidationError


def _receipt() -> BundleVersionIdentity:
    return BundleVersionIdentity(
        policyengine_version="4.22.0",
        model_version="1.0.0",
        data_version="1.2.3",
        data_artifact_revision="rev-abc",
        default_dataset="populace_cps",
    )


def _plan() -> PrecomputePlan:
    return PrecomputePlan(
        datasets=[
            DatasetPlanEntry(
                year=2026,
                digest="d1",
                path="datasets/us/d1/populace_year_2026.h5",
                filename="populace_year_2026.h5",
                exists=True,
            ),
            DatasetPlanEntry(
                year=2027,
                digest="d2",
                path="datasets/us/d2/populace_year_2027.h5",
                filename="populace_year_2027.h5",
                exists=False,
            ),
        ],
        baselines=[
            BaselinePlanEntry(
                year=2026,
                group=["state/ca"],
                region="region_group/state/ca",
                digest="b1",
                path="baselines/us/b1/bl1-aaaa.h5",
                simulation_id="bl1-aaaa",
                exists=True,
            ),
            BaselinePlanEntry(
                year=2027,
                group=["state/ca"],
                region="region_group/state/ca",
                digest="b2",
                path="baselines/us/b2/bl1-bbbb.h5",
                simulation_id="bl1-bbbb",
                exists=False,
            ),
        ],
        receipt=_receipt(),
    )


def _state_dataset_entry(year: int, state_code: str, *, exists: bool):
    digest = f"sd-{year}-{state_code}"
    return StateDatasetPlanEntry(
        year=year,
        state_code=state_code,
        region=f"state/{state_code.lower()}",
        dataset="populace_local",
        digest=digest,
        path=f"state-datasets/us/{digest}/populace_local_year_{year}.h5",
        filename=f"populace_local_year_{year}.h5",
        folder=f"state-datasets/populace_local/{state_code}",
        exists=exists,
    )


def _plan_with_state_datasets() -> PrecomputePlan:
    return _plan().model_copy(
        update={
            "state_datasets": [
                _state_dataset_entry(2026, "UT", exists=True),
                _state_dataset_entry(2026, "WY", exists=False),
                _state_dataset_entry(2027, "UT", exists=False),
            ]
        }
    )


class TestSchemas:
    def test_plan_round_trips_through_the_modal_boundary(self):
        plan = _plan()
        assert PrecomputePlan.model_validate(plan.model_dump()) == plan

    def test_unknown_keys_are_rejected(self):
        payload = _plan().model_dump()
        payload["surprise"] = True
        with pytest.raises(ValidationError):
            PrecomputePlan.model_validate(payload)

    def test_missing_keys_are_rejected(self):
        payload = _plan().model_dump()
        del payload["baselines"][0]["simulation_id"]
        with pytest.raises(ValidationError):
            PrecomputePlan.model_validate(payload)

    def test_artifact_outcome_mirrors_runtime_constants(self):
        """The outcome Literal must track baseline_artifacts.OUTCOME_*.

        Drift would surface as a ValidationError mid-precompute-run, not in
        review.
        """
        from typing import get_args

        from policyengine_simulation_executor import baseline_artifacts as ba
        from policyengine_simulation_executor.precompute_models import ArtifactOutcome

        assert set(get_args(ArtifactOutcome)) == {
            ba.OUTCOME_HIT,
            ba.OUTCOME_INCOMPLETE,
            ba.OUTCOME_MISS,
        }


class TestPlanning:
    def test_select_work_computes_only_misses(self):
        work = precompute.select_work(_plan(), force=False)
        assert [entry.year for entry in work.datasets] == [2027]
        assert [entry.simulation_id for entry in work.baselines] == ["bl1-bbbb"]

    def test_force_selects_everything(self):
        work = precompute.select_work(_plan(), force=True)
        assert len(work.datasets) == 2
        assert len(work.baselines) == 2

    def test_select_work_computes_only_missing_state_datasets(self):
        work = precompute.select_work(_plan_with_state_datasets(), force=False)
        assert [(entry.year, entry.state_code) for entry in work.state_datasets] == [
            (2026, "WY"),
            (2027, "UT"),
        ]

    def test_force_selects_every_state_dataset(self):
        work = precompute.select_work(_plan_with_state_datasets(), force=True)
        assert len(work.state_datasets) == 3

    def test_a_plan_without_state_datasets_selects_none(self):
        assert precompute.select_work(_plan(), force=False).state_datasets == []

    def test_plan_with_state_datasets_round_trips_through_the_modal_boundary(self):
        plan = _plan_with_state_datasets()
        assert PrecomputePlan.model_validate(plan.model_dump()) == plan

    def test_years_are_in_priority_order(self):
        assert precompute.PRECOMPUTE_YEARS == [2026, 2027, 2025]

    def test_precompute_years_cover_runtime_default_year(self):
        """The runtime default year must be one of the precomputed years.

        The image bakes exactly the precomputed years, so otherwise every
        default request pays a runtime dataset build.
        """
        from policyengine_simulation_executor.simulation_runtime import DEFAULT_YEAR

        assert DEFAULT_YEAR in precompute.PRECOMPUTE_YEARS

    def test_manifest_lists_every_artifact_with_runtime_filenames(self):
        manifest = precompute.build_manifest(_plan())
        assert manifest.manifest_schema == precompute.MANIFEST_SCHEMA
        assert manifest.receipt == _receipt()
        by_type: dict[str, list] = {}
        for artifact in manifest.artifacts:
            by_type.setdefault(artifact.type, []).append(artifact)
        assert [a.filename for a in by_type["dataset"]] == [
            "populace_year_2026.h5",
            "populace_year_2027.h5",
        ]
        # Baseline runtime filename is {simulation_id}.h5 — the exact name
        # Simulation.ensure() resolves beside the dataset.
        assert [a.filename for a in by_type["baseline"]] == [
            "bl1-aaaa.h5",
            "bl1-bbbb.h5",
        ]

    def test_manifest_lists_state_datasets_after_the_others_in_plan_order(self):
        plan = _plan_with_state_datasets()

        manifest = precompute.build_manifest(plan)

        assert [artifact.type for artifact in manifest.artifacts] == [
            "dataset",
            "dataset",
            "baseline",
            "baseline",
            "state_dataset",
            "state_dataset",
            "state_dataset",
        ]
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
                "digest": entry.digest,
            }
            for entry in plan.state_datasets
        ]

    def test_manifest_with_state_datasets_round_trips_its_wire_shape(self):
        payload = precompute.build_manifest(
            _plan_with_state_datasets()
        ).canonical_payload()

        assert ArtifactManifest.model_validate(payload).canonical_payload() == payload

    def test_a_state_dataset_entry_must_name_its_folder_and_state(self):
        payload = precompute.build_manifest(
            _plan_with_state_datasets()
        ).canonical_payload()
        del payload["artifacts"][-1]["folder"]

        with pytest.raises(ValidationError):
            ArtifactManifest.model_validate(payload)

    def test_manifest_payload_is_wire_compatible(self):
        """The typed manifest must digest identically to the published dict shape.

        The manifest is content-addressed, so key names matter too — note
        "schema", not "manifest_schema".
        """
        raw = {
            "schema": "mf1",
            "country": "us",
            "receipt": {
                "policyengine_version": "4.22.0",
                "model_version": "1.0.0",
                "data_version": "1.2.3",
                "data_artifact_revision": "rev-abc",
                "default_dataset": "populace_cps",
            },
            "artifacts": [
                {
                    "type": "dataset",
                    "path": "datasets/us/d1/populace_year_2026.h5",
                    "filename": "populace_year_2026.h5",
                    "year": 2026,
                    "digest": "d1",
                },
                {
                    "type": "dataset",
                    "path": "datasets/us/d2/populace_year_2027.h5",
                    "filename": "populace_year_2027.h5",
                    "year": 2027,
                    "digest": "d2",
                },
                {
                    "type": "baseline",
                    "path": "baselines/us/b1/bl1-aaaa.h5",
                    "filename": "bl1-aaaa.h5",
                    "year": 2026,
                    "digest": "b1",
                },
                {
                    "type": "baseline",
                    "path": "baselines/us/b2/bl1-bbbb.h5",
                    "filename": "bl1-bbbb.h5",
                    "year": 2027,
                    "digest": "b2",
                },
            ],
        }
        payload = precompute.build_manifest(_plan()).canonical_payload()
        assert payload == raw
        assert canonical_digest(payload) == canonical_digest(raw)
        # And the wire shape validates back into the model.
        assert ArtifactManifest.model_validate(raw).canonical_payload() == raw


class FakeHandle:
    def __init__(self, result):
        self._result = result

    def get(self):
        return self._result


class FakeFunction:
    """Stands in for a Modal function handle (.remote / .spawn).

    Speaks dicts only, as the real boundary does.
    """

    def __init__(self, result=None, results_by_year=None):
        self.result = result
        self.results_by_year = results_by_year or {}
        self.remote_calls = []
        self.spawn_calls = []

    def remote(self, *args):
        self.remote_calls.append(args)
        return self.result

    def spawn(self, *args):
        self.spawn_calls.append(args)
        year = args[1]["year"] if len(args) > 1 else None
        return FakeHandle(self.results_by_year.get(year, self.result))


def _dataset_result(year):
    return {
        "year": year,
        "path": f"datasets/us/d/populace_year_{year}.h5",
        "uploaded": True,
        "build_seconds": 1.0,
        "size_bytes": 10,
    }


def _baseline_result(year):
    return {
        "year": year,
        "group": ["state/ca"],
        "simulation_id": f"bl1-{year}",
        "outcome": "miss",
        "uploaded": True,
        "compute_seconds": 2.0,
        "size_bytes": 10,
    }


class RecordingFunction:
    """A Modal handle fake that logs, in one shared list, when it is used.

    ``results`` maps the spawned year to the result returned. The year is the
    second argument itself (a state build) or that plan entry's ``year``.
    """

    def __init__(self, name, log, results):
        self.name = name
        self.log = log
        self.results = results
        self.spawn_calls = []

    def spawn(self, *args):
        self.spawn_calls.append(args)
        year = args[1] if isinstance(args[1], int) else args[1]["year"]
        self.log.append(("spawn", self.name, year))
        log, name, result = self.log, self.name, self.results[year]

        class Handle:
            def get(self):
                log.append(("get", name, year))
                return result

        return Handle()


def _state_build_result(
    year, state_code, *, uploaded=True, size_bytes=100, build_seconds=30.0
):
    return {
        "year": year,
        "state_code": state_code,
        "path": (
            f"state-datasets/us/sd-{year}-{state_code}/populace_local_year_{year}.h5"
        ),
        "uploaded": uploaded,
        "size_bytes": size_bytes,
        "build_seconds": build_seconds,
    }


class TestRunPrecompute:
    def _functions(self, verify_result=None):
        return {
            "plan_artifacts": FakeFunction(result=_plan().model_dump()),
            "build_dataset": FakeFunction(
                results_by_year={2027: _dataset_result(2027)}
            ),
            "build_state_datasets": RecordingFunction("build_state_datasets", [], {}),
            "compute_baseline": FakeFunction(
                results_by_year={2027: _baseline_result(2027)}
            ),
            "verify_determinism": FakeFunction(
                result=verify_result or {"equal": True, "differences": []}
            ),
            "publish_manifest": FakeFunction(result="digest-123"),
        }

    def _run(self, functions, *, force=False):
        lines = []
        digest = precompute.run_precompute(
            "bucket-x",
            force=force,
            functions=precompute.PrecomputeFunctions(**functions),
            echo=lines.append,
        )
        return digest, lines

    def test_spawns_only_misses_and_gates_on_first_computed(self):
        functions = self._functions()
        digest, _lines = self._run(functions)

        (dataset_spawn,) = functions["build_dataset"].spawn_calls
        assert dataset_spawn[0] == "bucket-x"
        assert dataset_spawn[1] == _plan().datasets[1].model_dump()
        (baseline_spawn,) = functions["compute_baseline"].spawn_calls
        assert baseline_spawn[1] == _plan().baselines[1].model_dump()
        # The gate probes the first computed baseline, with its plan entry.
        (verify_call,) = functions["verify_determinism"].remote_calls
        assert verify_call[1]["simulation_id"] == "bl1-bbbb"
        assert digest == "digest-123"
        # The manifest publishes the FULL plan, never the miss-only work
        # subset: the fetch layer needs every artifact listed.
        assert functions["publish_manifest"].remote_calls == [
            ("bucket-x", _plan().model_dump())
        ]

    def test_last_echo_line_is_the_digest_contract(self):
        _, lines = self._run(self._functions())
        assert lines[-1] == "MANIFEST_DIGEST=digest-123"
        assert lines[-1].startswith(precompute.MANIFEST_DIGEST_PREFIX)

    def test_noop_run_spawns_nothing_and_skips_the_gate(self):
        functions = self._functions()
        noop_plan = _plan()
        for entry in noop_plan.datasets + noop_plan.baselines:
            entry.exists = True
        functions["plan_artifacts"] = FakeFunction(result=noop_plan.model_dump())

        digest, lines = self._run(functions)
        assert functions["build_dataset"].spawn_calls == []
        assert functions["compute_baseline"].spawn_calls == []
        assert functions["verify_determinism"].remote_calls == []
        # The manifest still publishes — the full plan — because a no-op
        # run must refresh nothing but the deploy still needs a digest.
        assert digest == "digest-123"
        assert lines[-1] == "MANIFEST_DIGEST=digest-123"
        assert functions["publish_manifest"].remote_calls == [
            ("bucket-x", noop_plan.model_dump())
        ]

    def test_failed_gate_aborts_before_publishing(self):
        functions = self._functions(
            verify_result={"equal": False, "differences": ["person.age: values differ"]}
        )
        with pytest.raises(SystemExit, match="values differ"):
            self._run(functions)
        assert functions["publish_manifest"].remote_calls == []

    def _state_run(self, *, uploaded_wyoming=True):
        log = []
        functions = self._functions()
        functions["plan_artifacts"] = FakeFunction(
            result=_plan_with_state_datasets().model_dump()
        )
        functions["build_state_datasets"] = RecordingFunction(
            "build_state_datasets",
            log,
            {
                2026: [
                    _state_build_result(
                        2026, "WY", uploaded=uploaded_wyoming, build_seconds=40.5
                    ),
                ],
                2027: [_state_build_result(2027, "UT", size_bytes=250)],
            },
        )
        functions["compute_baseline"] = RecordingFunction(
            "compute_baseline", log, {2027: _baseline_result(2027)}
        )
        publish = functions["publish_manifest"]

        def publish_remote(*args):
            log.append(("remote", "publish_manifest", None))
            return FakeFunction.remote(publish, *args)

        publish.remote = publish_remote
        digest, lines = self._run(functions)
        return functions, log, digest, lines

    def test_spawns_one_state_build_per_year_with_its_missing_entries(self):
        functions, _log, _digest, _lines = self._state_run()

        plan = _plan_with_state_datasets()
        assert functions["build_state_datasets"].spawn_calls == [
            ("bucket-x", 2026, [plan.state_datasets[1].model_dump()]),
            ("bucket-x", 2027, [plan.state_datasets[2].model_dump()]),
        ]

    def test_waits_for_state_builds_only_before_publishing(self):
        _functions, log, _digest, _lines = self._state_run()

        first_state_get = log.index(("get", "build_state_datasets", 2026))
        assert log.index(("spawn", "build_state_datasets", 2026)) < log.index(
            ("spawn", "compute_baseline", 2027)
        )
        assert log.index(("get", "compute_baseline", 2027)) < first_state_get
        assert first_state_get < log.index(("remote", "publish_manifest", None))

    def test_reports_each_builds_uploads_bytes_and_time(self):
        _functions, _log, _digest, lines = self._state_run(uploaded_wyoming=False)

        assert (
            "State datasets 2026 populace_local: 1 built, 0 uploaded "
            "(0 bytes) in 40.5 s"
        ) in lines
        assert (
            "State datasets 2027 populace_local: 1 built, 1 uploaded "
            "(250 bytes) in 30.0 s"
        ) in lines
        assert lines[-1] == "MANIFEST_DIGEST=digest-123"

    def test_spawns_one_state_build_per_year_and_dataset(self):
        log = []
        functions = self._functions()
        other = _state_dataset_entry(2026, "ID", exists=False).model_copy(
            update={"dataset": "populace_other", "folder": "state-datasets/o/ID"}
        )
        plan = _plan_with_state_datasets()
        plan = plan.model_copy(update={"state_datasets": [*plan.state_datasets, other]})
        functions["plan_artifacts"] = FakeFunction(result=plan.model_dump())
        functions["build_state_datasets"] = RecordingFunction(
            "build_state_datasets",
            log,
            {
                2026: [_state_build_result(2026, "WY")],
                2027: [_state_build_result(2027, "UT")],
            },
        )

        self._run(functions)

        assert [
            (args[1], [entry["dataset"] for entry in args[2]])
            for args in functions["build_state_datasets"].spawn_calls
        ] == [
            (2026, ["populace_local"]),
            (2027, ["populace_local"]),
            (2026, ["populace_other"]),
        ]

    def test_a_run_without_state_datasets_spawns_no_state_build(self):
        functions = self._functions()
        self._run(functions)
        assert functions["build_state_datasets"].spawn_calls == []

    def test_malformed_worker_result_fails_loudly(self):
        functions = self._functions()
        functions["build_dataset"] = FakeFunction(
            results_by_year={2027: {"year": 2027, "unexpected": "shape"}}
        )
        with pytest.raises(ValidationError):
            self._run(functions)
