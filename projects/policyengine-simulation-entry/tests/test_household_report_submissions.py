"""The household report service: what it reads, creates and starts, and refuses.

The store, the manifest and the Modal call are faked
(``household_report_fakes``); the store's own SQL is tested in the
persistence library.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from household_report_fakes import (
    CATALOG_VERSION_ID,
    HOUSEHOLD_ID,
    MODEL_ID,
    POLICY_ID,
    REFORM,
    SITUATION,
    STORED_DOCUMENT,
    FakeHouseholdReportCaller,
    FakeHouseholdReportStore,
    FakeHouseholdWorkers,
    household_report_request,
    household_report_service,
    manifest_household_worker,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_entry.household_report_submissions import (
    HouseholdReportBuildError,
    HouseholdReportSideInputs,
    household_report_calculation,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    VersionManifestUnavailableError,
)
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    MissingPolicyParametersError,
    ReportRunStatus,
    ReportSideError,
    RunVersions,
    StoredHousehold,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from sqlalchemy.exc import OperationalError

# The service's IDs, in the order it takes them: the report, its run, then
# each side's simulation and simulation run.
REPORT_ID, RUN_ID, BASELINE_ID, BASELINE_RUN_ID, REFORM_ID, REFORM_RUN_ID = (
    UUID(int=number) for number in range(1, 7)
)
CONTEXT = {"observability_id": "6e1f3d4c-0a5b-4c39-9d2e-7f8a9b0c1d2e"}
DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))


def _submit(service, request=None):
    return asyncio.run(
        service.submit_report(
            request or household_report_request(), observability_context=CONTEXT
        )
    )


def _side_request(calculation_id: UUID, parameters: dict) -> dict:
    worker = manifest_household_worker("us")
    return {
        "schema_version": 1,
        "calculation_id": str(calculation_id),
        "country": "us",
        "year": "2026",
        "household": SITUATION,
        "policy": {"parameters": parameters},
        "add_missing_variables": True,
        "spm_selection_source": "request",
        "bundle": worker.bundle.model_dump(mode="json"),
    }


def test_a_household_report_is_created_and_its_household_function_started():
    store = FakeHouseholdReportStore()
    caller = FakeHouseholdReportCaller()
    service = household_report_service(store, caller=caller)

    created = _submit(service)

    assert created == HouseholdReportCreated(
        report_id=REPORT_ID,
        report_run_id=RUN_ID,
        status=ReportRunStatus.PENDING,
        year=2026,
        baseline_simulation_id=BASELINE_ID,
        baseline_simulation_run_id=BASELINE_RUN_ID,
        reform_simulation_id=REFORM_ID,
        reform_simulation_run_id=REFORM_RUN_ID,
    )
    worker = manifest_household_worker("us")
    assert store.reads == [
        {
            "country": "us",
            "household_id": HOUSEHOLD_ID,
            "policy_ids": {"baseline": None, "reform": POLICY_ID},
            "policyengine_version": worker.bundle.policyengine_version,
        }
    ]
    (report,) = store.reports
    assert (report.id, report.run_id, report.country) == (REPORT_ID, RUN_ID, "us")
    assert report.label == "A larger child tax credit for one family"
    assert report.description == "Raises the credit to $3,000"
    assert (report.tax_benefit_model_id, report.tax_benefit_model_version_id) == (
        MODEL_ID,
        CATALOG_VERSION_ID,
    )
    assert (report.household_id, report.year) == (HOUSEHOLD_ID, 2026)
    assert report.inputs == {"add_missing_variables": True}
    assert report.versions == RunVersions(
        country_package_version=worker.bundle.country_package_version,
        policyengine_version=worker.bundle.policyengine_version,
    )
    assert (report.baseline.simulation_id, report.baseline.policy_id) == (
        BASELINE_ID,
        None,
    )
    assert report.reform is not None
    assert (report.reform.simulation_run_id, report.reform.policy_id) == (
        REFORM_RUN_ID,
        POLICY_ID,
    )
    ((called_worker, payload, report_run_id, context),) = caller.calls
    assert called_worker == worker
    assert (report_run_id, context) == (RUN_ID, CONTEXT)
    assert payload == {
        "schema_version": 1,
        "baseline": _side_request(BASELINE_RUN_ID, {}),
        "reform": _side_request(REFORM_RUN_ID, REFORM),
    }
    HouseholdReportRunCalculation.model_validate(payload)
    assert store.recorded_calls == [(RUN_ID, "fc-household-report")]


def test_a_report_of_the_baseline_alone_creates_and_calculates_one_side():
    store = FakeHouseholdReportStore()
    caller = FakeHouseholdReportCaller()
    service = household_report_service(store, caller=caller)

    created = _submit(
        service,
        household_report_request(baseline={"policy_id": str(POLICY_ID)}, reform=None),
    )

    assert (created.reform_simulation_id, created.reform_simulation_run_id) == (
        None,
        None,
    )
    assert store.reads[0]["policy_ids"] == {"baseline": POLICY_ID}
    (report,) = store.reports
    assert report.reform is None
    assert report.simulation_run_ids == (BASELINE_RUN_ID,)
    ((_, payload, _, _),) = caller.calls
    assert payload["reform"] is None
    assert payload["baseline"]["policy"] == {"parameters": REFORM}


def test_a_saved_spm_selection_is_applied_to_both_sides():
    saved = {"scenario": "ce_trend", "geography_kind": "national"}
    store = FakeHouseholdReportStore(document={**STORED_DOCUMENT, "spm": saved})
    caller = FakeHouseholdReportCaller()

    _submit(household_report_service(store, caller=caller))

    ((_, payload, _, _),) = caller.calls
    for side in ("baseline", "reform"):
        assert payload[side]["spm"] == saved
        assert payload[side]["spm_selection_source"] == "saved_household"


@pytest.mark.parametrize(
    ("failure", "expected_type", "code"),
    [
        (
            HouseholdFunctionUnavailableError("no household function"),
            EconomySubmissionRefusedError,
            "no_household_function",
        ),
        (
            VersionManifestUnavailableError("unreadable"),
            EconomySubmissionUnavailableError,
            "manifest_unavailable",
        ),
    ],
)
def test_the_default_workers_household_function_is_needed(failure, expected_type, code):
    store = FakeHouseholdReportStore()
    service = household_report_service(
        store, workers=FakeHouseholdWorkers(failure=failure)
    )

    with pytest.raises(expected_type) as raised:
        _submit(service)

    assert raised.value.code == code
    assert (store.reads, store.reports) == ([], [])


@pytest.mark.parametrize(
    ("failure", "expected_type", "code", "detail"),
    [
        (
            UnknownCatalogVersionError("no catalog version"),
            EconomySubmissionRefusedError,
            "unknown_catalog_version",
            "us has no catalog version for .py 6.17.1, the default v2 worker's "
            "version.",
        ),
        (
            UnknownHouseholdError("us has no household"),
            EconomySubmissionRefusedError,
            "unknown_household",
            "us has no stored household the request names.",
        ),
        (
            ReportSideError("reform", UnknownPolicyError("no policy")),
            EconomySubmissionRefusedError,
            "unknown_policy",
            "us has no stored policy the request's reform names.",
        ),
        (
            DATABASE_ERROR,
            EconomySubmissionUnavailableError,
            "database_unavailable",
            "The simulation database could not be read.",
        ),
    ],
    ids=["catalog-version", "household", "policy", "database"],
)
def test_each_missing_input_is_refused_before_anything_is_written(
    failure, expected_type, code, detail
):
    store = FakeHouseholdReportStore(read_failure=failure)
    caller = FakeHouseholdReportCaller()

    with pytest.raises(expected_type) as raised:
        _submit(household_report_service(store, caller=caller))

    assert (raised.value.code, raised.value.detail) == (code, detail)
    assert (store.reports, caller.calls) == ([], [])


def test_a_policy_setting_missing_parameters_is_refused_on_its_side():
    store = FakeHouseholdReportStore(
        read_failure=ReportSideError(
            "baseline", MissingPolicyParametersError(["gov.a", "gov.b"])
        )
    )

    with pytest.raises(EconomySubmissionUnprocessableError) as raised:
        _submit(household_report_service(store))

    assert raised.value.code == "missing_parameters"
    assert [reason.loc for reason in raised.value.reasons] == [
        ("body", "baseline", "policy_id", "gov.a"),
        ("body", "baseline", "policy_id", "gov.b"),
    ]
    assert {reason.type for reason in raised.value.reasons} == {"missing_parameter"}
    assert store.reports == []


@pytest.mark.parametrize(
    ("store", "reason_type"),
    [
        (FakeHouseholdReportStore(default_year=None), "household_without_default_year"),
        (
            FakeHouseholdReportStore(document={"people": "not a list of people"}),
            "household_unreadable",
        ),
    ],
    ids=["no-default-year", "unreadable"],
)
def test_a_household_that_cannot_be_calculated_is_refused(store, reason_type):
    caller = FakeHouseholdReportCaller()

    with pytest.raises(EconomySubmissionUnprocessableError) as raised:
        _submit(household_report_service(store, caller=caller))

    assert raised.value.code == "household_not_calculable"
    ((reason),) = raised.value.reasons
    assert (reason.loc, reason.type) == (("body", "household_id"), reason_type)
    # The detail is the service's own words, never the document's contents.
    assert "not a list of people" not in reason.msg
    assert (store.reports, caller.calls) == ([], [])


def test_a_household_the_contract_refuses_is_located_on_the_household():
    # A group no person belongs to has no members, which the contract refuses.
    document = {
        **STORED_DOCUMENT,
        "family": [*STORED_DOCUMENT["family"], {"id": "an empty family", "values": {}}],
    }
    store = FakeHouseholdReportStore(document=document)

    with pytest.raises(EconomySubmissionUnprocessableError) as raised:
        _submit(household_report_service(store))

    assert raised.value.code == "household_not_calculable"
    assert raised.value.reasons
    assert all(
        reason.loc[:3] == ("body", "household_id", "household")
        for reason in raised.value.reasons
    )
    assert store.reports == []


@pytest.mark.parametrize(
    ("column", "code"),
    [
        ("household_id", "unknown_household"),
        ("policy_id", "unknown_policy"),
        ("tax_benefit_model_version_id", "unknown_catalog_version"),
        (None, "missing_reference"),
    ],
)
def test_a_row_deleted_after_the_reads_is_refused_on_create(column, code):
    store = FakeHouseholdReportStore(
        create_failure=EconomyRecordReferenceError("simulations", column)
    )
    caller = FakeHouseholdReportCaller()

    with pytest.raises(EconomySubmissionRefusedError) as raised:
        _submit(household_report_service(store, caller=caller))

    assert raised.value.code == code
    assert caller.calls == []


def test_a_create_whose_commit_failed_may_exist():
    store = FakeHouseholdReportStore(
        create_failure=EconomyRecordOutcomeUnknownError("commit failed")
    )
    caller = FakeHouseholdReportCaller()

    with pytest.raises(EconomySubmissionOutcomeUnknownError) as raised:
        _submit(household_report_service(store, caller=caller))

    assert raised.value.record == "report"
    assert caller.calls == []


def test_a_create_the_database_cannot_take_is_unavailable():
    store = FakeHouseholdReportStore(create_failure=DATABASE_ERROR)

    with pytest.raises(EconomySubmissionUnavailableError) as raised:
        _submit(household_report_service(store))

    assert raised.value.code == "database_unavailable"
    assert raised.value.record_id is None


@pytest.mark.parametrize(
    ("caller", "error_type"),
    [
        (FakeHouseholdReportCaller(failure=RuntimeError("refused")), "RuntimeError"),
        (FakeHouseholdReportCaller(hang=True), "TimeoutError"),
    ],
    ids=["refused", "not-confirmed-in-time"],
)
def test_a_call_modal_does_not_confirm_fails_the_report_run_and_its_runs(
    caller, error_type
):
    store = FakeHouseholdReportStore()

    with pytest.raises(EconomySubmissionUnavailableError) as raised:
        _submit(household_report_service(store, caller=caller))

    assert raised.value.code == "modal_call_refused"
    assert raised.value.record_id == REPORT_ID
    assert store.failed_runs == [
        (
            RUN_ID,
            (BASELINE_RUN_ID, REFORM_RUN_ID),
            "modal_call_refused: Modal did not accept the report's call "
            f"({error_type}).",
        )
    ]
    assert store.recorded_calls == []


def test_a_call_modal_accepted_late_is_answered_pending():
    # The household function claimed the run, so the failure write changes nothing.
    store = FakeHouseholdReportStore(claimed=True)
    caller = FakeHouseholdReportCaller(failure=RuntimeError("late"))

    created = _submit(household_report_service(store, caller=caller))

    assert created.status is ReportRunStatus.PENDING
    assert store.failed_runs == []


def test_a_failure_write_that_fails_answers_pending():
    store = FakeHouseholdReportStore(fail_failure=DATABASE_ERROR)
    caller = FakeHouseholdReportCaller(failure=RuntimeError("refused"))

    created = _submit(household_report_service(store, caller=caller))

    assert created.status is ReportRunStatus.PENDING


@pytest.mark.parametrize(
    ("store", "status"),
    [
        (FakeHouseholdReportStore(record_failure=DATABASE_ERROR), "pending"),
        (FakeHouseholdReportStore(claimed=True), "running"),
    ],
    ids=["record-fails", "claimed-first"],
)
def test_the_answer_is_the_report_runs_status_once_its_call_is_recorded(store, status):
    created = _submit(household_report_service(store))

    assert created.status.value == status


def _stored_household(document: dict | None = None) -> StoredHousehold:
    return StoredHousehold(
        id=HOUSEHOLD_ID,
        country="us",
        default_year=2026,
        document=STORED_DOCUMENT if document is None else document,
    )


def test_a_report_runs_calculation_is_built_from_its_stored_inputs_alone():
    # A restart rebuilds the call from the stored report: its own year, not
    # the household's default year now, and its stored option.
    calculation = household_report_calculation(
        household=_stored_household(),
        year=2027,
        add_missing_variables=False,
        baseline=HouseholdReportSideInputs(
            simulation_run_id=BASELINE_RUN_ID, policy_values={}
        ),
        reform=HouseholdReportSideInputs(
            simulation_run_id=REFORM_RUN_ID, policy_values=REFORM
        ),
        worker=manifest_household_worker("us"),
    )

    payload = calculation.model_dump(mode="json")
    for side, run_id, parameters in (
        ("baseline", BASELINE_RUN_ID, {}),
        ("reform", REFORM_RUN_ID, REFORM),
    ):
        expected = {
            **_side_request(run_id, parameters),
            "year": "2027",
            "add_missing_variables": False,
        }
        assert payload[side] == expected


def test_a_calculation_of_the_baseline_alone_has_no_reform():
    calculation = household_report_calculation(
        household=_stored_household(),
        year=2026,
        add_missing_variables=True,
        baseline=HouseholdReportSideInputs(
            simulation_run_id=BASELINE_RUN_ID, policy_values=REFORM
        ),
        worker=manifest_household_worker("us"),
    )

    assert calculation.reform is None
    assert calculation.baseline.calculation_id == BASELINE_RUN_ID
    assert calculation.baseline.policy.parameters == REFORM


def test_a_stored_household_the_builder_cannot_read_is_not_calculable():
    with pytest.raises(EconomySubmissionUnprocessableError) as raised:
        household_report_calculation(
            household=_stored_household({"people": "not a list of people"}),
            year=2026,
            add_missing_variables=False,
            baseline=HouseholdReportSideInputs(
                simulation_run_id=BASELINE_RUN_ID, policy_values={}
            ),
            worker=manifest_household_worker("us"),
        )

    assert raised.value.code == "household_not_calculable"
    assert [reason.type for reason in raised.value.reasons] == ["household_unreadable"]


def test_two_sides_recording_one_simulation_run_are_built_wrong():
    side = HouseholdReportSideInputs(
        simulation_run_id=BASELINE_RUN_ID, policy_values={}
    )

    with pytest.raises(HouseholdReportBuildError, match="different simulation runs"):
        household_report_calculation(
            household=_stored_household(),
            year=2026,
            add_missing_variables=False,
            baseline=side,
            reform=side,
            worker=manifest_household_worker("us"),
        )
