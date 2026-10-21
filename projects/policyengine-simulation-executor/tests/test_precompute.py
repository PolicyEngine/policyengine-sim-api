"""Tests for the precompute library's planning, manifests and orchestration.

They also pin the writer==reader identity contract. No Modal involved —
the library takes function handles as parameters, so the orchestration runs
against plain fakes here. Everything structured is a strict model from
precompute_models; the fakes speak dicts, exactly like Modal's
serialization boundary does.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from fixtures.identity_stubs import install_identity_stubs
from fixtures.wrapper_spm import wrapper_storage_id
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


class TestWriterReaderContract:
    """The writer and the runtime reader must derive identical identities."""

    @pytest.fixture
    def identity_stubs(self, monkeypatch):
        from policyengine_simulation_executor import simulation_runtime as sr

        state = install_identity_stubs(monkeypatch)
        monkeypatch.setattr(
            sr, "get_country_release_bundle", lambda _country: state.bundle
        )

    @pytest.fixture
    def fake_regions(self, monkeypatch):
        from policyengine.core.scoping_strategy import RowFilterStrategy
        from policyengine_simulation_executor import simulation_runtime as sr

        def get_region(code):
            state = code.split("/")[-1].upper()
            return SimpleNamespace(
                code=code,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_code", variable_value=state
                ),
            )

        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda _country: SimpleNamespace(
                model=SimpleNamespace(get_region=get_region)
            ),
        )

    @pytest.mark.usefixtures("identity_stubs", "fake_regions")
    def test_precompute_identity_equals_runtime_id(self):
        from policyengine_simulation_executor import simulation_runtime as sr
        from policyengine_simulation_executor.baseline_artifacts import (
            BaselineIdentityRequest,
            deterministic_baseline_id,
        )

        group = ["state/ca", "state/wv"]
        writer_identity = precompute.cohort_identity(2026, group)

        params = precompute.cohort_params(2026, group)
        resolution = sr._resolve_region(
            country_module=sr._country_module("us"), country="us", params=params
        )
        dataset_selection = sr._resolve_dataset_selection(
            params, region_resolution=resolution
        )
        reader_id = deterministic_baseline_id(
            BaselineIdentityRequest(
                params,
                country="us",
                dataset_is_default=dataset_selection.is_default,
                policy=None,
                region_code=resolution.code,
                scoping_strategy=resolution.scoping_strategy,
                year=2026,
            )
        )
        assert writer_identity.simulation_id == reader_id
        assert writer_identity.store_path.endswith(f"/{writer_identity.storage_id}.h5")

    def test_dataset_filename_matches_runtime_stem_lookup(self, monkeypatch):
        """The dataset artifact filename is the name the runtime looks for.

        Real-manifest coverage: it equals the exact name the runtime's
        ensure_datasets existence check resolves.
        """
        from policyengine.provenance.manifest import (
            dataset_logical_name,
            resolve_dataset_reference,
        )
        from policyengine_simulation_executor.artifact_keys import (
            collect_dataset_identity,
        )
        from policyengine_simulation_executor.release_bundle import (
            get_country_release_bundle,
            resolve_bundle_dataset_name,
        )
        from policyengine_simulation_executor.simulation_runtime import DEFAULT_YEAR

        monkeypatch.delenv("POLICYENGINE_BUNDLE_RECEIPT", raising=False)
        get_country_release_bundle.cache_clear()
        try:
            expected_stem = dataset_logical_name(
                resolve_dataset_reference("us", resolve_bundle_dataset_name("us", None))
            )
            identity = collect_dataset_identity("us", DEFAULT_YEAR)
        finally:
            get_country_release_bundle.cache_clear()
        assert identity.filename == f"{expected_stem}_year_{DEFAULT_YEAR}.h5"


class TestPlanArtifactsImpl:
    """The 63-identity planner: enumeration, presence flags, receipt."""

    @pytest.fixture
    def planning_stubs(self, monkeypatch):
        from policyengine_simulation_executor import (
            artifact_keys,
            artifact_store,
            national_partition,
            release_bundle,
        )

        existing = {
            "datasets/us/ds-2026/populace_year_2026.h5",
            "baselines/us/bl-2026-1/bl1-2026-1.h5",
        }

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def exists(self, path):
                return path in existing

        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(
            national_partition,
            "national_region_groups",
            lambda _country: [["state/ca"], ["state/ny", "state/wv"]],
        )
        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda _country, year: SimpleNamespace(
                digest=f"ds-{year}",
                store_path=f"datasets/us/ds-{year}/populace_year_{year}.h5",
                filename=f"populace_year_{year}.h5",
            ),
        )

        def fake_cohort_identity(year, group):
            tag = f"{year}-{len(group)}"
            return SimpleNamespace(
                region="+".join(group),
                digest=f"bl-{tag}",
                store_path=f"baselines/us/bl-{tag}/bl1-{tag}.h5",
                simulation_id=f"bl1-{tag}",
            )

        monkeypatch.setattr(precompute, "cohort_identity", fake_cohort_identity)
        targets = [
            precompute.StateDatasetTarget(
                region="state/ut",
                state_code="UT",
                state_fips=49,
                filter_variable="state_fips",
                dataset="populace_local",
            ),
            precompute.StateDatasetTarget(
                region="state/wy",
                state_code="WY",
                state_fips=56,
                filter_variable="state_fips",
                dataset="populace_local",
            ),
        ]
        monkeypatch.setattr(precompute, "state_dataset_targets", lambda: list(targets))
        existing.add("state-datasets/us/sd-2026-UT/populace_local_year_2026.h5")

        def fake_state_identity(
            _country, year, *, dataset, state_code, state_fips, filter_variable
        ):
            assert (state_fips, filter_variable) in {
                (49, "state_fips"),
                (56, "state_fips"),
            }
            digest = f"sd-{year}-{state_code}"
            return SimpleNamespace(
                digest=digest,
                store_path=f"state-datasets/us/{digest}/{dataset}_year_{year}.h5",
                filename=f"{dataset}_year_{year}.h5",
            )

        monkeypatch.setattr(
            artifact_keys, "collect_state_dataset_identity", fake_state_identity
        )
        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda _country: SimpleNamespace(
                policyengine_version="4.22.0",
                model_version="1.0.0",
                data_version="1.2.3",
                data_artifact_revision="rev-abc",
                default_dataset="populace_cps",
            ),
        )
        return SimpleNamespace(existing=existing, targets=targets)

    @pytest.mark.usefixtures("planning_stubs")
    def test_plan_lists_every_state_dataset_year_major_with_presence(self):
        plan = precompute.plan_artifacts_impl("bucket-x")

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
        assert first.dataset == "populace_local"
        assert first.digest == "sd-2026-UT"
        assert first.path == "state-datasets/us/sd-2026-UT/populace_local_year_2026.h5"
        assert first.filename == "populace_local_year_2026.h5"
        assert first.folder == "state-datasets/populace_local/UT"
        work = precompute.select_work(plan, force=False)
        assert len(work.state_datasets) == 5

    def test_states_on_the_default_dataset_add_no_state_datasets(
        self, planning_stubs, monkeypatch
    ):
        monkeypatch.setattr(precompute, "state_dataset_targets", list)

        plan = precompute.plan_artifacts_impl("bucket-x")

        assert plan.state_datasets == []
        assert len(plan.datasets) == 3

    @pytest.mark.usefixtures("planning_stubs")
    def test_plan_enumerates_years_and_cohorts_with_presence(self):
        plan = precompute.plan_artifacts_impl("bucket-x")

        assert [entry.year for entry in plan.datasets] == [2026, 2027, 2025]
        assert [entry.exists for entry in plan.datasets] == [True, False, False]
        assert plan.datasets[0].digest == "ds-2026"
        assert plan.datasets[0].path == "datasets/us/ds-2026/populace_year_2026.h5"
        assert plan.datasets[0].filename == "populace_year_2026.h5"

        # Year-major, partition order inside each year.
        assert [entry.simulation_id for entry in plan.baselines] == [
            "bl1-2026-1",
            "bl1-2026-2",
            "bl1-2027-1",
            "bl1-2027-2",
            "bl1-2025-1",
            "bl1-2025-2",
        ]
        assert [entry.exists for entry in plan.baselines] == [
            True,
            False,
            False,
            False,
            False,
            False,
        ]
        assert plan.baselines[0].group == ["state/ca"]
        assert plan.baselines[1].group == ["state/ny", "state/wv"]
        assert plan.baselines[1].path == "baselines/us/bl-2026-2/bl1-2026-2.h5"

        assert plan.receipt == BundleVersionIdentity(
            policyengine_version="4.22.0",
            model_version="1.0.0",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        )
        # An exists-flag inversion would make select_work recompute (or,
        # worse, skip) the wrong entries — lock the wiring end to end.
        work = precompute.select_work(plan, force=False)
        assert [entry.year for entry in work.datasets] == [2027, 2025]
        assert len(work.baselines) == 5

    @pytest.mark.usefixtures("planning_stubs")
    def test_empty_partition_fails_loudly(self, monkeypatch):
        from policyengine_simulation_executor import national_partition

        monkeypatch.setattr(
            national_partition, "national_region_groups", lambda _country: []
        )
        with pytest.raises(RuntimeError, match="No national partition"):
            precompute.plan_artifacts_impl("bucket-x")


def _us_state_region(state_code: str, state_fips: int, dataset_path: str):
    from policyengine.core.scoping_strategy import RowFilterStrategy

    return SimpleNamespace(
        code=f"state/{state_code.lower()}",
        dataset_path=dataset_path,
        scoping_strategy=RowFilterStrategy(
            variable_name="state_fips", variable_value=state_fips
        ),
        parent_code="us",
        state_code=state_code,
    )


class TestStateDatasetTargets:
    """Which states get baked files, resolved the way a request resolves them."""

    @pytest.fixture
    def regional_bundle(self):
        from policyengine_simulation_executor.release_bundle import (
            get_country_release_bundle,
        )

        bundle = get_country_release_bundle("us")
        regional = next(
            name for name in bundle.dataset_uris if name.endswith("_acs_local")
        )
        return SimpleNamespace(
            default_uri=bundle.default_dataset_uri,
            regional=regional,
            regional_uri=bundle.dataset_uris[regional],
        )

    def _install_regions(self, monkeypatch, regions):
        from policyengine_simulation_executor import simulation_runtime

        by_code = {region.code: region for region in regions}
        country_module = SimpleNamespace(
            model=SimpleNamespace(
                get_region=by_code.get,
                region_registry=SimpleNamespace(
                    get_by_type=lambda region_type: (
                        list(regions) if region_type == "state" else []
                    )
                ),
            )
        )
        monkeypatch.setattr(
            simulation_runtime, "_country_module", lambda _country: country_module
        )

    def test_targets_states_whose_requests_load_a_regional_dataset(
        self, monkeypatch, regional_bundle
    ):
        self._install_regions(
            monkeypatch,
            [
                _us_state_region("WY", 56, regional_bundle.regional_uri),
                _us_state_region("UT", 49, regional_bundle.regional_uri),
            ],
        )

        targets = precompute.state_dataset_targets()

        assert targets == [
            precompute.StateDatasetTarget(
                region="state/ut",
                state_code="UT",
                state_fips=49,
                filter_variable="state_fips",
                dataset=regional_bundle.regional,
            ),
            precompute.StateDatasetTarget(
                region="state/wy",
                state_code="WY",
                state_fips=56,
                filter_variable="state_fips",
                dataset=regional_bundle.regional,
            ),
        ]

    def test_skips_states_whose_requests_load_the_default_dataset(
        self, monkeypatch, regional_bundle
    ):
        self._install_regions(
            monkeypatch,
            [
                _us_state_region("UT", 49, regional_bundle.default_uri),
                _us_state_region("WY", 56, regional_bundle.regional_uri),
            ],
        )

        assert [target.state_code for target in precompute.state_dataset_targets()] == [
            "WY"
        ]

    def test_refuses_a_state_without_a_row_filter(self, monkeypatch, regional_bundle):
        unfiltered = _us_state_region("UT", 49, regional_bundle.regional_uri)
        unfiltered.scoping_strategy = None
        self._install_regions(monkeypatch, [unfiltered])

        with pytest.raises(RuntimeError, match="state/ut"):
            precompute.state_dataset_targets()


class TestCohortIdentityGuard:
    def test_diverged_predicates_fail_loudly(self, monkeypatch):
        from policyengine_simulation_executor import baseline_artifacts as ba
        from policyengine_simulation_executor import simulation_runtime as sr

        monkeypatch.setattr(sr, "_country_module", lambda _country: SimpleNamespace())
        monkeypatch.setattr(
            sr,
            "_resolve_region",
            lambda **_kwargs: SimpleNamespace(
                code="x", dataset_reference=None, scoping_strategy=None
            ),
        )
        monkeypatch.setattr(
            ba, "qualifying_baseline_identity", lambda *_args, **_kwargs: None
        )
        with pytest.raises(RuntimeError, match="diverged"):
            precompute.cohort_identity(2026, ["state/ca"])


class TestBuildDatasetImpl:
    @pytest.fixture
    def dataset_stubs(self, monkeypatch, tmp_path):
        from policyengine_simulation_executor import (
            artifact_keys,
            artifact_store,
            release_bundle,
        )
        from policyengine_simulation_executor import simulation_runtime as sr

        state = SimpleNamespace(
            folder=tmp_path,
            uploads=[],
            ensure_calls=[],
            write_file=True,
            identity=SimpleNamespace(
                digest="ds-2026",
                store_path="datasets/us/ds-2026/populace_year_2026.h5",
                filename="populace_year_2026.h5",
            ),
        )

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def upload_file(self, path, local_path):
                state.uploads.append((path, str(local_path)))
                return True

        def fake_ensure_datasets(*, datasets, years, data_folder):
            state.ensure_calls.append((datasets, years, data_folder))
            if state.write_file:
                (tmp_path / state.identity.filename).write_bytes(b"h5-bytes")

        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda _country, _year: state.identity,
        )
        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(sr, "resolve_data_folder", lambda: str(tmp_path))
        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda _country: SimpleNamespace(ensure_datasets=fake_ensure_datasets),
        )
        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda _country: SimpleNamespace(default_dataset="populace_cps"),
        )
        return state

    def _entry(self, path="datasets/us/ds-2026/populace_year_2026.h5"):
        return DatasetPlanEntry(
            year=2026,
            digest="ds-2026",
            path=path,
            filename="populace_year_2026.h5",
            exists=False,
        )

    def test_builds_and_uploads_under_the_planned_key(self, dataset_stubs):
        result = precompute.build_dataset_impl("bucket-x", self._entry())
        assert dataset_stubs.ensure_calls == [
            (["populace_cps"], [2026], str(dataset_stubs.folder))
        ]
        assert dataset_stubs.uploads == [
            (
                "datasets/us/ds-2026/populace_year_2026.h5",
                str(dataset_stubs.folder / "populace_year_2026.h5"),
            )
        ]
        assert result.year == 2026
        assert result.uploaded is True
        assert result.size_bytes == len(b"h5-bytes")

    def test_refuses_to_upload_under_a_mismatched_key(self, dataset_stubs):
        with pytest.raises(RuntimeError, match="refusing to upload"):
            precompute.build_dataset_impl(
                "bucket-x", self._entry(path="datasets/us/OTHER/file.h5")
            )
        assert dataset_stubs.uploads == []

    def test_fails_loudly_when_no_file_is_produced(self, dataset_stubs):
        dataset_stubs.write_file = False
        with pytest.raises(RuntimeError, match="produced no file"):
            precompute.build_dataset_impl("bucket-x", self._entry())
        assert dataset_stubs.uploads == []


class TestBuildStateDatasetsImpl:
    """Building, checking and uploading one year's per-state datasets."""

    YEAR = 2026

    @pytest.fixture
    def state_build(self, monkeypatch, tmp_path):
        from fixtures import state_dataset_sources as sources
        from policyengine.core.scoping_strategy import RowFilterStrategy
        from policyengine.tax_benefit_models import us as us_module
        from policyengine_simulation_executor import (
            artifact_keys,
            artifact_store,
        )
        from policyengine_simulation_executor import simulation_runtime as sr

        state = SimpleNamespace(
            uploads=[],
            existing=set(),
            ensure_calls=[],
            entity_frames=sources.source_entity_frames(),
            work_folder=tmp_path / "work",
        )

        def target(state_code, fips):
            return precompute.StateDatasetTarget(
                region=f"state/{state_code.lower()}",
                state_code=state_code,
                state_fips=fips,
                filter_variable="state_fips",
                dataset=sources.REGIONAL_DATASET,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_fips", variable_value=fips
                ),
            )

        state.targets = [
            target("UT", sources.UTAH_FIPS),
            target("WY", sources.WYOMING_FIPS),
        ]
        monkeypatch.setattr(
            precompute, "state_dataset_targets", lambda: list(state.targets)
        )

        def fake_state_identity(
            _country, year, *, dataset, state_code, state_fips, filter_variable
        ):
            digest = f"sd-{year}-{state_code}-{state_fips}-{filter_variable}"
            return SimpleNamespace(
                digest=digest,
                store_path=f"state-datasets/us/{digest}/{dataset}_year_{year}.h5",
                filename=f"{dataset}_year_{year}.h5",
            )

        monkeypatch.setattr(
            artifact_keys, "collect_state_dataset_identity", fake_state_identity
        )

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def upload_file(self, path, local_path):
                state.uploads.append((path, Path(local_path)))
                return path not in state.existing

        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)

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
            sr,
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
        state.sources = sources
        state.entry = lambda state_code, fips: self._entry(
            fake_state_identity, state_code, fips
        )
        return state

    def _entry(self, identity_for, state_code, fips):
        from fixtures.state_dataset_sources import REGIONAL_DATASET

        identity = identity_for(
            "us",
            self.YEAR,
            dataset=REGIONAL_DATASET,
            state_code=state_code,
            state_fips=fips,
            filter_variable="state_fips",
        )
        return StateDatasetPlanEntry(
            year=self.YEAR,
            state_code=state_code,
            region=f"state/{state_code.lower()}",
            dataset=REGIONAL_DATASET,
            digest=identity.digest,
            path=identity.store_path,
            filename=identity.filename,
            folder=f"state-datasets/{REGIONAL_DATASET}/{state_code}",
            exists=False,
        )

    def _build(self, state_build, entries):
        return precompute.build_state_datasets_impl(
            "bucket-x", self.YEAR, entries, work_folder=state_build.work_folder
        )

    def _both_states(self, state_build):
        return [
            state_build.entry("UT", state_build.sources.UTAH_FIPS),
            state_build.entry("WY", state_build.sources.WYOMING_FIPS),
        ]

    def test_writes_each_state_file_with_only_its_households(self, state_build):
        sources = state_build.sources
        results = self._build(state_build, self._both_states(state_build))

        files = dict(state_build.uploads)
        utah, wyoming = (files[entry.path] for entry in self._both_states(state_build))
        assert sources.stored_ids(utah, "household") == [1, 2]
        assert sources.stored_ids(utah, "person") == [1, 2, 3]
        assert sources.stored_ids(utah, "tax_unit") == [1, 2]
        assert sources.stored_ids(utah, "marital_unit") == [1, 2, 3]
        assert sources.stored_ids(wyoming, "household") == [3]
        assert sources.stored_ids(wyoming, "person") == [4]
        assert sources.stored_ids(wyoming, "spm_unit") == [3]
        assert [(result.state_code, result.year) for result in results] == [
            ("UT", 2026),
            ("WY", 2026),
        ]

    def test_keeps_every_weight_unchanged(self, state_build):
        sources = state_build.sources
        self._build(state_build, self._both_states(state_build))

        utah = state_build.uploads[0][1]
        assert sources.stored_weights(utah, "household") == [10.0, 20.0]
        assert sources.stored_weights(utah, "person") == [10.0, 11.0, 20.0]
        assert sources.stored_weights(utah, "marital_unit") == [10.0, 11.0, 20.0]

    def test_state_files_pass_dotpy_reuse_checks(self, state_build):
        from policyengine.tax_benefit_models.us.datasets import (
            _expected_legacy_spm_profile,
        )
        from policyengine.tax_benefit_models.us.legacy_inputs import (
            read_year_file_stamp,
            year_file_stamp,
            year_file_stamp_problem,
        )
        from policyengine.tax_benefit_models.us.legacy_spm.record import (
            year_file_problem,
        )

        self._build(state_build, self._both_states(state_build))

        for _path, local in state_build.uploads:
            assert read_year_file_stamp(local) == year_file_stamp()
            assert year_file_stamp_problem(local) is None
            assert (
                year_file_problem(
                    local,
                    _expected_legacy_spm_profile(state_build.sources.REGIONAL_DATASET),
                )
                is None
            )

    def test_dotpy_reuses_a_state_file_without_rebuilding(
        self, state_build, monkeypatch
    ):
        from policyengine.tax_benefit_models.us import datasets as us_datasets

        self._build(state_build, self._both_states(state_build))

        def refuse_to_rebuild(**_kwargs):
            msg = "ensure_datasets rebuilt a baked state file"
            raise AssertionError(msg)

        monkeypatch.setattr(us_datasets, "create_datasets", refuse_to_rebuild)
        utah_folder = state_build.uploads[0][1].parent
        loaded = us_datasets.ensure_datasets(
            datasets=[state_build.sources.REGIONAL_DATASET],
            years=[self.YEAR],
            data_folder=str(utah_folder),
        )

        (dataset,) = loaded.values()
        assert sorted(dataset.data.household["household_id"]) == [1, 2]

    def test_each_state_file_holds_exactly_the_rows_its_filter_keeps(self, state_build):
        from policyengine.tax_benefit_models.us.model import US_GROUP_ENTITIES

        entries = self._both_states(state_build)
        self._build(state_build, entries)
        stored = dict(state_build.uploads)

        for target, entry in zip(state_build.targets, entries, strict=True):
            expected = target.scoping_strategy.apply(
                entity_data=state_build.sources.source_entity_frames(),
                group_entities=US_GROUP_ENTITIES,
                year=self.YEAR,
            )
            for entity, frame in expected.items():
                assert state_build.sources.stored_ids(stored[entry.path], entity) == [
                    int(value) for value in frame[f"{entity}_id"]
                ], (target.state_code, entity)

    def test_prepares_the_full_country_year_file_once(self, state_build):
        self._build(state_build, self._both_states(state_build))

        assert len(state_build.ensure_calls) == 1
        datasets, years, folder = state_build.ensure_calls[0]
        assert datasets == [state_build.sources.REGIONAL_DATASET]
        assert years == [self.YEAR]
        assert Path(folder).is_relative_to(state_build.work_folder)

    def test_uploads_each_file_under_its_planned_path(self, state_build):
        entries = self._both_states(state_build)
        state_build.existing.add(entries[1].path)

        results = self._build(state_build, entries)

        assert [path for path, _local in state_build.uploads] == [
            entry.path for entry in entries
        ]
        assert [local.name for _path, local in state_build.uploads] == [
            entry.filename for entry in entries
        ]
        assert [result.uploaded for result in results] == [True, False]
        assert [result.path for result in results] == [entry.path for entry in entries]
        assert all(result.size_bytes > 0 for result in results)
        assert 0 <= results[0].build_seconds <= results[1].build_seconds

    def test_refuses_a_district_that_lies_outside_its_state(self, state_build):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=4903
        )

        with pytest.raises(RuntimeError, match="congressional district"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.uploads == []

    def test_a_household_without_a_district_code_does_not_stop_the_build(
        self, state_build
    ):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=0
        )

        results = self._build(state_build, self._both_states(state_build))

        assert [result.state_code for result in results] == ["UT", "WY"]

    def test_refuses_a_target_without_a_row_filter(self, state_build):
        from dataclasses import replace

        state_build.targets[0] = replace(state_build.targets[0], scoping_strategy=None)

        with pytest.raises(RuntimeError, match="no row filter"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.uploads == []

    def test_refuses_a_state_with_no_households(self, state_build):
        from policyengine.core.scoping_strategy import RowFilterStrategy

        state_build.targets.append(
            precompute.StateDatasetTarget(
                region="state/id",
                state_code="ID",
                state_fips=16,
                filter_variable="state_fips",
                dataset=state_build.sources.REGIONAL_DATASET,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_fips", variable_value=16
                ),
            )
        )

        with pytest.raises(ValueError, match="No households"):
            self._build(state_build, [state_build.entry("ID", 16)])
        assert state_build.uploads == []

    def test_refuses_a_plan_entry_whose_key_differs_in_the_container(self, state_build):
        entry = state_build.entry("UT", state_build.sources.UTAH_FIPS)
        mismatched = entry.model_copy(update={"path": "state-datasets/us/OTHER/x.h5"})

        with pytest.raises(RuntimeError, match="refusing to build"):
            self._build(state_build, [mismatched])
        assert state_build.ensure_calls == []

    def test_refuses_a_state_the_container_does_not_target(self, state_build):
        state_build.targets = state_build.targets[:1]

        with pytest.raises(RuntimeError, match="state/wy"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.ensure_calls == []

    def test_refuses_entries_for_another_year(self, state_build):
        entry = state_build.entry("UT", state_build.sources.UTAH_FIPS)

        with pytest.raises(ValueError, match="2027"):
            precompute.build_state_datasets_impl(
                "bucket-x",
                2027,
                [entry],
                work_folder=state_build.work_folder,
            )


class TestComputeBaselineImpl:
    @pytest.fixture
    def cohort_stubs(self, monkeypatch, tmp_path):
        from policyengine_simulation_executor import artifact_keys, artifact_store
        from policyengine_simulation_executor import simulation_runtime as sr
        from policyengine_simulation_executor.baseline_artifacts import (
            ArtifactBaselineSimulation,
        )

        state = SimpleNamespace(
            folder=tmp_path, downloads=[], uploads=[], configured=[]
        )

        class StubBaseline(ArtifactBaselineSimulation):
            def ensure(self):
                self._artifact_outcome = "miss"
                (tmp_path / f"{self.id}.h5").write_bytes(b"artifact-bytes")

        class SilentBaseline(ArtifactBaselineSimulation):
            def ensure(self):
                self._artifact_outcome = "miss"

        class SPMStubBaseline(ArtifactBaselineSimulation):
            """A baseline that names its artifact the way the wrapper does.

            The canonical wrapper derives ``storage_id`` from the resolved
            selection and saves under it; ``compute_baseline_impl`` plans a
            path from the identity's own derivation and refuses to act when
            the two disagree. Only a stub that computes its side
            independently can show the guard passing rather than aborting.
            """

            @property
            def spm_config(self):
                return state.spm

            @property
            def storage_id(self):
                return wrapper_storage_id(self.id, self.spm_config)

            def ensure(self):
                self._artifact_outcome = "miss"
                (tmp_path / f"{self.storage_id}.h5").write_bytes(b"artifact-bytes")

        state.spm = None
        state.baseline = StubBaseline.model_construct(id="bl1-cohort")
        state.make_silent = lambda: SilentBaseline.model_construct(id="bl1-cohort")
        state.make_spm = lambda sim_id="bl1-cohort": SPMStubBaseline.model_construct(
            id=sim_id
        )

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def download_file(self, path, local_path):
                state.downloads.append((path, str(local_path)))
                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                Path(local_path).write_bytes(b"dataset-bytes")

            def upload_file(self, path, local_path):
                state.uploads.append((path, str(local_path)))
                return True

        fake_analysis = ModuleType("policyengine.tax_benefit_models.us.analysis")
        fake_analysis.configure_budgetary_impact_variables = lambda baseline, reform: (
            state.configured.append((baseline, reform))
        )
        monkeypatch.setitem(
            sys.modules, "policyengine.tax_benefit_models.us.analysis", fake_analysis
        )

        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda _country, year: SimpleNamespace(
                store_path=f"datasets/us/ds-{year}/populace_year_{year}.h5",
                filename=f"populace_year_{year}.h5",
            ),
        )
        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(sr, "resolve_data_folder", lambda: str(tmp_path))
        monkeypatch.setattr(sr, "_country_module", lambda _country: SimpleNamespace())
        monkeypatch.setattr(
            sr,
            "_resolve_region",
            lambda **_kwargs: SimpleNamespace(
                code="state/ca+state/wv",
                scoping_strategy="scoping",
                dataset_reference=None,
            ),
        )
        monkeypatch.setattr(
            sr,
            "_load_dataset",
            lambda _params, country_module, selection: "dataset",
        )
        monkeypatch.setattr(
            sr, "_build_simulation", lambda _params, **_kwargs: state.baseline
        )
        return state

    def _entry(self, sim_id="bl1-cohort"):
        return BaselinePlanEntry(
            year=2026,
            group=["state/ca", "state/wv"],
            region="state/ca+state/wv",
            digest="bl-d",
            path=f"baselines/us/bl-d/{sim_id}.h5",
            simulation_id=sim_id,
            exists=False,
        )

    def test_downloads_dataset_configures_extras_and_uploads(self, cohort_stubs):
        result = precompute.compute_baseline_impl("bucket-x", self._entry())
        assert cohort_stubs.downloads == [
            (
                "datasets/us/ds-2026/populace_year_2026.h5",
                str(cohort_stubs.folder / "populace_year_2026.h5"),
            )
        ]
        # The extras configuration is load-bearing: without it every
        # production request fails the column guard and recomputes.
        assert cohort_stubs.configured == [
            (cohort_stubs.baseline, cohort_stubs.baseline)
        ]
        assert cohort_stubs.uploads == [
            (
                "baselines/us/bl-d/bl1-cohort.h5",
                str(cohort_stubs.folder / "bl1-cohort.h5"),
            )
        ]
        assert result.simulation_id == "bl1-cohort"
        assert result.outcome == "miss"
        assert result.uploaded is True
        assert result.size_bytes == len(b"artifact-bytes")

    def test_present_local_dataset_skips_download(self, cohort_stubs):
        (cohort_stubs.folder / "populace_year_2026.h5").write_bytes(b"already-here")
        precompute.compute_baseline_impl("bucket-x", self._entry())
        assert cohort_stubs.downloads == []

    def test_refuses_to_act_under_a_mismatched_id(self, cohort_stubs):
        with pytest.raises(RuntimeError, match="refusing to act"):
            precompute.compute_baseline_impl("bucket-x", self._entry("bl1-other"))
        assert cohort_stubs.uploads == []

    def test_refuses_a_mismatched_planned_storage_id(self, cohort_stubs):
        entry = self._entry()
        entry.path = "baselines/us/bl-d/bl1-cohort-spm-planned.h5"
        with pytest.raises(RuntimeError, match="storage ids disagree"):
            precompute.compute_baseline_impl("bucket-x", entry)
        assert cohort_stubs.configured == []
        assert cohort_stubs.uploads == []
        assert not (cohort_stubs.folder / "bl1-cohort.h5").exists()

    @staticmethod
    def _planned_identity(selection):
        """Return a real ``BaselineArtifactIdentity``, not a restated expression.

        The plan the deploy publishes comes from this object's properties,
        so keying the test off them is what makes the write path a
        writer==reader check: reformat or truncate ``storage_id``'s digest
        and the container -- which derives its own the wrapper's way -- no
        longer names the planned file.
        """
        from policyengine_simulation_executor.artifact_keys import (
            BaselineArtifactIdentity,
            DatasetArtifactIdentity,
        )

        return BaselineArtifactIdentity(
            spm=selection,
            country="us",
            region="state/ca+state/wv",
            scope_key="scoping",
            dataset=DatasetArtifactIdentity(
                spm=selection,
                country="us",
                dataset="populace_cps",
                stem="populace",
                year=2026,
                data_version="1.2.3",
                data_artifact_revision="rev-abc",
                source_sha256=None,
                data_build_fingerprint=None,
                model_version="9.9.9",
                policyengine_version="4.22.0",
            ),
        )

    def test_publishes_a_selection_scoped_artifact_under_the_planned_path(
        self, cohort_stubs
    ):
        """The canonical write path, end to end through the guard.

        Nothing else in hermetic CI runs ``compute_baseline_impl`` with a
        selection: the identity's storage id was only ever compared to
        itself, and the one case that reached the guard covered the abort.
        Here the plan is the planner's own ``BaselineArtifactIdentity`` and
        the container names its artifact the wrapper's way, so the guard
        passing at all is the claim -- if the two derivations differed, no
        canonical artifact could be published and the deploy would be
        blocked.
        """
        selection = {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        }
        planned = self._planned_identity(selection)
        cohort_stubs.spm = selection
        cohort_stubs.baseline = cohort_stubs.make_spm(planned.simulation_id)

        entry = self._entry(planned.simulation_id)
        entry.digest = planned.digest
        entry.path = planned.store_path

        result = precompute.compute_baseline_impl("bucket-x", entry)

        assert cohort_stubs.uploads == [
            (
                planned.store_path,
                str(cohort_stubs.folder / f"{planned.storage_id}.h5"),
            )
        ]
        assert result.simulation_id == planned.simulation_id
        assert result.uploaded is True
        assert result.size_bytes == len(b"artifact-bytes")
        # The digest the container computed is the one the planner planned.
        assert planned.storage_id == wrapper_storage_id(
            planned.simulation_id, selection
        )
        # A selection-free plan for the same cohort is a different artifact.
        assert planned.storage_id != planned.simulation_id

    def test_refuses_a_selection_scoped_plan_the_container_does_not_share(
        self, cohort_stubs
    ):
        """Abort when the container resolved a different SPM selection than the plan.

        This is the abort the review worried about, under a real selection: a
        container that resolved a different selection names a different
        artifact, and nothing is published.
        """
        cohort_stubs.spm = {"scenario": "zero_real"}
        cohort_stubs.baseline = cohort_stubs.make_spm()
        entry = self._entry()
        entry.path = (
            f"baselines/us/bl-d/bl1-cohort-spm-"
            f"{canonical_digest({'scenario': 'ce_trend'})}.h5"
        )
        with pytest.raises(RuntimeError, match="storage ids disagree"):
            precompute.compute_baseline_impl("bucket-x", entry)
        assert cohort_stubs.configured == []
        assert cohort_stubs.uploads == []

    def test_refuses_a_plain_simulation(self, cohort_stubs):
        cohort_stubs.baseline = SimpleNamespace(id="bl1-cohort")
        with pytest.raises(RuntimeError, match="plain Simulation"):
            precompute.compute_baseline_impl("bucket-x", self._entry())
        assert cohort_stubs.uploads == []

    def test_fails_loudly_when_ensure_leaves_no_artifact(self, cohort_stubs):
        cohort_stubs.baseline = cohort_stubs.make_silent()
        with pytest.raises(RuntimeError, match="left no artifact"):
            precompute.compute_baseline_impl("bucket-x", self._entry())
        assert cohort_stubs.uploads == []


class TestVerifyDeterminismImpl:
    """The determinism gate's comparator must never fail open.

    It is the pipeline's only correctness check.
    """

    @pytest.fixture
    def run_verification(self, monkeypatch, tmp_path):
        """Run the determinism gate on stubbed loaded and recomputed frames."""

        def verify(loaded, recomputed, *, outcome="hit", on_disk=True):
            downloads = []
            captured = []

            class FakeStore:
                def download_file(self, path, local_path):
                    downloads.append(path)
                    Path(local_path).write_bytes(b"artifact")

            class FakeBaseline:
                id = "bl1-verify"
                dataset = "dataset"
                tax_benefit_model_version = "model-version"
                scoping_strategy = "scoping"
                extra_variables: ClassVar[dict[str, int]] = {"extra": 1}
                artifact_outcome = None

                def ensure(self):
                    self.artifact_outcome = outcome
                    self.output_dataset = SimpleNamespace(
                        data=SimpleNamespace(entity_data=loaded)
                    )

            class FakeFresh:
                def __init__(self, **kwargs):
                    captured.append(kwargs)

                def run(self):
                    self.output_dataset = SimpleNamespace(
                        data=SimpleNamespace(entity_data=recomputed)
                    )

            monkeypatch.setattr(
                precompute,
                "_prepare_cohort_baseline",
                lambda _bucket, _expected: (FakeStore(), tmp_path, FakeBaseline()),
            )
            monkeypatch.setattr("policyengine.core.Simulation", FakeFresh)
            if on_disk:
                (tmp_path / "bl1-verify.h5").write_bytes(b"artifact")
            entry = BaselinePlanEntry(
                year=2026,
                group=["state/ca"],
                region="state/ca",
                digest="d",
                path="baselines/us/d/bl1-verify.h5",
                simulation_id="bl1-verify",
                exists=True,
            )
            verdict = precompute.verify_determinism_impl("bucket-x", entry)
            return verdict, downloads, captured

        return verify

    def _frames(self, age=(30.0, 40.0)):
        return {"person": pd.DataFrame({"age": list(age)})}

    def test_identical_frames_pass(self, run_verification):
        verdict, downloads, _ = run_verification(self._frames(), self._frames())
        assert verdict.equal is True
        assert verdict.differences == []
        assert downloads == []

    def test_value_difference_fails(self, run_verification):
        verdict, _, _ = run_verification(self._frames(), self._frames(age=(30.0, 41.0)))
        assert verdict.equal is False
        assert verdict.differences == ["person.age: values differ"]

    def test_dtype_difference_fails(self, run_verification):
        left = {"person": pd.DataFrame({"age": pd.array([30, 40], dtype="int64")})}
        right = {"person": pd.DataFrame({"age": pd.array([30.0, 40.0])})}
        verdict, _, _ = run_verification(left, right)
        assert verdict.equal is False
        assert verdict.differences == ["person.age: dtype differs"]

    def test_column_asymmetry_fails(self, run_verification):
        right = {"person": pd.DataFrame({"age": [30.0, 40.0], "income": [1.0, 2.0]})}
        verdict, _, _ = run_verification(self._frames(), right)
        assert verdict.equal is False
        assert verdict.differences == ["person.income: only on one side"]

    def test_missing_entity_fails(self, run_verification):
        verdict, _, _ = run_verification(self._frames(), {})
        assert verdict.equal is False
        assert verdict.differences == ["person: missing on one side"]

    def test_differences_are_truncated_but_verdict_stays_false(self, run_verification):
        left = {"person": pd.DataFrame({f"c{i}": [1.0] for i in range(60)})}
        right = {"person": pd.DataFrame({f"c{i}": [2.0] for i in range(60)})}
        verdict, _, _ = run_verification(left, right)
        assert verdict.equal is False
        assert len(verdict.differences) == 50

    def test_unloadable_artifact_fails_loudly(self, run_verification):
        with pytest.raises(RuntimeError, match="could not load the artifact"):
            run_verification(
                self._frames(),
                self._frames(),
                outcome="incomplete",
            )

    def test_absent_artifact_is_downloaded_first(self, run_verification):
        verdict, downloads, _ = run_verification(
            self._frames(), self._frames(), on_disk=False
        )
        assert downloads == ["baselines/us/d/bl1-verify.h5"]
        assert verdict.equal is True

    def test_fresh_run_reuses_the_baseline_inputs(self, run_verification):
        _, _, captured = run_verification(self._frames(), self._frames())
        assert captured == [
            {
                "dataset": "dataset",
                "tax_benefit_model_version": "model-version",
                "policy": None,
                "scoping_strategy": "scoping",
                "extra_variables": {"extra": 1},
            }
        ]
