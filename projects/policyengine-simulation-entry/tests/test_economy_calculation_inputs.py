"""What the coordinator or a simulation function is given to calculate a run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from economy_fixtures import STATE_DATASET, economy_manifest
from household_fixtures import POLICYENGINE_VERSION
from policyengine_simulation_contract.stage12_execution import (
    ReportAggregate,
    ReportExecutionInput,
    ReportRunIds,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    SIMULATION_RUN_ID_FIELD,
    CalculationScope,
    CallOrigin,
    DatasetNotInBundleError,
    bundle_dataset,
    report_run_calculation,
    simulation_options,
    simulation_run_calculation,
)
from policyengine_simulation_entry.economy_workers import default_economy_worker

REPORT_RUN_ID = UUID("00000000-0000-4000-8000-000000000001")
BASELINE_RUN_ID = UUID("00000000-0000-4000-8000-000000000002")
REFORM_RUN_ID = UUID("00000000-0000-4000-8000-000000000003")
CREATED_AT = datetime(2026, 11, 12, 15, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REFORM = {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
ORIGIN = CallOrigin(
    environment="staging",
    modal_environment="staging",
    request_id="observed-request",
    created_at=CREATED_AT,
)
RUN_IDS = ReportRunIds(
    report_run_id=REPORT_RUN_ID,
    baseline_simulation_run_id=BASELINE_RUN_ID,
    reform_simulation_run_id=REFORM_RUN_ID,
)


def _worker():
    return default_economy_worker(economy_manifest(), "us")


def _scope(**changes) -> CalculationScope:
    values = {
        "country": "us",
        "region_code": "state/ca",
        "dataset": STATE_DATASET,
        "year": 2026,
        "options": {"include_cliffs": True},
    }
    return CalculationScope(**{**values, **changes})


def test_the_bundle_dataset_is_the_one_the_catalog_dataset_names() -> None:
    worker = _worker()

    assert bundle_dataset(worker, STATE_DATASET.identity) == STATE_DATASET
    with pytest.raises(DatasetNotInBundleError, match="enhanced_cps_2024"):
        bundle_dataset(worker, "enhanced_cps_2024")


def test_the_options_hold_cliffs_only_when_asked_and_the_spm_selection_given() -> None:
    assert simulation_options(include_cliffs=False, spm=None) == {}
    assert simulation_options(
        include_cliffs=True, spm={"geography_kind": "national"}
    ) == {"include_cliffs": True, "spm": {"geography_kind": "national"}}


def test_a_report_run_is_named_by_its_run_ids_with_no_evaluation_record() -> None:
    worker = _worker()

    calculation = report_run_calculation(
        worker,
        _scope(),
        ORIGIN,
        run_ids=RUN_IDS,
        baseline_policy={},
        reform_policy=REFORM,
    )

    report = ReportExecutionInput.model_validate(calculation.payload)
    assert report.evaluation_id == REPORT_RUN_ID
    assert report.requested_aggregates == tuple(ReportAggregate)
    for side, role, run_id, policy in (
        (report.baseline, SimulationRole.BASELINE, BASELINE_RUN_ID, {}),
        (report.reform, SimulationRole.REFORM, REFORM_RUN_ID, REFORM),
    ):
        assert (side.role, side.simulation_execution_id, side.policy) == (
            role,
            run_id,
            policy,
        )
        assert side.evaluation_id == REPORT_RUN_ID
        assert side.year == 2026
        assert (side.geography.country, side.geography.region) == ("us", "state/ca")
        assert side.options == {"include_cliffs": True}
        assert side.population.artifact.uri == STATE_DATASET.uri
        assert side.population.artifact.content_sha256 == STATE_DATASET.sha256
        assert side.bundle.policyengine_version == POLICYENGINE_VERSION
        assert side.bundle.country_package_version == "2.2.1"
        assert side.bundle.dataset.identity == STATE_DATASET.identity
        assert side.bundle.dataset.artifact_revision == "acs-local-revision"
        assert side.bundle.bundle_manifest_sha256 == worker.bundle_manifest_sha256

    context = Stage12InvocationContext.model_validate(calculation.context)
    assert context.report_run == RUN_IDS
    assert context.production_function_call_id is None
    assert (context.environment, context.modal_environment, context.request_id) == (
        "staging",
        "staging",
        "observed-request",
    )
    assert context.worker_version == worker.version
    assert context.modal_application == worker.application_name
    assert context.simulation_callable == "run_single_simulation_us"
    assert context.version_manifest_sha256 == worker.version_manifest_sha256
    assert context.artifact_prefix == f"stage-12-runs/staging/2026/11/{REPORT_RUN_ID}"
    assert context.retention_expires_at - context.created_at == timedelta(days=30)


def test_a_standalone_run_is_named_in_its_functions_context() -> None:
    worker = _worker()

    calculation = simulation_run_calculation(
        worker,
        _scope(options={}),
        ORIGIN,
        simulation_run_id=BASELINE_RUN_ID,
        policy=REFORM,
    )

    simulation = SimulationExecutionInput.model_validate(calculation.payload)
    assert simulation.role is SimulationRole.STANDALONE
    assert simulation.simulation_execution_id == BASELINE_RUN_ID
    assert simulation.evaluation_id == BASELINE_RUN_ID
    assert simulation.policy == REFORM
    assert calculation.context[SIMULATION_RUN_ID_FIELD] == str(BASELINE_RUN_ID)
    assert calculation.context["report_run"] is None
    assert calculation.context["artifact_prefix"] == (
        f"stage-12-runs/staging/2026/11/{BASELINE_RUN_ID}"
    )
