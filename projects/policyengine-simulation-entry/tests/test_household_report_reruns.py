"""Re-running a stored household report; the stores and Modal are faked.

The re-run service reads the stored report through the economy store and
hands a household report to the household report service, which reads its
household and policies, creates its new runs and starts its household
function call.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from economy_rerun_fakes import (
    REPORT_ID,
    RUN_UNDER_WAY_ID,
    FakeRerunStore,
    rerun_service,
)
from economy_service_fakes import FakeCaller
from household_report_fakes import (
    CATALOG_VERSION_ID,
    CTC_AMOUNT,
    HOUSEHOLD_ID,
    POLICY_ID,
    REFORM,
    SITUATION,
    FakeHouseholdReportCaller,
    FakeHouseholdReportStore,
    FakeHouseholdWorkers,
    household_rerun_service,
    manifest_household_worker,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
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
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
)
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    HouseholdRerunSide,
    MissingPolicyParametersError,
    NewHouseholdReportRerun,
    ReportDefinition,
    ReportRunStatus,
    ReportSideError,
    RerunRecordDeletedError,
    RunInProgressError,
    RunVersions,
    SideDefinition,
    SimulationChoice,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from sqlalchemy.exc import OperationalError

DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))
CONTEXT = {"observability_id": "observed"}
BASELINE_SIMULATION_ID = UUID("00000000-0000-4000-8000-0000000000e1")
REFORM_SIMULATION_ID = UUID("00000000-0000-4000-8000-0000000000e2")
# The household report service's IDs, in the order it takes them: the new
# report run, then each side's new simulation run.
RUN_ID, BASELINE_RUN_ID, REFORM_RUN_ID = (UUID(int=number) for number in range(1, 4))
# The stored report calculates 2025, which its household's default year
# (2026, in the fake store) no longer is.
STORED_HOUSEHOLD_REPORT = ReportDefinition(
    id=REPORT_ID,
    country="us",
    kind="household",
    year=2025,
    region_code=None,
    dataset_name=None,
    inputs={},
    baseline=SideDefinition(
        simulation_id=BASELINE_SIMULATION_ID,
        choice=SimulationChoice(policy_id=None, dynamic_id=None),
    ),
    reform=SideDefinition(
        simulation_id=REFORM_SIMULATION_ID,
        choice=SimulationChoice(policy_id=POLICY_ID, dynamic_id=None),
    ),
    household_id=HOUSEHOLD_ID,
)
BASELINE_ALONE = replace(STORED_HOUSEHOLD_REPORT, reform=None)


def _services(
    definition: ReportDefinition = STORED_HOUSEHOLD_REPORT,
    *,
    households: FakeHouseholdReportStore | None = None,
    workers: FakeHouseholdWorkers | None = None,
    caller: FakeHouseholdReportCaller | None = None,
):
    """Return the re-run service, its economy store and its economy caller."""
    economy_store = FakeRerunStore(report_definition=definition)
    economy_caller = FakeCaller()
    service = rerun_service(
        economy_store,
        caller=economy_caller,
        household_reports=household_rerun_service(
            households or FakeHouseholdReportStore(),
            workers=workers,
            caller=caller,
        ),
    )
    return service, economy_store, economy_caller


def _rerun(service, *, force: bool = False):
    return asyncio.run(
        service.rerun_report(
            REPORT_ID,
            ReportRerunRequest(force=force),
            request_id="observed",
            observability_context=CONTEXT,
        )
    )


def _side_request(calculation_id: UUID, parameters: dict) -> dict:
    worker = manifest_household_worker("us")
    return {
        "schema_version": 1,
        "calculation_id": str(calculation_id),
        "country": "us",
        "year": "2025",
        "household": SITUATION,
        "policy": {"parameters": parameters},
        "add_missing_variables": True,
        "spm_selection_source": "request",
        "bundle": worker.bundle.model_dump(mode="json"),
    }


# A household run is never reused, so a forced re-run creates the same runs.
@pytest.mark.parametrize("force", [False, True])
def test_a_household_report_is_rerun_as_new_runs_of_its_own_simulations(
    force,
) -> None:
    households = FakeHouseholdReportStore()
    caller = FakeHouseholdReportCaller()
    service, economy_store, economy_caller = _services(
        households=households, caller=caller
    )
    worker = manifest_household_worker("us")

    rerun = _rerun(service, force=force)

    assert households.reads == [
        {
            "country": "us",
            "household_id": HOUSEHOLD_ID,
            "policy_ids": {"baseline": None, "reform": POLICY_ID},
            "policyengine_version": worker.bundle.policyengine_version,
        }
    ]
    assert households.report_reruns == [
        NewHouseholdReportRerun(
            report_id=REPORT_ID,
            run_id=RUN_ID,
            tax_benefit_model_version_id=CATALOG_VERSION_ID,
            year=2025,
            versions=RunVersions(
                country_package_version=worker.bundle.country_package_version,
                policyengine_version=worker.bundle.policyengine_version,
            ),
            baseline=HouseholdRerunSide(
                simulation_id=BASELINE_SIMULATION_ID, simulation_run_id=BASELINE_RUN_ID
            ),
            reform=HouseholdRerunSide(
                simulation_id=REFORM_SIMULATION_ID, simulation_run_id=REFORM_RUN_ID
            ),
        )
    ]
    ((called_worker, payload, report_run_id, context),) = caller.calls
    assert called_worker == worker
    assert (report_run_id, context) == (RUN_ID, CONTEXT)
    assert payload == {
        "schema_version": 1,
        "baseline": _side_request(BASELINE_RUN_ID, {}),
        "reform": _side_request(REFORM_RUN_ID, REFORM),
    }
    HouseholdReportRunCalculation.model_validate(payload)
    assert households.recorded_calls == [(RUN_ID, "fc-household-report")]
    assert rerun == HouseholdReportCreated(
        report_id=REPORT_ID,
        report_run_id=RUN_ID,
        status=ReportRunStatus.PENDING,
        year=2025,
        baseline_simulation_id=BASELINE_SIMULATION_ID,
        baseline_simulation_run_id=BASELINE_RUN_ID,
        reform_simulation_id=REFORM_SIMULATION_ID,
        reform_simulation_run_id=REFORM_RUN_ID,
    )
    # Nothing of it goes through the economy store or coordinator.
    assert economy_store.rerun_reads == []
    assert economy_caller.report_runs == []


def test_a_household_report_of_the_baseline_alone_is_rerun_without_a_reform() -> None:
    households = FakeHouseholdReportStore()
    caller = FakeHouseholdReportCaller()
    service, _, _ = _services(BASELINE_ALONE, households=households, caller=caller)

    rerun = _rerun(service)

    (read,) = households.reads
    assert read["policy_ids"] == {"baseline": None}
    (created,) = households.report_reruns
    assert created.reform is None
    assert created.simulation_run_ids == (BASELINE_RUN_ID,)
    ((_, payload, _, _),) = caller.calls
    assert payload["reform"] is None
    assert (rerun.reform_simulation_id, rerun.reform_simulation_run_id) == (None, None)


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"household_id": None}, "household"),
        ({"baseline": None}, "baseline simulation"),
        ({"year": None}, "year"),
        ({"country": "fr"}, "country"),
    ],
)
def test_a_household_report_the_service_cannot_rerun_is_refused(changes, named) -> None:
    households = FakeHouseholdReportStore()
    service, _, _ = _services(
        replace(STORED_HOUSEHOLD_REPORT, **changes), households=households
    )

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun(service)

    assert refused.value.code == "rerun_not_supported"
    assert named in refused.value.detail
    assert households.reads == []


def test_a_household_rerun_answers_503_when_household_reports_are_not_configured() -> (
    None
):
    store = FakeRerunStore(report_definition=STORED_HOUSEHOLD_REPORT)

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun(rerun_service(store))

    assert unavailable.value.code == "not_configured"
    assert unavailable.value.detail == "Household reports are not configured."


def test_no_household_function_for_the_country_is_refused() -> None:
    households = FakeHouseholdReportStore()
    service, _, _ = _services(
        households=households,
        workers=FakeHouseholdWorkers(
            HouseholdFunctionUnavailableError("no household function")
        ),
    )

    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun(service)

    assert refused.value.code == "no_household_function"
    assert households.reads == []


@pytest.mark.parametrize(
    ("failure", "code", "named"),
    [
        (UnknownCatalogVersionError("none"), "unknown_catalog_version", "catalog"),
        (UnknownHouseholdError("gone"), "unknown_household", "the report names"),
        (
            ReportSideError("reform", UnknownPolicyError("gone")),
            "unknown_policy",
            "the report's reform names",
        ),
    ],
)
def test_a_stored_row_missing_from_the_catalog_is_refused_as_the_reports(
    failure, code, named
) -> None:
    households = FakeHouseholdReportStore(read_failure=failure)
    service, _, _ = _services(households=households)

    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun(service)

    assert refused.value.code == code
    assert named in refused.value.detail
    assert households.report_reruns == []


def test_a_policy_setting_parameters_the_catalog_lacks_is_located_at_the_report() -> (
    None
):
    households = FakeHouseholdReportStore(
        read_failure=ReportSideError(
            "reform", MissingPolicyParametersError([CTC_AMOUNT])
        )
    )
    service, _, _ = _services(households=households)

    with pytest.raises(EconomySubmissionUnprocessableError) as unprocessable:
        _rerun(service)

    assert unprocessable.value.code == "missing_parameters"
    assert [reason.loc for reason in unprocessable.value.reasons] == [
        ("report", "reform", "policy_id", CTC_AMOUNT)
    ]


def test_a_stored_household_the_contract_cannot_read_is_located_at_the_report() -> None:
    households = FakeHouseholdReportStore(document={"people": "not a list of people"})
    service, _, _ = _services(households=households)

    with pytest.raises(EconomySubmissionUnprocessableError) as unprocessable:
        _rerun(service)

    assert unprocessable.value.code == "household_not_calculable"
    assert [reason.loc for reason in unprocessable.value.reasons] == [
        ("report", "household_id")
    ]
    assert households.report_reruns == []


def test_a_household_rerun_whose_inputs_cannot_be_read_answers_503() -> None:
    service, _, _ = _services(
        households=FakeHouseholdReportStore(read_failure=DATABASE_ERROR)
    )

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun(service)

    assert unavailable.value.code == "database_unavailable"


def test_a_household_report_with_a_run_under_way_is_not_rerun() -> None:
    households = FakeHouseholdReportStore(
        rerun_failure=RunInProgressError(RUN_UNDER_WAY_ID)
    )
    caller = FakeHouseholdReportCaller()
    service, _, _ = _services(households=households, caller=caller)

    with pytest.raises(EconomyRerunConflictError) as refused:
        _rerun(service)

    assert (refused.value.code, refused.value.run_id) == (
        "run_in_progress",
        RUN_UNDER_WAY_ID,
    )
    assert caller.calls == []


def test_a_household_report_or_simulation_deleted_after_its_read_is_answered() -> None:
    deleted_report, _, _ = _services(
        households=FakeHouseholdReportStore(rerun_failure=RerunRecordDeletedError())
    )
    deleted_simulation, _, _ = _services(
        households=FakeHouseholdReportStore(
            rerun_failure=EconomyRecordReferenceError(
                "simulation_runs", "simulation_id"
            )
        )
    )

    with pytest.raises(EconomyRecordNotFoundError) as not_found:
        _rerun(deleted_report)
    with pytest.raises(EconomySubmissionRefusedError) as refused:
        _rerun(deleted_simulation)

    assert not_found.value.record == "report"
    assert refused.value.code == "missing_reference"
    assert refused.value.detail == "a simulation the report names no longer exists"


def test_a_household_rerun_whose_commit_is_unconfirmed_may_have_been_written() -> None:
    commit_failed = EconomyRecordOutcomeUnknownError("the commit failed")
    commit_failed.__cause__ = DATABASE_ERROR
    service, _, _ = _services(
        households=FakeHouseholdReportStore(rerun_failure=commit_failed)
    )

    with pytest.raises(EconomySubmissionOutcomeUnknownError) as unknown:
        _rerun(service)

    assert (unknown.value.record, unknown.value.rerun) == ("report", True)
    assert unknown.value.error_type == "OperationalError"


def test_a_household_rerun_modal_refuses_is_failed_and_named() -> None:
    households = FakeHouseholdReportStore()
    service, _, _ = _services(
        households=households,
        caller=FakeHouseholdReportCaller(failure=RuntimeError("Modal refused")),
    )

    with pytest.raises(EconomySubmissionUnavailableError) as unavailable:
        _rerun(service)

    assert unavailable.value.code == "modal_call_refused"
    assert (unavailable.value.record_id, unavailable.value.run_id) == (
        REPORT_ID,
        RUN_ID,
    )
    ((failed_run, failed_simulation_runs, message),) = households.failed_runs
    assert (failed_run, failed_simulation_runs) == (
        RUN_ID,
        (BASELINE_RUN_ID, REFORM_RUN_ID),
    )
    assert message.startswith("modal_call_refused:")
    assert households.recorded_calls == []
