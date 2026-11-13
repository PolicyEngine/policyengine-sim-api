"""Starting a household report run again when its household function call never started.

One household function call calculates both sides of a household report run.
The restart reads back the report run's stored rows, reads the household and
each side's policy as they are now, against the catalog version the run was
created on, and rebuilds the call with the submission's own builder
(``household_report_calculation``). Each test first submits a household
report through the service that creates it, captures the call that
submission started, and requires the restart to start the same call. The
store's reads are tested against PostgreSQL in the persistence library.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from economy_service_fakes import CREATED_AT
from household_report_fakes import (
    POLICY_ID,
    FakeHouseholdReportCaller,
    FakeHouseholdReportStore,
    household_report_request,
    numbered_ids,
)
from policyengine_simulation_entry.household_report_submissions import (
    HouseholdReportSubmissionService,
)
from policyengine_simulation_entry.household_workers import ManifestHouseholdWorkers
from policyengine_simulation_entry.run_restarts import RestartOutcome, RestartSummary
from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    ReportSideError,
    RestartKind,
    RunVersions,
    StoredHouseholdReportRun,
    StoredHouseholdReportSide,
    StoredRunUnreadableError,
    UnknownHouseholdError,
    UnknownPolicyError,
    UnstartedRun,
)
from test_run_restarts import (
    DATABASE_ERROR,
    MANIFEST_STORE,
    FakeHouseholdCaller,
    FakeRestartStore,
    RestartRig,
    restart_rig,
)

NOT_STARTED = (
    "run_not_started: The run stayed pending with no Modal call recorded for it"
)


def _submitted_household_report(
    **request_fields: object,
) -> tuple[StoredHouseholdReportRun, dict, UUID]:
    """Submit a household report; return its stored run, its call's payload and run.

    The report run is stored as the submission created it.
    """
    store = FakeHouseholdReportStore()
    caller = FakeHouseholdReportCaller()
    submissions = HouseholdReportSubmissionService(
        store=store,
        workers=ManifestHouseholdWorkers(MANIFEST_STORE),
        caller=caller,
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
    )
    asyncio.run(
        submissions.submit_report(
            household_report_request(**request_fields), observability_context=None
        )
    )
    (report,) = store.reports
    (_worker, payload, report_run_id, _observability) = caller.calls[0]
    stored = StoredHouseholdReportRun(
        report_run_id=report.run_id,
        country=report.country,
        versions=report.versions,
        household_id=report.household_id,
        year=report.year,
        catalog_version_id=report.tax_benefit_model_version_id,
        baseline=StoredHouseholdReportSide(
            simulation_run_id=report.baseline.simulation_run_id,
            policy_id=report.baseline.policy_id,
        ),
        reform=(
            None
            if report.reform is None
            else StoredHouseholdReportSide(
                simulation_run_id=report.reform.simulation_run_id,
                policy_id=report.reform.policy_id,
            )
        ),
        created_at=CREATED_AT,
    )
    return stored, payload, report_run_id


def _household_report_rig(
    stored: StoredHouseholdReportRun, **options: Any
) -> RestartRig:
    return restart_rig(
        restarts=FakeRestartStore(
            unstarted=(
                UnstartedRun(
                    kind=RestartKind.HOUSEHOLD_REPORT_RUN,
                    run_id=stored.report_run_id,
                    created_at=CREATED_AT,
                ),
            ),
            household_report_runs={stored.report_run_id: stored},
        ),
        **options,
    )


def _sides(stored: StoredHouseholdReportRun) -> tuple[UUID, ...]:
    """Return the report run's simulation runs, the baseline's first."""
    sides = (
        [stored.baseline] if stored.reform is None else [stored.baseline, stored.reform]
    )
    return tuple(side.simulation_run_id for side in sides)


def test_a_household_report_run_is_started_again_with_its_submissions_call() -> None:
    stored, payload, report_run_id = _submitted_household_report()
    rig = _household_report_rig(stored)

    summary = rig.restart()

    (restarted,) = rig.household_caller.report_runs
    assert restarted["payload"] == payload
    assert restarted["report_run_id"] == report_run_id
    assert restarted["observability_context"] == {"traceparent": "00-restart-check"}
    assert [run.run_id for run in rig.restarts.marked] == [report_run_id]
    assert rig.economy.recorded_calls == [
        (report_run_id, "fc-household-report-restarted")
    ]
    assert summary == RestartSummary(listed=1, restarted=1)
    assert rig.telemetry.outcomes == [
        (
            RestartKind.HOUSEHOLD_REPORT_RUN,
            report_run_id,
            RestartOutcome.RESTARTED,
            None,
        )
    ]
    # The household and each side's policy are read on the run's own .py
    # version, the report's year kept as stored.
    (read,) = rig.household_reports.reads
    assert read["policyengine_version"] == stored.versions.policyengine_version
    assert read["policy_ids"] == {"baseline": None, "reform": POLICY_ID}


def test_a_household_report_of_the_baseline_alone_is_started_again() -> None:
    stored, payload, _report_run_id = _submitted_household_report(reform=None)
    rig = _household_report_rig(stored)

    summary = rig.restart()

    assert stored.reform is None
    (restarted,) = rig.household_caller.report_runs
    assert restarted["payload"] == payload
    assert set(rig.household_reports.reads[0]["policy_ids"]) == {"baseline"}
    assert summary == RestartSummary(listed=1, restarted=1)


def _failed_message(rig: RestartRig, stored: StoredHouseholdReportRun) -> str:
    """Return the message the report run was failed with, with its own runs."""
    ((report_run_id, simulation_run_ids, message),) = rig.economy.failed_runs
    assert report_run_id == stored.report_run_id
    assert simulation_run_ids == _sides(stored)
    return message


def test_a_household_report_run_whose_catalog_version_changed_is_failed() -> None:
    """The .py version's catalog version is no longer the one the run records."""
    stored, _payload, _report_run_id = _submitted_household_report()
    stored = replace(stored, catalog_version_id=UUID(int=404))
    rig = _household_report_rig(stored)

    summary = rig.restart()

    assert rig.household_caller.report_runs == []
    assert rig.restarts.marked == []
    assert _failed_message(rig, stored) == (
        f"{NOT_STARTED}, and its call could not be built again: its catalog "
        "version changed since it was created."
    )
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_household_report_run_whose_versions_no_worker_holds_is_failed() -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    stored = replace(
        stored,
        versions=RunVersions(
            country_package_version="0.0.1",
            policyengine_version=stored.versions.policyengine_version,
        ),
    )
    rig = _household_report_rig(stored)

    summary = rig.restart()

    assert rig.household_caller.report_runs == []
    assert "calculates on other versions than the run records" in _failed_message(
        rig, stored
    )
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_household_report_run_whose_household_is_gone_is_failed() -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored,
        household_reports=FakeHouseholdReportStore(
            read_failure=UnknownHouseholdError("us has no household")
        ),
    )

    summary = rig.restart()

    assert _failed_message(rig, stored).endswith(": its household is gone.")
    assert summary == RestartSummary(listed=1, failed=1)
    assert rig.telemetry.outcomes[0][3] == "UnknownHouseholdError"


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (UnknownPolicyError("no policy"), "its policy is gone"),
        (
            MissingPolicyParametersError("no such parameter"),
            "its policy names parameters its catalog lacks",
        ),
    ],
    ids=["policy-gone", "parameters-missing"],
)
def test_a_household_report_run_whose_reforms_policy_is_refused_is_failed(
    refusal, reason
) -> None:
    """A side's refusal is read, and counted, as its own cause."""
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored,
        household_reports=FakeHouseholdReportStore(
            read_failure=ReportSideError("reform", refusal)
        ),
    )

    summary = rig.restart()

    assert _failed_message(rig, stored).endswith(f": {reason}.")
    assert summary == RestartSummary(listed=1, failed=1)
    assert rig.telemetry.outcomes[0][3] == type(refusal).__name__


def test_a_household_report_run_whose_sides_name_one_run_is_failed() -> None:
    """The contract refuses a payload the service built wrong."""
    stored, _payload, _report_run_id = _submitted_household_report()
    assert stored.reform is not None
    stored = replace(
        stored,
        reform=replace(
            stored.reform, simulation_run_id=stored.baseline.simulation_run_id
        ),
    )
    rig = _household_report_rig(stored)

    summary = rig.restart()

    assert rig.household_caller.report_runs == []
    ((_run_id, _sides_failed, message),) = rig.economy.failed_runs
    assert message.endswith(": its household report request no longer validates.")
    assert summary == RestartSummary(listed=1, failed=1)
    assert rig.telemetry.outcomes[0][3] == "HouseholdReportBuildError"


def test_a_household_report_run_whose_worker_left_the_manifest_is_failed() -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    stored = replace(
        stored,
        versions=RunVersions(
            country_package_version=stored.versions.country_package_version,
            policyengine_version="0.0.1",
        ),
    )
    rig = _household_report_rig(stored)

    summary = rig.restart()

    assert _failed_message(rig, stored).endswith(
        ": the v2 version manifest has no worker for .py 0.0.1."
    )
    assert summary == RestartSummary(listed=1, failed=1)


def test_a_household_report_run_whose_rows_no_longer_describe_it_is_failed() -> None:
    """It is failed with the simulation runs it names, read on their own."""
    stored, _payload, _report_run_id = _submitted_household_report(reform=None)
    rig = _household_report_rig(stored)
    rig.restarts.read_failures[stored.report_run_id] = StoredRunUnreadableError(
        "report run names a missing simulation run"
    )
    rig.restarts.report_simulation_run_ids[stored.report_run_id] = (
        stored.baseline.simulation_run_id,
    )

    summary = rig.restart()

    assert _failed_message(rig, stored).endswith(
        ": its stored rows no longer describe one calculation."
    )
    assert summary == RestartSummary(listed=1, failed=1)


def test_an_unexpected_rebuild_error_defers_a_household_report_run(caplog) -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored,
        household_reports=FakeHouseholdReportStore(
            read_failure=RuntimeError("unforeseen")
        ),
    )

    summary = rig.restart()

    assert rig.restarts.marked == []
    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)
    assert [
        record.policyengine_attributes
        for record in caplog.records
        if record.getMessage() == "v2_run_restart_unexpected_error"
    ] == [{"run_kind": "household_report_run", "run_id": str(stored.report_run_id)}]


def test_a_household_report_run_whose_household_cannot_be_calculated_is_failed() -> (
    None
):
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored,
        household_reports=FakeHouseholdReportStore(document={"people": "unreadable"}),
    )

    summary = rig.restart()

    assert _failed_message(rig, stored).endswith(
        ": its household can no longer be calculated."
    )
    assert summary == RestartSummary(listed=1, failed=1)


def test_unreadable_rows_leave_a_household_report_run_for_the_next_check() -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored, household_reports=FakeHouseholdReportStore(read_failure=DATABASE_ERROR)
    )

    summary = rig.restart()

    assert rig.restarts.marked == []
    assert rig.economy.failed_runs == []
    assert summary == RestartSummary(listed=1, deferred=1)


def test_a_household_report_run_whose_restart_modal_refuses_is_failed() -> None:
    stored, _payload, _report_run_id = _submitted_household_report()
    rig = _household_report_rig(
        stored, household_caller=FakeHouseholdCaller(failure=ConnectionError("down"))
    )

    summary = rig.restart()

    assert [run.run_id for run in rig.restarts.marked] == [stored.report_run_id]
    assert _failed_message(rig, stored) == (
        f"{NOT_STARTED}, and Modal did not accept the call that started it again "
        "(ConnectionError)."
    )
    assert summary == RestartSummary(listed=1, failed=1)
