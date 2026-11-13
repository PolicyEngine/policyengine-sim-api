"""Re-running stored economy reports and simulations; the store and Modal are faked."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from economy_rerun_fakes import (
    BASELINE_SIMULATION_ID,
    REFORM_SIMULATION_ID,
    REPORT_ID,
    RUN_UNDER_WAY_ID,
    SIMULATION_ID,
    STORED_OPTIONS,
    STORED_REPORT,
    STORED_SIMULATION,
    STORED_WINDOW,
    WINDOW_SIMULATION_IDS,
    FakeRerunStore,
    rerun_service,
)
from economy_service_fakes import (
    CATALOG,
    REFORM,
    FakeCaller,
)
from household_fixtures import POLICYENGINE_VERSION
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    ReportRunIds,
    Stage12InvocationContext,
)
from policyengine_simulation_entry.economy_api_models import ReportRerunRequest
from policyengine_simulation_entry.economy_submission_errors import (
    EconomyRecordNotFoundError,
    EconomyRerunConflictError,
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    FoundReportRun,
    FoundSimulationRun,
    MissingPolicyParametersError,
    NewReportRerun,
    NewSimulationRerun,
    ReportRunStatus,
    ReportSideError,
    RerunRecordDeletedError,
    RerunSide,
    ReusableRun,
    RunInProgressError,
    RunVersions,
    SharedRowNotSettledError,
    SimulationStatus,
    UnknownRegionError,
)
from sqlalchemy.exc import OperationalError
from test_budget_window_submissions import FakeAdvancer

DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))
VERSIONS = RunVersions(
    country_package_version="2.2.1", policyengine_version=POLICYENGINE_VERSION
)
OBSERVED = {"observability_id": "observed"}
REUSED_2027 = ReusableRun(
    simulation_id=UUID("00000000-0000-4000-8000-000000002027"),
    simulation_run_id=UUID("00000000-0000-4000-8000-0000000000b2"),
)
REUSED_BASELINE = ReusableRun(
    simulation_id=BASELINE_SIMULATION_ID,
    simulation_run_id=UUID("00000000-0000-4000-8000-0000000000b1"),
)


def _rerun_report(service, request: ReportRerunRequest | None = None):
    return asyncio.run(
        service.rerun_report(
            REPORT_ID,
            request or ReportRerunRequest(),
            request_id="observed",
            observability_context=OBSERVED,
        )
    )


def _rerun_simulation(service, simulation_id: UUID = SIMULATION_ID):
    return asyncio.run(
        service.rerun_simulation(
            simulation_id, request_id="observed", observability_context=OBSERVED
        )
    )


def test_a_report_with_no_keys_is_rerun_as_forced_runs_on_the_current_versions() -> (
    None
):
    """Interim answer B: its simulations' runs may have report-dependent plans."""
    store = FakeRerunStore(reusable=(REUSED_BASELINE, None))
    caller = FakeCaller()

    rerun = _rerun_report(rerun_service(store, caller=caller))

    ((request, (baseline, reform, reuse)),) = store.rerun_reads
    assert (request.country, request.policyengine_version, request.year) == (
        "us",
        POLICYENGINE_VERSION,
        2026,
    )
    assert (request.region_code, request.dataset_name) == ("us", "populace_us_2024")
    assert (baseline, reform) == (STORED_REPORT.baseline, STORED_REPORT.reform)
    assert reuse is None
    # Extra runs, marked forced, which nothing joins and no index refuses.
    assert store.report_reruns == [
        NewReportRerun(
            report_id=REPORT_ID,
            run_id=UUID(int=1),
            population=CATALOG.population(2026),
            versions=VERSIONS,
            baseline=RerunSide(
                simulation_id=BASELINE_SIMULATION_ID,
                simulation_run_id=UUID(int=2),
                reused=False,
            ),
            reform=RerunSide(
                simulation_id=REFORM_SIMULATION_ID,
                simulation_run_id=UUID(int=3),
                reused=False,
            ),
            forced=True,
        )
    ]
    ((_, payload, context, observed),) = caller.report_runs
    assert observed == OBSERVED
    report = ReportExecutionInput.model_validate(payload)
    assert report.evaluation_id == UUID(int=1)
    assert report.baseline.simulation_execution_id == UUID(int=2)
    assert (report.reform.simulation_execution_id, report.reform.policy) == (
        UUID(int=3),
        REFORM,
    )
    assert report.reform.options == STORED_OPTIONS
    assert Stage12InvocationContext.model_validate(context).report_run == ReportRunIds(
        report_run_id=UUID(int=1),
        baseline_simulation_run_id=UUID(int=2),
        reform_simulation_run_id=UUID(int=3),
    )
    assert store.recorded_calls == [(UUID(int=1), "fc-coordinator")]
    assert rerun.model_dump() == {
        "report_id": REPORT_ID,
        "report_run_id": UUID(int=1),
        "status": ReportRunStatus.PENDING,
        "baseline_simulation_id": BASELINE_SIMULATION_ID,
        "baseline_simulation_run_id": UUID(int=2),
        "baseline_reused": False,
        "reform_simulation_id": REFORM_SIMULATION_ID,
        "reform_simulation_run_id": UUID(int=3),
        "reform_reused": False,
    }


def test_a_forced_rerun_recalculates_both_simulations() -> None:
    store = FakeRerunStore(reusable=(REUSED_BASELINE, None))

    rerun = _rerun_report(rerun_service(store), ReportRerunRequest(force=True))

    assert store.rerun_reads[0][1][2] is None
    (created,) = store.report_reruns
    # An extra run, marked forced, which nothing joins (answer 3).
    assert created.forced is True
    assert (created.baseline.reused, created.reform.reused) == (False, False)
    assert created.new_simulation_run_ids == (UUID(int=2), UUID(int=3))
    assert created.baseline.simulation_id == BASELINE_SIMULATION_ID
    assert (rerun.baseline_reused, rerun.reform_reused) == (False, False)


@pytest.mark.parametrize(
    "status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.SUCCEEDED],
)
def test_a_report_rerun_joining_a_run_answers_it_and_starts_no_call(
    status: ReportRunStatus,
) -> None:
    """Answer 3: a run under way, or one that succeeded on these versions."""
    joined = FoundReportRun(
        id=UUID(int=90),
        inserted=False,
        status=status,
        baseline_simulation_run_id=UUID(int=91),
        reform_simulation_run_id=UUID(int=92),
    )
    store = FakeRerunStore(joined_report_run=joined)
    caller = FakeCaller()

    rerun = _rerun_report(rerun_service(store, caller=caller))

    assert caller.report_runs == []
    assert store.recorded_calls == []
    assert store.failed_runs == []
    assert rerun.model_dump() == {
        "report_id": REPORT_ID,
        "report_run_id": UUID(int=90),
        "status": status,
        "baseline_simulation_id": BASELINE_SIMULATION_ID,
        "baseline_simulation_run_id": UUID(int=91),
        "baseline_reused": True,
        "reform_simulation_id": REFORM_SIMULATION_ID,
        "reform_simulation_run_id": UUID(int=92),
        "reform_reused": True,
    }


def test_a_keyed_reports_rerun_has_each_side_join_its_run_on_the_versions() -> None:
    """Answers 4 and 5: a side joins its simulation's unfailed run, in any state."""
    joined_baseline_run = UUID(int=77)
    store = FakeRerunStore(
        report_definition=replace(STORED_REPORT, keyed=True),
        joined_side_runs={"baseline": joined_baseline_run},
    )
    caller = FakeCaller()

    rerun = _rerun_report(rerun_service(store, caller=caller))

    assert store.rerun_reads[0][1][2] is None
    (created,) = store.report_reruns
    assert created.sides_join_runs is True
    assert (created.baseline.reused, created.reform.reused) == (False, False)
    payload = ReportExecutionInput.model_validate(caller.report_runs[0][1])
    assert payload.baseline.simulation_execution_id == joined_baseline_run
    assert payload.reform.simulation_execution_id == created.reform.simulation_run_id
    assert (rerun.baseline_simulation_run_id, rerun.baseline_reused) == (
        joined_baseline_run,
        True,
    )
    assert (rerun.reform_simulation_run_id, rerun.reform_reused) == (
        created.reform.simulation_run_id,
        False,
    )


def test_a_keyed_rerun_with_one_simulation_on_both_sides_reuses_neither() -> None:
    """Review round 4: the one run this re-run inserted is named on both sides."""
    store = FakeRerunStore(
        report_definition=replace(
            STORED_REPORT,
            keyed=True,
            reform=STORED_REPORT.baseline,
        ),
        one_run_on_both_sides=True,
    )
    caller = FakeCaller()

    rerun = _rerun_report(rerun_service(store, caller=caller))

    (created,) = store.report_reruns
    assert rerun.baseline_simulation_run_id == rerun.reform_simulation_run_id
    assert rerun.baseline_simulation_run_id == created.baseline.simulation_run_id
    assert (rerun.baseline_reused, rerun.reform_reused) == (False, False)


def test_a_keyed_reports_forced_rerun_joins_nothing() -> None:
    """Answer 3: a forced run is an extra run, on new runs of both sides."""
    store = FakeRerunStore(
        report_definition=replace(STORED_REPORT, keyed=True),
        joined_report_run=FoundReportRun(
            id=UUID(int=90),
            inserted=False,
            status=ReportRunStatus.SUCCEEDED,
            baseline_simulation_run_id=UUID(int=91),
            reform_simulation_run_id=UUID(int=92),
        ),
    )
    caller = FakeCaller()

    rerun = _rerun_report(
        rerun_service(store, caller=caller), ReportRerunRequest(force=True)
    )

    (created,) = store.report_reruns
    assert (created.forced, created.sides_join_runs) == (True, False)
    assert created.new_simulation_run_ids == (UUID(int=2), UUID(int=3))
    assert rerun.report_run_id == UUID(int=1)
    assert (rerun.baseline_reused, rerun.reform_reused) == (False, False)
    assert len(caller.report_runs) == 1


def test_a_keyed_reports_refused_rerun_fails_only_the_report_run() -> None:
    """Its simulation runs may already be another report run's."""
    store = FakeRerunStore(report_definition=replace(STORED_REPORT, keyed=True))

    with pytest.raises(EconomySubmissionUnavailableError):
        _rerun_report(
            rerun_service(store, caller=FakeCaller(failure=ConnectionError("no")))
        )

    ((run_id, simulation_run_ids, _message),) = store.failed_runs
    assert run_id == store.report_reruns[0].run_id
    assert simulation_run_ids == ()


def test_an_unknown_report_or_simulation_is_not_found() -> None:
    store = FakeRerunStore(report_definition=None, simulation_definition=None)
    service = rerun_service(store)

    with pytest.raises(EconomyRecordNotFoundError) as report:
        _rerun_report(service)
    with pytest.raises(EconomyRecordNotFoundError) as simulation:
        _rerun_simulation(service)

    assert (report.value.record, simulation.value.record) == ("report", "simulation")


def test_a_report_kind_the_service_does_not_rerun_is_refused() -> None:
    store = FakeRerunStore(report_definition=replace(STORED_REPORT, kind="unsupported"))
    caller = FakeCaller()

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun_report(rerun_service(store, caller=caller))

    assert refused.value.code == "rerun_not_supported"
    assert "this type of" in refused.value.detail
    assert (store.rerun_reads, caller.report_runs) == ([], [])


@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ({"country": "fr"}, "country"),
        ({"baseline": None}, "baseline simulation"),
        ({"reform": None}, "reform simulation"),
        ({"region_code": None}, "region"),
        ({"dataset_name": None}, "dataset"),
        ({"year": None}, "year"),
    ],
)
def test_a_report_missing_a_row_it_calculates_on_is_not_rerun(missing, named) -> None:
    store = FakeRerunStore(report_definition=replace(STORED_REPORT, **missing))

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun_report(rerun_service(store))

    assert refused.value.code == "rerun_not_supported"
    assert named in refused.value.detail
    assert store.rerun_reads == []


def test_a_rerun_is_refused_while_a_run_is_under_way() -> None:
    store = FakeRerunStore(rerun_failure=RunInProgressError(RUN_UNDER_WAY_ID))
    caller = FakeCaller()
    service = rerun_service(store, caller=caller)

    with pytest.raises(EconomyRerunConflictError) as report:
        _rerun_report(service)
    with pytest.raises(EconomyRerunConflictError) as simulation:
        _rerun_simulation(service)

    for refused, record in ((report, "report"), (simulation, "simulation")):
        assert refused.value.code == "run_in_progress"
        assert refused.value.run_id == RUN_UNDER_WAY_ID
        assert f"The {record} has a run" in refused.value.detail
    assert (caller.report_runs, caller.simulation_runs) == ([], [])


def test_a_rerun_modal_refuses_fails_only_its_new_runs_and_names_them() -> None:
    store = FakeRerunStore(reusable=(REUSED_BASELINE, None))

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun_report(
            rerun_service(store, caller=FakeCaller(failure=ConnectionError("no")))
        )

    error = unavailable.value
    assert (error.code, error.record_id, error.run_id) == (
        "modal_call_refused",
        REPORT_ID,
        UUID(int=1),
    )
    assert "the report's new run" in error.detail
    ((run_id, simulation_run_ids, message),) = store.failed_runs
    assert (run_id, simulation_run_ids) == (UUID(int=1), (UUID(int=2), UUID(int=3)))
    assert message.startswith("modal_call_refused: Modal did not accept the report")


def test_a_rerun_its_coordinator_claimed_meanwhile_is_answered_created() -> None:
    store = FakeRerunStore(claimed=True)

    rerun = _rerun_report(
        rerun_service(store, caller=FakeCaller(failure=TimeoutError("late")))
    )

    assert store.failed_runs == []
    assert rerun.report_run_id == UUID(int=1)


def test_a_stored_policy_the_current_catalog_cannot_set_is_located_on_its_side() -> (
    None
):
    store = FakeRerunStore(
        inputs_failure=ReportSideError(
            "reform", MissingPolicyParametersError(("gov.old.parameter",))
        )
    )

    with pytest.raises(EconomySubmissionUnprocessableError) as refused:
        _rerun_report(rerun_service(store))

    assert refused.value.code == "missing_parameters"
    ((reason,),) = (refused.value.reasons,)
    assert reason.loc == ("report", "reform", "policy_id", "gov.old.parameter")
    assert POLICYENGINE_VERSION in reason.msg
    assert store.report_reruns == []


@pytest.mark.parametrize("stored", [STORED_REPORT, STORED_WINDOW])
def test_a_stored_report_asking_for_cliffs_is_not_rerun_until_cliffs_exist(
    stored,
) -> None:
    """Interim answer A: created before C1 · 27 with ``include_cliffs: true``."""
    store = FakeRerunStore(
        report_definition=replace(
            stored, inputs={**STORED_OPTIONS, "include_cliffs": True}
        )
    )
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionUnprocessableError) as refused:
        _rerun_report(rerun_service(store, caller=caller))

    assert refused.value.code == "cliffs_not_available"
    ((reason,),) = (refused.value.reasons,)
    assert reason.loc == ("report", "inputs", "include_cliffs")
    assert "cliff impacts" in reason.msg
    assert (store.rerun_reads, store.report_reruns, store.windows) == ([], [], [])
    assert caller.report_runs == []


def test_a_stored_report_whose_cliffs_were_not_asked_for_is_rerun() -> None:
    store = FakeRerunStore(
        report_definition=replace(
            STORED_REPORT, inputs={**STORED_OPTIONS, "include_cliffs": False}
        )
    )

    _rerun_report(rerun_service(store))

    assert len(store.report_reruns) == 1


def test_a_stored_region_the_current_catalog_lacks_is_refused_naming_the_report() -> (
    None
):
    store = FakeRerunStore(inputs_failure=UnknownRegionError("the region is gone"))

    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun_report(rerun_service(store))

    assert refused.value.code == "unknown_region"
    assert refused.value.detail == (
        f"us's catalog version for .py {POLICYENGINE_VERSION} has no region us, "
        "which the report names."
    )


def test_a_stored_dataset_the_bundle_lacks_is_refused() -> None:
    store = FakeRerunStore(
        catalog=replace(
            CATALOG, dataset=CatalogDataset(id=CATALOG.dataset.id, name="cps_2019")
        )
    )

    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun_report(rerun_service(store))

    assert refused.value.code == "dataset_not_in_bundle"
    assert "cps_2019" in refused.value.detail
    assert store.report_reruns == []


def test_a_database_that_cannot_be_read_is_unavailable_before_any_call() -> None:
    store = FakeRerunStore(read_failure=DATABASE_ERROR)
    caller = FakeCaller()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun_report(rerun_service(store, caller=caller))

    assert unavailable.value.code == "database_unavailable"
    assert caller.report_runs == []


@pytest.mark.parametrize("record", ["report", "simulation"])
def test_a_run_that_keeps_failing_before_it_is_read_is_answered_unavailable(
    record: str,
) -> None:
    """Nothing was written, so the caller is told to ask again (503)."""
    store = FakeRerunStore(rerun_failure=SharedRowNotSettledError("kept failing"))
    caller = FakeCaller()
    rerun = _rerun_report if record == "report" else _rerun_simulation

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        rerun(rerun_service(store, caller=caller))

    assert unavailable.value.code == "database_unavailable"
    assert (caller.report_runs, caller.simulation_runs) == ([], [])


def test_a_rerun_whose_commit_is_unconfirmed_may_have_written_its_run() -> None:
    store = FakeRerunStore(
        rerun_failure=EconomyRecordOutcomeUnknownError("commit failed")
    )
    store.rerun_failure.__cause__ = DATABASE_ERROR

    with pytest.raises(EconomySubmissionOutcomeUnknownError) as unknown:
        _rerun_report(rerun_service(store))

    assert (unknown.value.record, unknown.value.rerun) == ("report", True)
    assert unknown.value.error_type == "OperationalError"


def test_a_simulation_is_rerun_as_a_new_run_of_it() -> None:
    store = FakeRerunStore()
    caller = FakeCaller()

    rerun = _rerun_simulation(rerun_service(store, caller=caller))

    ((request, choice),) = store.simulation_reads
    assert (request.country, request.region_code, request.dataset_name) == (
        "us",
        "us",
        "populace_us_2024",
    )
    assert (request.policyengine_version, request.year) == (POLICYENGINE_VERSION, 2027)
    assert choice == STORED_SIMULATION.choice
    assert store.simulation_reruns == [
        NewSimulationRerun(
            simulation_id=SIMULATION_ID,
            run_id=UUID(int=1),
            population=CATALOG.population(2027),
            versions=VERSIONS,
        )
    ]
    ((_, payload, context, observed),) = caller.simulation_runs
    assert payload["simulation_execution_id"] == str(UUID(int=1))
    assert (payload["policy"], payload["year"]) == (REFORM, 2027)
    assert context["simulation_run_id"] == str(UUID(int=1))
    assert observed == OBSERVED
    assert store.recorded_calls == [(UUID(int=1), "fc-worker")]
    assert rerun.model_dump() == {
        "simulation_id": SIMULATION_ID,
        "simulation_run_id": UUID(int=1),
        "status": SimulationStatus.PENDING,
    }


@pytest.mark.parametrize(
    "status", [SimulationStatus.PENDING, SimulationStatus.SUCCEEDED]
)
def test_a_simulation_rerun_joining_a_run_answers_it_and_starts_no_call(
    status: SimulationStatus,
) -> None:
    """Answer 3: a run under way, or one that succeeded on these versions."""
    joined = FoundSimulationRun(id=UUID(int=99), inserted=False, status=status)
    store = FakeRerunStore(joined_simulation_run=joined)
    caller = FakeCaller()

    rerun = _rerun_simulation(rerun_service(store, caller=caller))

    assert caller.simulation_runs == []
    assert store.recorded_calls == []
    assert store.failed_runs == []
    assert rerun.model_dump() == {
        "simulation_id": SIMULATION_ID,
        "simulation_run_id": UUID(int=99),
        "status": status,
    }


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"kind": "household"}, "only through its report"),
        ({"kind": "unsupported"}, "this type of"),
        ({"country": None}, "country"),
        ({"country": "fr"}, "country"),
        ({"region_code": None}, "region"),
        ({"dataset_name": None}, "dataset"),
        ({"year": None}, "year"),
    ],
)
def test_a_simulation_the_service_cannot_rerun_is_refused(changes, named) -> None:
    store = FakeRerunStore(simulation_definition=replace(STORED_SIMULATION, **changes))

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun_simulation(rerun_service(store))

    assert refused.value.code == "rerun_not_supported"
    assert named in refused.value.detail
    assert store.simulation_reads == []


def test_a_simulation_rerun_modal_refuses_is_failed_and_named() -> None:
    store = FakeRerunStore()

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun_simulation(
            rerun_service(store, caller=FakeCaller(failure=ConnectionError("no")))
        )

    error = unavailable.value
    assert (error.code, error.record_id, error.run_id) == (
        "modal_call_refused",
        SIMULATION_ID,
        UUID(int=1),
    )
    assert store.failed_runs[0][0] == UUID(int=1)


def test_a_simulations_stored_policy_is_located_on_the_simulation() -> None:
    store = FakeRerunStore(
        inputs_failure=MissingPolicyParametersError(
            ("gov.old.elasticity",), source="dynamic"
        )
    )

    with pytest.raises(EconomySubmissionUnprocessableError) as refused:
        _rerun_simulation(rerun_service(store))

    ((reason,),) = (refused.value.reasons,)
    assert reason.loc == ("simulation", "dynamic_id", "gov.old.elasticity")


@pytest.mark.parametrize("record", ["report", "simulation"])
def test_a_record_deleted_after_its_reads_is_not_found(record) -> None:
    service = rerun_service(FakeRerunStore(rerun_failure=RerunRecordDeletedError()))
    rerun = _rerun_report if record == "report" else _rerun_simulation

    with pytest.raises(EconomyRecordNotFoundError) as missing:
        rerun(service)

    assert missing.value.record == record


@pytest.mark.parametrize(
    ("deleted", "code", "detail"),
    [
        (
            EconomyRecordReferenceError("simulation_runs", "dataset_id"),
            "unknown_dataset",
            "a dataset the report names no longer exists",
        ),
        (
            EconomyRecordReferenceError("simulation_runs", "simulation_id"),
            "missing_reference",
            "a simulation the report names no longer exists",
        ),
    ],
)
def test_a_row_deleted_after_a_reruns_reads_is_refused_naming_the_report(
    deleted, code, detail
) -> None:
    store = FakeRerunStore(rerun_failure=deleted)

    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun_report(rerun_service(store))

    assert (refused.value.code, refused.value.detail) == (code, detail)


def test_a_budget_window_is_rerun_as_a_new_window_run_and_its_first_years_started():
    """Interim answer B: a window has no keys yet, so every year is recalculated."""
    store = FakeRerunStore(
        report_definition=STORED_WINDOW, reusable_by_year={2027: (REUSED_2027, None)}
    )
    caller = FakeCaller()
    advancer = FakeAdvancer(store)

    rerun = _rerun_report(rerun_service(store, caller=caller, advancer=advancer))

    ((request, (baseline, reform, years, reuse)),) = store.rerun_reads
    assert (request.policyengine_version, request.year) == (POLICYENGINE_VERSION, 2026)
    assert (request.region_code, request.dataset_name) == ("us", "populace_us_2024")
    assert (baseline, reform, years) == (
        STORED_WINDOW.baseline.choice,
        STORED_WINDOW.reform.choice,
        STORED_WINDOW.window_years,
    )
    assert reuse is None
    (window,) = store.windows
    assert (window.report_id, window.window_run_id, window.recalculate) == (
        REPORT_ID,
        UUID(int=1),
        True,
    )
    assert (window.population, window.versions) == (CATALOG.population(2026), VERSIONS)
    assert [window_year.year for window_year in window.years] == [2026, 2027, 2028]
    for window_year in window.years:
        baseline_id, reform_id = WINDOW_SIMULATION_IDS[window_year.year]
        assert window_year.baseline.simulation_id == baseline_id
        assert window_year.reform.simulation_id == reform_id
    assert not any(window_year.baseline.reused for window_year in window.years)
    assert advancer.advances == [window.window_run_id]
    assert len(caller.report_runs) == 3
    for (year_to_start, _), (_, payload, context, observed) in zip(
        advancer.started, caller.report_runs, strict=True
    ):
        report = ReportExecutionInput.model_validate(payload)
        assert report.baseline.year == report.reform.year == year_to_start.year
        assert report.reform.policy == REFORM
        assert report.reform.options == STORED_OPTIONS
        assert Stage12InvocationContext.model_validate(context).report_run == (
            year_to_start.run_ids
        )
        assert observed == OBSERVED
    assert rerun.model_dump() == {
        "report_id": REPORT_ID,
        "report_run_id": window.years[0].report_run_id,
        "budget_window_run_id": window.window_run_id,
        "status": ReportRunStatus.PENDING,
        "years": [
            {
                "year": window_year.year,
                "report_run_id": window_year.report_run_id,
                "baseline_simulation_id": window_year.baseline.simulation_id,
                "baseline_simulation_run_id": window_year.baseline.simulation_run_id,
                "baseline_reused": window_year.baseline.reused,
                "reform_simulation_id": window_year.reform.simulation_id,
                "reform_simulation_run_id": window_year.reform.simulation_run_id,
                "reform_reused": False,
            }
            for window_year in window.years
        ],
    }


def test_a_forced_window_rerun_recalculates_every_year() -> None:
    store = FakeRerunStore(
        report_definition=STORED_WINDOW, reusable_by_year={2027: (REUSED_2027, None)}
    )

    rerun = _rerun_report(
        rerun_service(store, advancer=FakeAdvancer(store)),
        ReportRerunRequest(force=True),
    )

    assert store.rerun_reads[0][1][3] is None
    (window,) = store.windows
    assert window.recalculate is True
    assert not any(window_year.baseline.reused for window_year in window.years)
    assert not any(year.baseline_reused for year in rerun.years)


def test_a_window_rerun_whose_first_year_modal_refuses_names_the_window_run() -> None:
    store = FakeRerunStore(report_definition=STORED_WINDOW)
    caller = FakeCaller(failure=RuntimeError("Modal is unavailable"))

    with pytest.raises(EconomySubmissionUnavailableError) as refused:
        _rerun_report(rerun_service(store, caller=caller, advancer=FakeAdvancer(store)))

    error = refused.value
    (window,) = store.windows
    assert (error.code, error.record_id, error.run_id) == (
        "modal_call_refused",
        REPORT_ID,
        window.window_run_id,
    )
    assert "the report's new run" in error.detail


@pytest.mark.parametrize(
    "changes",
    [
        {"window_years": ()},
        {"budget_window_size": 4},
        # A year missing from the newest window run, as one naming no reform is.
        {"window_years": STORED_WINDOW.window_years[::2]},
        {
            "window_years": (
                STORED_WINDOW.window_years[0],
                STORED_WINDOW.window_years[2],
                STORED_WINDOW.window_years[1],
            )
        },
    ],
)
def test_a_window_whose_years_cannot_be_rerun_is_refused(changes) -> None:
    store = FakeRerunStore(report_definition=replace(STORED_WINDOW, **changes))

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun_report(rerun_service(store, advancer=FakeAdvancer(store)))

    assert refused.value.code == "rerun_not_supported"
    assert "complete budget-window calculation" in refused.value.detail
    assert (store.rerun_reads, store.windows) == ([], [])
