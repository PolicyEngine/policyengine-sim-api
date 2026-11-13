"""Creating economy reports and simulations, with the database and Modal faked."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from economy_fixtures import STATE_DATASET
from economy_service_fakes import (
    CATALOG,
    CREATED_AT,
    DYNAMIC_ID,
    POLICY_ID,
    REFORM,
    REPORT_BODY,
    REUSED,
    FakeCaller,
    FakeStore,
    FakeWorkers,
    report_request,
    service,
    simulation_request,
)
from household_fixtures import POLICYENGINE_VERSION
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    ReportRunIds,
    Stage12InvocationContext,
)
from policyengine_simulation_entry.economy_api_models import EconomyReportRequest
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_entry.economy_workers import (
    EconomyWorkerUnavailableError,
)
from policyengine_simulation_entry.household_workers import (
    VersionManifestUnavailableError,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    EconomyReportDefinition,
    EconomyReportState,
    EconomySimulationDefinition,
    EconomySimulationState,
    FoundEconomyReport,
    FoundEconomySimulation,
    FoundReportRun,
    FoundSide,
    FoundSimulationRun,
    MissingPolicyParametersError,
    OverlappingReformError,
    ProposedSide,
    ReportRunState,
    ReportRunStatus,
    ReportSideError,
    RunVersions,
    SimulationChoice,
    SimulationRunState,
    SimulationStatus,
    UnknownCatalogVersionError,
    UnknownDatasetError,
    UnknownDynamicError,
    UnknownPolicyError,
    UnknownRegionError,
)
from sqlalchemy.exc import OperationalError

DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))
VERSIONS = RunVersions(
    country_package_version="2.2.1", policyengine_version=POLICYENGINE_VERSION
)
OBSERVED = {"observability_id": "observed"}


def _submit_report(submissions, request=None):
    return asyncio.run(
        submissions.submit_report(
            request or report_request(),
            request_id="observed",
            observability_context=OBSERVED,
        )
    )


def _submit_simulation(submissions, request=None):
    return asyncio.run(
        submissions.submit_simulation(
            request or simulation_request(),
            request_id="observed",
            observability_context=OBSERVED,
        )
    )


def test_a_report_is_created_by_key_and_its_coordinator_started() -> None:
    store = FakeStore()
    caller = FakeCaller()

    created = _submit_report(service(store, caller=caller))

    (request, (baseline, reform, reuse)) = store.report_reads[0]
    assert (request.country, request.policyengine_version, request.region_code) == (
        "us",
        POLICYENGINE_VERSION,
        "us",
    )
    assert (request.dataset_name, request.year) == (None, 2026)
    assert baseline == SimulationChoice(policy_id=None, dynamic_id=None)
    assert reform == SimulationChoice(policy_id=POLICY_ID, dynamic_id=DYNAMIC_ID)
    # Runs are found by key, not searched for (C1 · 24).
    assert reuse is None
    (report,) = store.reports
    spm = {"geography_kind": "national"}
    assert report.definition == EconomyReportDefinition(
        country="us",
        baseline=baseline,
        reform=reform,
        dataset_name=CATALOG.dataset.name,
        region_code=CATALOG.region.code,
        year=2026,
        budget_window_size=None,
        spm=spm,
    )
    assert (report.report_id, report.run_id) == (UUID(int=1), UUID(int=2))
    assert (report.baseline, report.reform) == (
        ProposedSide(simulation_id=UUID(int=3), simulation_run_id=UUID(int=4)),
        ProposedSide(simulation_id=UUID(int=5), simulation_run_id=UUID(int=6)),
    )
    assert report.tax_benefit_model_id == CATALOG.model_id
    assert report.population == CATALOG.population(2026)
    # Answer 4: the report stores no option but the SPM selection.
    assert (report.versions, report.inputs) == (VERSIONS, {"spm": spm})
    ((_, payload, context, observed),) = caller.report_runs
    assert observed == OBSERVED
    assert ReportExecutionInput.model_validate(payload).reform.policy == REFORM
    assert Stage12InvocationContext.model_validate(context).report_run == ReportRunIds(
        report_run_id=UUID(int=2),
        baseline_simulation_run_id=UUID(int=4),
        reform_simulation_run_id=UUID(int=6),
    )
    assert store.recorded_calls == [(UUID(int=2), "fc-coordinator")]
    assert created.model_dump() == {
        "report_id": UUID(int=1),
        "report_run_id": UUID(int=2),
        "status": ReportRunStatus.PENDING,
        "baseline_simulation_id": UUID(int=3),
        "baseline_simulation_run_id": UUID(int=4),
        "baseline_reused": False,
        "reform_simulation_id": UUID(int=5),
        "reform_simulation_run_id": UUID(int=6),
        "reform_reused": False,
    }


def _found_report(*, run_inserted: bool, status: ReportRunStatus) -> FoundEconomyReport:
    """Return a report another request created, with its run as told."""
    return FoundEconomyReport(
        report_id=UUID(int=90),
        report_inserted=False,
        run=FoundReportRun(
            id=UUID(int=91),
            inserted=run_inserted,
            status=status,
            baseline_simulation_run_id=REUSED.simulation_run_id,
            reform_simulation_run_id=UUID(int=93),
        ),
        baseline=FoundSide(
            simulation_id=REUSED.simulation_id,
            simulation_run_id=REUSED.simulation_run_id,
            run_inserted=False,
        ),
        reform=FoundSide(
            simulation_id=UUID(int=92),
            simulation_run_id=UUID(int=93),
            run_inserted=run_inserted,
        ),
    )


@pytest.mark.parametrize(
    "status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.SUCCEEDED],
)
def test_a_report_joining_a_run_answers_it_and_starts_no_call(
    status: ReportRunStatus,
) -> None:
    """An identical request ends on the same report and run, with no call."""
    store = FakeStore(found_report=_found_report(run_inserted=False, status=status))
    caller = FakeCaller()

    created = _submit_report(service(store, caller=caller))

    assert caller.report_runs == []
    assert (store.recorded_calls, store.failed_runs) == ([], [])
    assert created.model_dump() == {
        "report_id": UUID(int=90),
        "report_run_id": UUID(int=91),
        "status": status,
        "baseline_simulation_id": REUSED.simulation_id,
        "baseline_simulation_run_id": REUSED.simulation_run_id,
        "baseline_reused": True,
        "reform_simulation_id": UUID(int=92),
        "reform_simulation_run_id": UUID(int=93),
        "reform_reused": True,
    }


def test_a_side_whose_run_already_exists_is_named_and_answered_reused() -> None:
    """A new report run naming a simulation run another request inserted."""
    store = FakeStore(
        found_report=_found_report(run_inserted=True, status=ReportRunStatus.PENDING)
    )
    caller = FakeCaller()

    created = _submit_report(service(store, caller=caller))

    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert payload.baseline.simulation_execution_id == REUSED.simulation_run_id
    assert payload.reform.simulation_execution_id == UUID(int=93)
    assert store.recorded_calls == [(UUID(int=91), "fc-coordinator")]
    assert (created.baseline_reused, created.reform_reused) == (True, False)
    assert created.baseline_simulation_run_id == REUSED.simulation_run_id


def test_the_same_policy_on_both_sides_names_one_simulation_run_twice() -> None:
    store = FakeStore()
    caller = FakeCaller()

    created = _submit_report(
        service(store, caller=caller),
        report_request(reform={"policy_id": None, "dynamic_id": None}),
    )

    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert (
        payload.baseline.simulation_execution_id
        == payload.reform.simulation_execution_id
        == UUID(int=4)
    )
    assert created.baseline_simulation_run_id == created.reform_simulation_run_id


def test_the_report_stores_only_the_spm_settings_given() -> None:
    store = FakeStore()
    caller = FakeCaller()

    _submit_report(
        service(store, caller=caller),
        report_request(include_cliffs=False, spm={"scenario": "zero"}),
    )

    (report,) = store.reports
    assert report.inputs == {"spm": {"scenario": "zero"}}
    assert report.definition.spm == {"scenario": "zero"}
    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert payload.baseline.options == {"spm": {"scenario": "zero"}}


def test_a_report_without_spm_settings_stores_none() -> None:
    store = FakeStore()
    caller = FakeCaller()
    without_spm = {
        **{key: value for key, value in REPORT_BODY.items() if key != "spm"},
        "include_cliffs": False,
    }

    _submit_report(
        service(store, caller=caller),
        EconomyReportRequest.model_validate(without_spm),
    )

    (report,) = store.reports
    assert (report.inputs, report.definition.spm) == ({}, None)
    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert payload.reform.options == {}


def test_a_report_asking_for_cliffs_is_refused_before_anything_is_read() -> None:
    """Interim answer A: a 422 until per-run cliff calculations exist (C1 · 39)."""
    store = FakeStore()
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit_report(
            service(store, caller=caller), report_request(include_cliffs=True)
        )

    assert refusal.value.code == "cliffs_not_available"
    ((reason),) = refusal.value.reasons
    assert (reason.loc, reason.type) == (
        ("body", "include_cliffs"),
        "cliffs_not_available",
    )
    assert "cliff" in reason.msg
    assert (store.report_reads, store.reports, caller.report_runs) == ([], [], [])


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            UnknownCatalogVersionError("store text: no catalog version"),
            "unknown_catalog_version",
        ),
        (UnknownRegionError("store text: no region"), "unknown_region"),
        (UnknownDatasetError("store text: no dataset"), "unknown_dataset"),
        (UnknownPolicyError("store text: no policy"), "unknown_policy"),
        (UnknownDynamicError("store text: no dynamic"), "unknown_dynamic"),
    ],
)
def test_a_missing_row_is_refused_before_anything_is_written(error, code) -> None:
    store = FakeStore(read_failure=error)
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionRefusedError) as report_refusal:
        _submit_report(service(store, caller=caller))
    with pytest.raises(EconomySubmissionRefusedError) as simulation_refusal:
        _submit_simulation(service(store, caller=caller))

    assert report_refusal.value.code == simulation_refusal.value.code == code
    assert str(error) not in report_refusal.value.detail
    assert (store.reports, store.simulations, caller.report_runs) == ([], [], [])


def test_values_the_catalog_version_lacks_cannot_be_calculated() -> None:
    error = MissingPolicyParametersError(["gov.a", "gov.b"], source="dynamic")
    store = FakeStore(read_failure=error)

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit_simulation(service(store))

    assert refusal.value.code == "missing_parameters"
    assert [reason.loc for reason in refusal.value.reasons] == [
        ("body", "dynamic_id", "gov.a"),
        ("body", "dynamic_id", "gov.b"),
    ]
    assert POLICYENGINE_VERSION in refusal.value.reasons[0].msg
    assert store.simulations == []


def test_a_parameter_the_policy_and_dynamic_both_set_cannot_be_calculated() -> None:
    store = FakeStore(read_failure=OverlappingReformError(["gov.a"]))

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit_simulation(service(store))

    assert refusal.value.code == "overlapping_parameters"
    assert [reason.type for reason in refusal.value.reasons] == [
        "overlapping_parameter"
    ]


@pytest.mark.parametrize(
    ("dataset_name", "code"),
    [(None, "dataset_not_in_bundle"), ("enhanced_cps_2024", "unknown_dataset")],
)
def test_a_catalog_dataset_the_bundle_lacks_is_refused(dataset_name, code) -> None:
    catalog = replace(
        CATALOG, dataset=CatalogDataset(id=CATALOG.dataset.id, name="enhanced_cps_2024")
    )
    store = FakeStore(catalog=catalog)

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(service(store), report_request(dataset_name=dataset_name))

    assert refusal.value.code == code
    assert store.reports == []


def test_a_named_dataset_in_the_bundle_is_the_one_calculated() -> None:
    catalog = replace(
        CATALOG,
        dataset=CatalogDataset(id=CATALOG.dataset.id, name=STATE_DATASET.identity),
    )
    store = FakeStore(catalog=catalog)
    caller = FakeCaller()

    _submit_report(
        service(store, caller=caller),
        report_request(dataset_name=STATE_DATASET.identity),
    )

    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert payload.baseline.bundle.dataset.identity == STATE_DATASET.identity


def test_a_country_the_default_worker_cannot_calculate_is_refused() -> None:
    store = FakeStore()
    workers = FakeWorkers(EconomyWorkerUnavailableError("cannot calculate uk"))

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(service(store, workers=workers))

    assert refusal.value.code == "no_economy_worker"
    assert store.report_reads == []


def test_a_manifest_that_cannot_be_read_is_unavailable() -> None:
    failure = VersionManifestUnavailableError("unreadable")
    failure.__cause__ = ConnectionError("Modal")
    store = FakeStore()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _submit_report(service(store, workers=FakeWorkers(failure)))

    assert (unavailable.value.code, unavailable.value.record_id) == (
        "manifest_unavailable",
        None,
    )


@pytest.mark.parametrize("failing", ["read", "create"])
def test_a_database_that_cannot_be_used_is_unavailable_before_any_call(
    failing,
) -> None:
    store = FakeStore(**{f"{failing}_failure": DATABASE_ERROR})
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _submit_report(service(store, caller=caller))

    assert (unavailable.value.code, unavailable.value.record_id) == (
        "database_unavailable",
        None,
    )
    assert caller.report_runs == []


@pytest.mark.parametrize(
    ("column", "code"),
    [
        ("region_id", "unknown_region"),
        ("dataset_id", "unknown_dataset"),
        ("dynamic_id", "unknown_dynamic"),
        ("policy_id", "unknown_policy"),
        ("tax_benefit_model_version_id", "unknown_catalog_version"),
        ("baseline_simulation_run_id", "missing_reference"),
        (None, "missing_reference"),
    ],
)
def test_a_row_deleted_after_the_reads_is_refused_by_its_column(column, code) -> None:
    store = FakeStore(
        create_failure=EconomyRecordReferenceError(
            None if column is None else "simulations", column
        )
    )
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(service(store, caller=caller))

    assert refusal.value.code == code
    assert "no longer exists" in refusal.value.detail
    assert caller.report_runs == []


@pytest.mark.parametrize(
    "caller",
    [FakeCaller(failure=ConnectionError("refused")), FakeCaller(hang=True)],
    ids=["refused", "not accepted in time"],
)
def test_a_call_modal_does_not_confirm_fails_the_report_run_and_names_it(
    caller,
) -> None:
    store = FakeStore()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _submit_report(service(store, caller=caller))

    (report,) = store.reports
    assert (unavailable.value.code, unavailable.value.record_id) == (
        "modal_call_refused",
        report.report_id,
    )
    # Only the report run: once committed, another request's report run may
    # have joined the simulation runs created with it.
    ((run_id, simulation_run_ids, message),) = store.failed_runs
    assert run_id == report.run_id
    assert simulation_run_ids == ()
    assert message.startswith("modal_call_refused: Modal did not accept the report")
    assert store.recorded_calls == []


def test_a_report_run_its_coordinator_claimed_meanwhile_is_answered_created() -> None:
    store = FakeStore(claimed=True)

    created = _submit_report(
        service(store, caller=FakeCaller(failure=TimeoutError("late")))
    )

    assert store.failed_runs == []
    assert created.report_id == store.reports[0].report_id
    assert created.status is ReportRunStatus.PENDING


@pytest.mark.parametrize("failing", ["fail", "record"])
def test_a_run_whose_state_cannot_be_written_is_answered_created_pending(
    failing,
) -> None:
    store = FakeStore(**{f"{failing}_failure": DATABASE_ERROR})
    caller = FakeCaller(
        failure=ConnectionError("refused") if failing == "fail" else None
    )

    created = _submit_report(service(store, caller=caller))

    assert created.report_id == store.reports[0].report_id
    assert created.status is ReportRunStatus.PENDING


def test_a_simulation_is_created_and_its_run_started_alone() -> None:
    store = FakeStore()
    caller = FakeCaller()

    created = _submit_simulation(service(store, caller=caller))

    (request, choice) = store.simulation_reads[0]
    assert (request.policyengine_version, request.year) == (POLICYENGINE_VERSION, 2026)
    assert choice == SimulationChoice(policy_id=POLICY_ID, dynamic_id=None)
    (simulation,) = store.simulations
    assert simulation.definition == EconomySimulationDefinition(
        country="us",
        choice=choice,
        dataset_name=CATALOG.dataset.name,
        region_code=CATALOG.region.code,
        year=2026,
        spm=None,
    )
    assert (simulation.simulation_id, simulation.run_id) == (UUID(int=1), UUID(int=2))
    assert simulation.population == CATALOG.population(2026)
    assert simulation.versions == VERSIONS
    ((_, payload, context, observed),) = caller.simulation_runs
    assert payload["simulation_execution_id"] == str(simulation.run_id)
    assert payload["policy"] == REFORM
    assert context["simulation_run_id"] == str(simulation.run_id)
    assert observed == OBSERVED
    assert store.recorded_calls == [(simulation.run_id, "fc-worker")]
    assert created.model_dump() == {
        "simulation_id": simulation.simulation_id,
        "simulation_run_id": simulation.run_id,
        "status": SimulationStatus.PENDING,
    }


@pytest.mark.parametrize(
    "status", [SimulationStatus.PENDING, SimulationStatus.SUCCEEDED]
)
def test_a_simulation_joining_its_run_answers_it_and_starts_no_call(
    status: SimulationStatus,
) -> None:
    joined = FoundEconomySimulation(
        simulation_id=UUID(int=80),
        simulation_inserted=False,
        run=FoundSimulationRun(id=UUID(int=81), inserted=False, status=status),
    )
    store = FakeStore(found_simulation=joined)
    caller = FakeCaller()

    created = _submit_simulation(service(store, caller=caller))

    assert caller.simulation_runs == []
    assert (store.recorded_calls, store.failed_runs) == ([], [])
    assert created.model_dump() == {
        "simulation_id": UUID(int=80),
        "simulation_run_id": UUID(int=81),
        "status": status,
    }


def test_a_simulation_run_modal_refuses_is_failed_and_the_simulation_named() -> None:
    store = FakeStore()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _submit_simulation(
            service(store, caller=FakeCaller(failure=ConnectionError("refused")))
        )

    (simulation,) = store.simulations
    assert (unavailable.value.code, unavailable.value.record_id) == (
        "modal_call_refused",
        simulation.simulation_id,
    )
    assert store.failed_runs[0][0] == simulation.run_id
    assert store.failed_runs[0][2].startswith(
        "modal_call_refused: Modal did not accept the simulation"
    )


def test_the_calls_context_records_when_and_where_it_was_made() -> None:
    caller = FakeCaller()

    _submit_report(service(FakeStore(), caller=caller))

    context = Stage12InvocationContext.model_validate(caller.report_runs[0][2])
    assert (context.created_at, context.request_id, context.environment) == (
        CREATED_AT,
        "observed",
        "staging",
    )


REPORT_RUN = ReportRunState(
    id=UUID(int=11),
    status=ReportRunStatus.SUCCEEDED,
    country_package_version="2.2.1",
    policyengine_version=POLICYENGINE_VERSION,
    error_message=None,
    created_at=CREATED_AT,
    started_at=CREATED_AT,
    completed_at=CREATED_AT,
    baseline_simulation_run_id=UUID(int=12),
    reform_simulation_run_id=UUID(int=13),
)
STORED_REPORT = EconomyReportState(
    id=UUID(int=10),
    country="us",
    baseline_simulation_id=UUID(int=14),
    reform_simulation_id=UUID(int=15),
    run=REPORT_RUN,
)
STORED_SIMULATION = EconomySimulationState(
    id=UUID(int=20),
    run=SimulationRunState(
        id=UUID(int=21),
        status=SimulationStatus.FAILED,
        country_package_version="2.2.1",
        policyengine_version=POLICYENGINE_VERSION,
        error_message="simulation_failed: no",
        created_at=CREATED_AT,
        started_at=None,
        completed_at=CREATED_AT,
    ),
)


def test_a_report_and_a_simulation_are_read_as_they_stand() -> None:
    submissions = service(
        FakeStore(stored_report=STORED_REPORT, stored_simulation=STORED_SIMULATION)
    )

    assert asyncio.run(submissions.read_report(STORED_REPORT.id)) == STORED_REPORT
    assert (
        asyncio.run(submissions.read_simulation(STORED_SIMULATION.id))
        == STORED_SIMULATION
    )
    assert asyncio.run(submissions.read_report(UUID(int=99))) is None
    assert asyncio.run(submissions.read_simulation(UUID(int=99))) is None


def test_a_read_the_database_cannot_answer_is_unavailable() -> None:
    submissions = service(FakeStore(read_failure=DATABASE_ERROR))

    for read in (submissions.read_report, submissions.read_simulation):
        with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
            asyncio.run(read(UUID(int=1)))
        assert unavailable.value.code == "database_unavailable"


@pytest.mark.parametrize(
    ("side", "source", "field"),
    [("reform", "policy", "policy_id"), ("baseline", "dynamic", "dynamic_id")],
)
def test_a_reports_missing_parameters_are_located_on_their_side(
    side, source, field
) -> None:
    error = ReportSideError(
        side, MissingPolicyParametersError(["gov.a"], source=source)
    )

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit_report(service(FakeStore(read_failure=error)))

    assert refusal.value.code == "missing_parameters"
    assert [reason.loc for reason in refusal.value.reasons] == [
        ("body", side, field, "gov.a")
    ]


def test_a_reports_overlapping_parameters_are_located_on_their_side() -> None:
    error = ReportSideError("baseline", OverlappingReformError(["gov.a"]))

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit_report(service(FakeStore(read_failure=error)))

    assert [reason.loc for reason in refusal.value.reasons] == [
        ("body", "baseline", "dynamic_id", "gov.a")
    ]


# Text a store error carries, which no answer may pass on.
ERROR_TEXT = "internal text that must not be passed on"


@pytest.mark.parametrize(
    ("error", "request_fields", "code", "detail"),
    [
        (
            UnknownCatalogVersionError(ERROR_TEXT),
            {},
            "unknown_catalog_version",
            f"us has no catalog version for .py {POLICYENGINE_VERSION}, the "
            "default v2 worker's version.",
        ),
        (
            UnknownRegionError(ERROR_TEXT),
            {"region_code": "state/zz"},
            "unknown_region",
            f"us's catalog version for .py {POLICYENGINE_VERSION} has no region "
            "state/zz.",
        ),
        (
            UnknownDatasetError(ERROR_TEXT),
            {"dataset_name": "acs_2099"},
            "unknown_dataset",
            f"us's catalog version for .py {POLICYENGINE_VERSION} has no dataset "
            "acs_2099.",
        ),
        (
            UnknownDatasetError(ERROR_TEXT),
            {},
            "unknown_dataset",
            f"us's catalog version for .py {POLICYENGINE_VERSION} has no default "
            "dataset for region us.",
        ),
        (
            ReportSideError("reform", UnknownPolicyError(ERROR_TEXT)),
            {},
            "unknown_policy",
            "us has no stored policy the request's reform names.",
        ),
        (
            ReportSideError("baseline", UnknownDynamicError(ERROR_TEXT)),
            {},
            "unknown_dynamic",
            "No stored dynamic matches the one the request's baseline names.",
        ),
    ],
)
def test_a_refusals_detail_is_built_from_the_request_not_the_error(
    error, request_fields, code, detail
) -> None:
    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(
            service(FakeStore(read_failure=error)), report_request(**request_fields)
        )

    assert (refusal.value.code, refusal.value.detail) == (code, detail)
    assert ERROR_TEXT not in refusal.value.detail


def test_a_standalone_simulations_unknown_rows_name_its_own_fields() -> None:
    for error, detail in (
        (UnknownPolicyError(ERROR_TEXT), "us has no stored policy the request names."),
        (
            UnknownDynamicError(ERROR_TEXT),
            "No stored dynamic matches the one the request names.",
        ),
    ):
        with pytest.raises(EconomySubmissionRefusedError) as refusal:
            _submit_simulation(service(FakeStore(read_failure=error)))
        assert refusal.value.detail == detail


def test_a_country_without_an_economy_worker_is_refused_in_the_services_words() -> None:
    workers = FakeWorkers(EconomyWorkerUnavailableError(ERROR_TEXT))

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(service(FakeStore(), workers=workers))

    assert refusal.value.detail == (
        "The default v2 worker cannot calculate economy runs for us."
    )


def test_a_dataset_the_bundle_lacks_is_named_in_the_services_words() -> None:
    catalog = replace(
        CATALOG, dataset=CatalogDataset(id=CATALOG.dataset.id, name="enhanced_cps_2024")
    )

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit_report(service(FakeStore(catalog=catalog)))

    assert refusal.value.detail == (
        f"The default v2 worker, .py {POLICYENGINE_VERSION}, has no dataset "
        "enhanced_cps_2024."
    )


@pytest.mark.parametrize("record", ["report", "simulation"])
def test_a_create_whose_commit_is_unconfirmed_may_have_written_the_record(
    record,
) -> None:
    lost_commit = EconomyRecordOutcomeUnknownError("lost")
    lost_commit.__cause__ = DATABASE_ERROR
    store = FakeStore(create_failure=lost_commit)
    caller = FakeCaller()
    submit = _submit_report if record == "report" else _submit_simulation

    with pytest.raises(EconomySubmissionOutcomeUnknownError) as unknown:
        submit(service(store, caller=caller))

    assert (unknown.value.record, unknown.value.error_type) == (
        record,
        "OperationalError",
    )
    assert (caller.report_runs, caller.simulation_runs) == ([], [])
