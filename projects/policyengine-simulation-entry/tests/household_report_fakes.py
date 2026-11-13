"""Fakes of the household report service's store, manifest and Modal call.

The store's own reads and writes are tested against PostgreSQL in the
persistence library; here a fake records what the service asks of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import UUID

from household_fixtures import household_worker_version
from policyengine_simulation_contract.stage12_manifest import V2VersionManifest
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportRequest,
)
from policyengine_simulation_entry.household_report_reruns import (
    HouseholdReportRerunService,
)
from policyengine_simulation_entry.household_report_submissions import (
    HouseholdReportSubmissionService,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdWorker,
    default_household_worker,
)
from policyengine_simulation_persistence import (
    HouseholdReportInputs,
    ReportRunStatus,
    StoredHousehold,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from policyengine_simulation_persistence import (
        NewHouseholdReport,
        NewHouseholdReportRerun,
    )

HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-0000000000bb")
POLICY_ID = UUID("00000000-0000-4000-8000-0000000000cc")
CATALOG_VERSION_ID = UUID("00000000-0000-4000-8000-0000000000dd")
MODEL_ID = UUID("00000000-0000-4000-8000-0000000000de")
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REFORM = {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
STORED_DOCUMENT: dict[str, Any] = {
    "people": [
        {
            "id": "you",
            "values": {"age": 40, "employment_income": {"2026": 30_000}},
            "memberships": {
                "household": "your household",
                "family": "your family",
                "tax_unit": "your tax unit",
                "spm_unit": "your spm unit",
                "marital_unit": "your marital unit",
            },
        }
    ],
    "household": [{"id": "your household", "values": {"household_net_income": None}}],
    "family": [{"id": "your family", "values": {}}],
    "tax_unit": [{"id": "your tax unit", "values": {}}],
    "spm_unit": [{"id": "your spm unit", "values": {}}],
    "marital_unit": [{"id": "your marital unit", "values": {}}],
}
# The stored household above, in the situation shape a household function takes.
SITUATION = {
    "people": {"you": {"age": 40, "employment_income": {"2026": 30_000}}},
    "households": {
        "your household": {"members": ["you"], "household_net_income": None}
    },
    "families": {"your family": {"members": ["you"]}},
    "tax_units": {"your tax unit": {"members": ["you"]}},
    "spm_units": {"your spm unit": {"members": ["you"]}},
    "marital_units": {"your marital unit": {"members": ["you"]}},
}
HOUSEHOLD_REPORT_BODY: dict[str, Any] = {
    "country": "us",
    "report_type": "household",
    "label": "A larger child tax credit for one family",
    "description": "Raises the credit to $3,000",
    "household_id": str(HOUSEHOLD_ID),
    "baseline": {"policy_id": None},
    "reform": {"policy_id": str(POLICY_ID)},
}


def household_report_request(**changes: Any) -> HouseholdReportRequest:
    return HouseholdReportRequest.model_validate({**HOUSEHOLD_REPORT_BODY, **changes})


def manifest_household_worker(country: str) -> HouseholdWorker:
    """Return the country's household function in a manifest of one worker."""
    version = household_worker_version()
    manifest = V2VersionManifest(
        generation="test",
        default_version=version.bundle.policyengine_version,
        versions={version.bundle.policyengine_version: version},
    )
    return default_household_worker(manifest, country)


class FakeHouseholdWorkers:
    """The default worker's household functions, or the lookup's failure."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure

    def default_worker(self, country: str) -> HouseholdWorker:
        if self.failure is not None:
            raise self.failure
        return manifest_household_worker(country)


@dataclass
class FakeHouseholdReportStore:
    """Records what the service asks of the database, and answers as told.

    ``claimed`` marks the report run as claimed by its household function: a
    failure write then changes nothing. ``rerun_failure`` is raised by a
    re-run's create instead of recording it.
    """

    default_year: int | None = 2026
    document: dict[str, Any] = field(default_factory=lambda: STORED_DOCUMENT)
    read_failure: Exception | None = None
    create_failure: Exception | None = None
    record_failure: Exception | None = None
    fail_failure: Exception | None = None
    rerun_failure: Exception | None = None
    claimed: bool = False
    reads: list[dict[str, Any]] = field(default_factory=list)
    reports: list[NewHouseholdReport] = field(default_factory=list)
    report_reruns: list[NewHouseholdReportRerun] = field(default_factory=list)
    recorded_calls: list[tuple[UUID, str]] = field(default_factory=list)
    failed_runs: list[tuple[UUID, tuple[UUID, ...], str]] = field(default_factory=list)

    def read_inputs(
        self,
        *,
        country: str,
        household_id: UUID,
        policy_ids: Mapping[str, UUID | None],
        policyengine_version: str,
    ) -> HouseholdReportInputs:
        self.reads.append(
            {
                "country": country,
                "household_id": household_id,
                "policy_ids": dict(policy_ids),
                "policyengine_version": policyengine_version,
            }
        )
        if self.read_failure is not None:
            raise self.read_failure
        return HouseholdReportInputs(
            catalog_version_id=CATALOG_VERSION_ID,
            tax_benefit_model_id=MODEL_ID,
            household=StoredHousehold(
                id=household_id,
                country=country,
                default_year=self.default_year,
                document=self.document,
            ),
            reforms={
                side: ({} if policy_id is None else REFORM)
                for side, policy_id in policy_ids.items()
            },
        )

    def create_report(self, report: NewHouseholdReport) -> None:
        if self.create_failure is not None:
            raise self.create_failure
        self.reports.append(report)

    def create_report_rerun(self, rerun: NewHouseholdReportRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.report_reruns.append(rerun)

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        if self.record_failure is not None:
            raise self.record_failure
        self.recorded_calls.append((report_run_id, modal_call_id))
        return ReportRunStatus.RUNNING if self.claimed else ReportRunStatus.PENDING

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        if self.fail_failure is not None:
            raise self.fail_failure
        if self.claimed:
            return False
        self.failed_runs.append(
            (report_run_id, tuple(simulation_run_ids), error_message)
        )
        return True


@dataclass
class FakeHouseholdReportCaller:
    """Starts calls by recording them; ``failure`` is raised instead when set.

    ``hang`` makes a start wait until the service stops waiting for it.
    """

    failure: Exception | None = None
    hang: bool = False
    calls: list[tuple[HouseholdWorker, dict, UUID, Any]] = field(default_factory=list)

    async def start_report_run(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        report_run_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        if self.hang:
            await asyncio.sleep(60)
        if self.failure is not None:
            raise self.failure
        self.calls.append((worker, payload, report_run_id, observability_context))
        return "fc-household-report"


def numbered_ids():
    """Return a function giving 00…01, 00…02 and so on, in order."""
    numbers = count(1)
    return lambda: UUID(int=next(numbers))


def household_report_service(
    store: FakeHouseholdReportStore,
    *,
    workers: FakeHouseholdWorkers | None = None,
    caller: FakeHouseholdReportCaller | None = None,
) -> HouseholdReportSubmissionService:
    return HouseholdReportSubmissionService(
        store=store,
        workers=workers or FakeHouseholdWorkers(),
        caller=caller or FakeHouseholdReportCaller(),
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
    )


def household_rerun_service(
    store: FakeHouseholdReportStore,
    *,
    workers: FakeHouseholdWorkers | None = None,
    caller: FakeHouseholdReportCaller | None = None,
) -> HouseholdReportRerunService:
    """Return the service that creates and re-runs household reports, on fakes."""
    return HouseholdReportRerunService(
        store=store,
        workers=workers or FakeHouseholdWorkers(),
        caller=caller or FakeHouseholdReportCaller(),
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
    )
