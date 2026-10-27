"""Tests for planning the artifact precompute, and its writer and reader identities."""

from types import SimpleNamespace

import pytest
from fixtures.identity_stubs import install_identity_stubs
from policyengine_simulation_executor import precompute, precompute_planning
from policyengine_simulation_executor.precompute_models import BundleVersionIdentity


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

        monkeypatch.setattr(
            precompute_planning, "cohort_identity", fake_cohort_identity
        )
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
        monkeypatch.setattr(
            precompute_planning, "state_dataset_targets", lambda: list(targets)
        )
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
        monkeypatch.setattr(precompute_planning, "state_dataset_targets", list)

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
