"""Tests for the precompute's typed schemas at the Modal serialization boundary.

The plan, manifest and orchestration through the level handlers are tested
in ``test_prebuilt_plan`` and ``test_prebuilt_precompute_run``.
"""

import pytest
from policyengine_simulation_executor import precompute
from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    PrecomputePlan,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.stage12_cache.levels import DatasetLevel
from pydantic import ValidationError


def _plan() -> PrecomputePlan:
    national_path = "year-files/us/d1/populace_year_2026.h5"
    return PrecomputePlan(
        country="us",
        year_files=[
            YearFilePlanEntry(
                country="us",
                level=DatasetLevel.NATIONAL,
                year=2026,
                dataset="populace",
                region=None,
                subset_code=None,
                digest="d1",
                path=national_path,
                filename="populace_year_2026.h5",
                folder="prebuilt/us/populace/all",
                exists=False,
            )
        ],
        baselines=[
            BaselinePlanEntry(
                country="us",
                level=DatasetLevel.STATE_GROUP,
                year=2026,
                region="region_group/state/ca",
                label="group 16 of 20",
                request={
                    "country": "us",
                    "scope": "macro",
                    "region_group": ["state/ca"],
                    "time_period": 2026,
                },
                year_file_path=national_path,
                year_file_filename="populace_year_2026.h5",
                folder="prebuilt/us/populace/all",
                digest="b1",
                path="baselines/us/b1/bl3-aaaa.h5",
                simulation_id="bl3-aaaa",
                storage_id="bl3-aaaa",
                exists=False,
            )
        ],
        receipt=BundleVersionIdentity(
            policyengine_version="4.22.0",
            policyengine_core_version="3.33.3",
            model_version="1.0.0",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace",
        ),
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

    def test_an_unknown_level_is_rejected(self):
        payload = _plan().model_dump()
        payload["year_files"][0]["level"] = "county"
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

    def test_the_manifest_schema_label(self):
        assert precompute.MANIFEST_SCHEMA == "mf2"


def test_us_years_are_in_priority_order_and_cover_the_runtime_default_year():
    """The runtime default year must be one of the precomputed years.

    The image bakes exactly the precomputed years, so otherwise every
    default request pays a runtime dataset build.
    """
    from policyengine_simulation_executor.simulation_runtime import DEFAULT_YEAR
    from policyengine_simulation_executor.stage12_cache.levels_us import (
        US_PREBUILT_YEARS,
    )

    assert US_PREBUILT_YEARS == (2026, 2027, 2025)
    assert DEFAULT_YEAR in US_PREBUILT_YEARS


def test_a_malformed_worker_result_fails_loudly():
    plan = _plan()

    class _Handle:
        def get(self):
            return {"year": 2026, "unexpected": "shape"}

    class _Function:
        def remote(self, *_arguments):
            return plan.model_dump()

        def spawn(self, *_arguments):
            return _Handle()

    function = _Function()
    with pytest.raises(ValidationError):
        precompute.run_precompute(
            "bucket-x",
            country="us",
            force=False,
            functions=precompute.PrecomputeFunctions(
                plan_artifacts=function,
                build_dataset=function,
                build_state_datasets=function,
                compute_baseline=function,
                verify_determinism=function,
                publish_manifest=function,
            ),
            echo=lambda _line: None,
        )
