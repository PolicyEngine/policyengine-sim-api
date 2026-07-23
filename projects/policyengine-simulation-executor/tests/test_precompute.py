"""Precompute library: planning, manifest assembly, orchestration, and the
writer==reader identity contract.

No Modal involved — the library takes function handles as parameters, so
the orchestration runs against plain fakes here. Everything structured is
a strict model from precompute_models; the fakes speak dicts, exactly
like Modal's serialization boundary does.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError

from fixtures.identity_stubs import install_identity_stubs
from policyengine_simulation_executor import precompute
from policyengine_simulation_executor.artifact_keys import canonical_digest
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
)


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
        """The Literal must track baseline_artifacts.OUTCOME_*: drift would
        surface as a ValidationError mid-precompute-run, not in review."""
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

    def test_years_are_in_priority_order(self):
        assert precompute.PRECOMPUTE_YEARS == [2026, 2027, 2025]

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

    def test_manifest_payload_is_wire_compatible(self):
        """The manifest is content-addressed: the typed model must digest
        identically to the raw dict shape already published to the store
        (key names included — note "schema", not "manifest_schema")."""
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


class TestRunPrecompute:
    def _functions(self, verify_result=None):
        return {
            "plan_artifacts": FakeFunction(result=_plan().model_dump()),
            "build_dataset": FakeFunction(
                results_by_year={2027: _dataset_result(2027)}
            ),
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
            "bucket-x", force=force, echo=lines.append, **functions
        )
        return digest, lines

    def test_spawns_only_misses_and_gates_on_first_computed(self):
        functions = self._functions()
        digest, lines = self._run(functions)

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
        install_identity_stubs(monkeypatch)

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
            lambda country: SimpleNamespace(
                model=SimpleNamespace(get_region=get_region)
            ),
        )

    def test_precompute_identity_equals_runtime_id(self, identity_stubs, fake_regions):
        from policyengine_simulation_executor import simulation_runtime as sr
        from policyengine_simulation_executor.baseline_artifacts import (
            deterministic_baseline_id,
        )

        group = ["state/ca", "state/wv"]
        writer_identity = precompute.cohort_identity(2026, group)

        params = precompute.cohort_params(2026, group)
        resolution = sr._resolve_region(
            country_module=sr._country_module("us"), country="us", params=params
        )
        reader_id = deterministic_baseline_id(
            params,
            country="us",
            policy=None,
            region_code=resolution.code,
            scoping_strategy=resolution.scoping_strategy,
            year=2026,
        )
        assert writer_identity.simulation_id == reader_id
        assert writer_identity.store_path.endswith(
            f"/{writer_identity.simulation_id}.h5"
        )

    def test_dataset_filename_matches_runtime_stem_lookup(self, monkeypatch):
        """Real-manifest coverage: the artifact filename equals the exact
        name the runtime's ensure_datasets existence check resolves."""
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
            lambda country: [["state/ca"], ["state/ny", "state/wv"]],
        )
        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda country, year: SimpleNamespace(
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
        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda country: SimpleNamespace(
                policyengine_version="4.22.0",
                model_version="1.0.0",
                data_version="1.2.3",
                data_artifact_revision="rev-abc",
                default_dataset="populace_cps",
            ),
        )
        return SimpleNamespace(existing=existing)

    def test_plan_enumerates_years_and_cohorts_with_presence(self, planning_stubs):
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

    def test_empty_partition_fails_loudly(self, planning_stubs, monkeypatch):
        from policyengine_simulation_executor import national_partition

        monkeypatch.setattr(
            national_partition, "national_region_groups", lambda country: []
        )
        with pytest.raises(RuntimeError, match="No national partition"):
            precompute.plan_artifacts_impl("bucket-x")


class TestCohortIdentityGuard:
    def test_diverged_predicates_fail_loudly(self, monkeypatch):
        from policyengine_simulation_executor import baseline_artifacts as ba
        from policyengine_simulation_executor import simulation_runtime as sr

        monkeypatch.setattr(sr, "_country_module", lambda country: SimpleNamespace())
        monkeypatch.setattr(
            sr,
            "_resolve_region",
            lambda **kwargs: SimpleNamespace(code="x", scoping_strategy=None),
        )
        monkeypatch.setattr(
            ba, "qualifying_baseline_identity", lambda *args, **kwargs: None
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
            artifact_keys, "collect_dataset_identity", lambda c, y: state.identity
        )
        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(sr, "resolve_data_folder", lambda: str(tmp_path))
        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda country: SimpleNamespace(ensure_datasets=fake_ensure_datasets),
        )
        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda country: SimpleNamespace(default_dataset="populace_cps"),
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

        state.baseline = StubBaseline.model_construct(id="bl1-cohort")
        state.make_silent = lambda: SilentBaseline.model_construct(id="bl1-cohort")

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
        fake_analysis.configure_budgetary_impact_variables = (
            lambda baseline, reform: state.configured.append((baseline, reform))
        )
        monkeypatch.setitem(
            sys.modules, "policyengine.tax_benefit_models.us.analysis", fake_analysis
        )

        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda country, year: SimpleNamespace(
                store_path=f"datasets/us/ds-{year}/populace_year_{year}.h5",
                filename=f"populace_year_{year}.h5",
            ),
        )
        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(sr, "resolve_data_folder", lambda: str(tmp_path))
        monkeypatch.setattr(sr, "_country_module", lambda country: SimpleNamespace())
        monkeypatch.setattr(
            sr,
            "_resolve_region",
            lambda **kwargs: SimpleNamespace(
                code="state/ca+state/wv",
                scoping_strategy="scoping",
                dataset_reference=None,
            ),
        )
        monkeypatch.setattr(
            sr,
            "_load_dataset",
            lambda params, country_module, region_resolution: "dataset",
        )
        monkeypatch.setattr(
            sr, "_build_simulation", lambda params, **kwargs: state.baseline
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
    """The gate's comparator: the pipeline's only correctness check must
    never fail open."""

    def _run(
        self,
        monkeypatch,
        tmp_path,
        loaded,
        recomputed,
        *,
        outcome="hit",
        on_disk=True,
    ):
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
            extra_variables = {"extra": 1}
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
            lambda bucket, expected: (FakeStore(), tmp_path, FakeBaseline()),
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

    def _frames(self, age=(30.0, 40.0)):
        return {"person": pd.DataFrame({"age": list(age)})}

    def test_identical_frames_pass(self, monkeypatch, tmp_path):
        verdict, downloads, _ = self._run(
            monkeypatch, tmp_path, self._frames(), self._frames()
        )
        assert verdict.equal is True
        assert verdict.differences == []
        assert downloads == []

    def test_value_difference_fails(self, monkeypatch, tmp_path):
        verdict, _, _ = self._run(
            monkeypatch, tmp_path, self._frames(), self._frames(age=(30.0, 41.0))
        )
        assert verdict.equal is False
        assert verdict.differences == ["person.age: values differ"]

    def test_dtype_difference_fails(self, monkeypatch, tmp_path):
        left = {"person": pd.DataFrame({"age": pd.array([30, 40], dtype="int64")})}
        right = {"person": pd.DataFrame({"age": pd.array([30.0, 40.0])})}
        verdict, _, _ = self._run(monkeypatch, tmp_path, left, right)
        assert verdict.equal is False
        assert verdict.differences == ["person.age: dtype differs"]

    def test_column_asymmetry_fails(self, monkeypatch, tmp_path):
        right = {"person": pd.DataFrame({"age": [30.0, 40.0], "income": [1.0, 2.0]})}
        verdict, _, _ = self._run(monkeypatch, tmp_path, self._frames(), right)
        assert verdict.equal is False
        assert verdict.differences == ["person.income: only on one side"]

    def test_missing_entity_fails(self, monkeypatch, tmp_path):
        verdict, _, _ = self._run(monkeypatch, tmp_path, self._frames(), {})
        assert verdict.equal is False
        assert verdict.differences == ["person: missing on one side"]

    def test_differences_are_truncated_but_verdict_stays_false(
        self, monkeypatch, tmp_path
    ):
        left = {"person": pd.DataFrame({f"c{i}": [1.0] for i in range(60)})}
        right = {"person": pd.DataFrame({f"c{i}": [2.0] for i in range(60)})}
        verdict, _, _ = self._run(monkeypatch, tmp_path, left, right)
        assert verdict.equal is False
        assert len(verdict.differences) == 50

    def test_unloadable_artifact_fails_loudly(self, monkeypatch, tmp_path):
        with pytest.raises(RuntimeError, match="could not load the artifact"):
            self._run(
                monkeypatch,
                tmp_path,
                self._frames(),
                self._frames(),
                outcome="incomplete",
            )

    def test_absent_artifact_is_downloaded_first(self, monkeypatch, tmp_path):
        verdict, downloads, _ = self._run(
            monkeypatch, tmp_path, self._frames(), self._frames(), on_disk=False
        )
        assert downloads == ["baselines/us/d/bl1-verify.h5"]
        assert verdict.equal is True

    def test_fresh_run_reuses_the_baseline_inputs(self, monkeypatch, tmp_path):
        _, _, captured = self._run(
            monkeypatch, tmp_path, self._frames(), self._frames()
        )
        assert captured == [
            {
                "dataset": "dataset",
                "tax_benefit_model_version": "model-version",
                "policy": None,
                "scoping_strategy": "scoping",
                "extra_variables": {"extra": 1},
            }
        ]
