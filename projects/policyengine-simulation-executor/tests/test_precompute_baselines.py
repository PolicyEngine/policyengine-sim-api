"""Tests for computing the precompute's cohort baselines and checking determinism."""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from fixtures.wrapper_spm import wrapper_storage_id
from policyengine_simulation_executor import precompute, precompute_baselines
from policyengine_simulation_executor.artifact_keys import canonical_digest
from policyengine_simulation_executor.precompute_models import BaselinePlanEntry


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
            "resolve_region",
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
                precompute_baselines,
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
