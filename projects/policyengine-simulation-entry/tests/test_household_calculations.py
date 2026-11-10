"""Submitting and reading household calculations, with the database and Modal faked.

The store's own reads and writes are tested against PostgreSQL in the
persistence library; here a fake store records what the service asks of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from household_fixtures import household_worker_version
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_contract.stage12_manifest import V2VersionManifest
from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationRequest,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationRefusedError,
    HouseholdCalculationService,
    HouseholdCalculationUnavailableError,
    HouseholdCalculationUnprocessableError,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    HouseholdWorker,
    VersionManifestUnavailableError,
    default_household_worker,
)
from policyengine_simulation_persistence import (
    ConflictingPolicyValuesError,
    HouseholdCalculationInputs,
    HouseholdJobRecord,
    HouseholdJobReferenceError,
    HouseholdJobStatus,
    MissingPolicyParametersError,
    NewHouseholdJob,
    StoredHousehold,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from sqlalchemy.exc import OperationalError

JOB_ID = UUID("00000000-0000-4000-8000-0000000000aa")
HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-0000000000bb")
POLICY_ID = UUID("00000000-0000-4000-8000-0000000000cc")
CATALOG_VERSION_ID = UUID("00000000-0000-4000-8000-0000000000dd")
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REFORM = {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
STORED_DOCUMENT = {
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
SAVED_SPM = {"scenario": "ce_trend", "geography_kind": "national"}
DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("server closed"))


def _manifest_worker(country: str) -> HouseholdWorker:
    """Return the country's household function in a manifest of one worker."""
    version = household_worker_version()
    manifest = V2VersionManifest(
        generation="test",
        default_version=version.bundle.policyengine_version,
        versions={version.bundle.policyengine_version: version},
    )
    return default_household_worker(manifest, country)


class FakeWorkers:
    """The default worker's household functions, or the lookup's failure."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.countries: list[str] = []

    def default_worker(self, country: str) -> HouseholdWorker:
        self.countries.append(country)
        if self.failure is not None:
            raise self.failure
        return _manifest_worker(country)


@dataclass
class FakeStore:
    """Records what the service asks of the database, and answers as told."""

    default_year: int | None = 2026
    document: dict[str, Any] = field(default_factory=lambda: dict(STORED_DOCUMENT))
    reform: dict[str, dict[str, object]] = field(default_factory=dict)
    read_failure: Exception | None = None
    create_failure: Exception | None = None
    record_failure: Exception | None = None
    recorded_status: HouseholdJobStatus = HouseholdJobStatus.PENDING
    stored_job: HouseholdJobRecord | None = None
    reads: list[dict[str, Any]] = field(default_factory=list)
    created: list[NewHouseholdJob] = field(default_factory=list)
    recorded: list[tuple[UUID, str]] = field(default_factory=list)
    failed: list[tuple[UUID, str]] = field(default_factory=list)

    def read_inputs(self, **arguments: Any) -> HouseholdCalculationInputs:
        self.reads.append(arguments)
        if self.read_failure is not None:
            raise self.read_failure
        return HouseholdCalculationInputs(
            catalog_version_id=CATALOG_VERSION_ID,
            household=StoredHousehold(
                id=arguments["household_id"],
                country=arguments["country"],
                default_year=self.default_year,
                document=self.document,
            ),
            reform=self.reform,
        )

    def create_job(self, job: NewHouseholdJob) -> None:
        if self.create_failure is not None:
            raise self.create_failure
        self.created.append(job)

    def record_call(self, job_id: UUID, modal_call_id: str) -> HouseholdJobStatus:
        if self.record_failure is not None:
            raise self.record_failure
        self.recorded.append((job_id, modal_call_id))
        return self.recorded_status

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        self.failed.append((job_id, error_message))
        return True

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        if self.read_failure is not None:
            raise self.read_failure
        return (
            self.stored_job
            if self.stored_job and self.stored_job.id == job_id
            else None
        )


@dataclass
class FakeCaller:
    """Records each household function call, and answers as Modal would."""

    failure: Exception | None = None
    delay_seconds: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def spawn(
        self,
        worker: HouseholdWorker,
        payload: dict[str, Any],
        *,
        household_job_id: UUID,
        observability_context: dict[str, Any] | None,
    ) -> str:
        self.calls.append(
            {
                "worker": worker,
                "payload": payload,
                "household_job_id": household_job_id,
                "observability_context": observability_context,
            }
        )
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.failure is not None:
            raise self.failure
        return "fc-household-1"


def _service(
    store: FakeStore | None = None,
    workers: FakeWorkers | None = None,
    caller: FakeCaller | None = None,
    **options: Any,
) -> tuple[HouseholdCalculationService, FakeStore, FakeWorkers, FakeCaller]:
    store = store or FakeStore()
    workers = workers or FakeWorkers()
    caller = caller or FakeCaller()
    service = HouseholdCalculationService(
        store=store,
        workers=workers,
        caller=caller,
        new_job_id=lambda: JOB_ID,
        **options,
    )
    return service, store, workers, caller


def _request(**fields: Any) -> HouseholdCalculationRequest:
    return HouseholdCalculationRequest.model_validate(
        {"country": "us", "household_id": str(HOUSEHOLD_ID), **fields}
    )


@pytest.mark.asyncio
async def test_a_calculation_creates_its_job_then_starts_and_records_its_call():
    service, store, workers, caller = _service(FakeStore(reform=REFORM))
    context = {"observability_id": "00000000-0000-4000-8000-0000000000ee"}

    accepted = await service.submit(
        _request(policy_id=str(POLICY_ID), add_missing_variables=True),
        observability_context=context,
    )

    worker = _manifest_worker("us")
    assert (accepted.id, accepted.status) == (JOB_ID, HouseholdJobStatus.PENDING)
    assert workers.countries == ["us"]
    assert store.reads == [
        {
            "country": "us",
            "household_id": HOUSEHOLD_ID,
            "policy_id": POLICY_ID,
            "policyengine_version": "6.17.1",
        }
    ]
    assert store.created == [
        NewHouseholdJob(
            id=JOB_ID,
            country="us",
            household_id=HOUSEHOLD_ID,
            policy_id=POLICY_ID,
            year=2026,
            request_data={
                "country": "us",
                "household_id": str(HOUSEHOLD_ID),
                "policy_id": str(POLICY_ID),
                "add_missing_variables": True,
            },
            tax_benefit_model_version_id=CATALOG_VERSION_ID,
            country_package_version="2.2.1",
            policyengine_version="6.17.1",
        )
    ]
    (call,) = caller.calls
    assert call["worker"] == worker
    assert call["household_job_id"] == JOB_ID
    assert call["observability_context"] == context
    assert call["payload"] == HouseholdExecutionRequest.model_validate(
        {
            "calculation_id": JOB_ID,
            "country": "us",
            "year": "2026",
            "household": SITUATION,
            "policies": [{"policy_key": "reform", "parameters": REFORM}],
            "add_missing_variables": True,
            "bundle": worker.bundle,
        }
    ).model_dump(mode="json")
    assert "spm" not in call["payload"]
    assert store.recorded == [(JOB_ID, "fc-household-1")]
    assert store.failed == []


@pytest.mark.asyncio
async def test_current_law_is_calculated_as_a_baseline_without_parameters():
    service, store, _, caller = _service()

    await service.submit(_request(), observability_context=None)

    assert store.reads[0]["policy_id"] is None
    assert store.created[0].policy_id is None
    assert caller.calls[0]["payload"]["policies"] == [
        {"policy_key": "baseline", "parameters": {}}
    ]
    assert caller.calls[0]["payload"]["add_missing_variables"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_fields", "saved", "expected_spm", "expected_source"),
    [
        # The request's settings, even an explicit null, are its choice.
        (
            {"spm": {"scenario": "zero_real"}},
            SAVED_SPM,
            {"scenario": "zero_real"},
            "request",
        ),
        ({"spm": None}, SAVED_SPM, None, "request"),
        # Otherwise the household's saved selection is used, as saved.
        ({}, SAVED_SPM, SAVED_SPM, "saved_household"),
    ],
)
async def test_the_spm_settings_come_from_the_request_or_the_saved_household(
    request_fields, saved, expected_spm, expected_source
):
    service, _, _, caller = _service(
        FakeStore(document={**STORED_DOCUMENT, "spm": saved})
    )

    await service.submit(_request(**request_fields), observability_context=None)

    payload = caller.calls[0]["payload"]
    assert payload["spm"] == expected_spm
    assert payload["spm_selection_source"] == expected_source


@pytest.mark.asyncio
async def test_without_spm_settings_or_a_saved_selection_none_is_sent():
    """The household function then inherits the bundle's certified defaults."""
    service, _, _, caller = _service(
        FakeStore(document={**STORED_DOCUMENT, "spm": None})
    )

    await service.submit(_request(), observability_context=None)

    assert "spm" not in caller.calls[0]["payload"]
    assert caller.calls[0]["payload"]["spm_selection_source"] == "request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "workers", "code", "detail"),
    [
        (
            FakeStore(),
            FakeWorkers(
                HouseholdFunctionUnavailableError(
                    "the default v2 worker, .py 6.17.1, has no household "
                    "function for us"
                )
            ),
            "no_household_function",
            "the default v2 worker, .py 6.17.1, has no household function for us",
        ),
        (
            FakeStore(
                read_failure=UnknownCatalogVersionError(
                    "us has no catalog version for .py 6.17.1"
                )
            ),
            FakeWorkers(),
            "unknown_catalog_version",
            "us has no catalog version for .py 6.17.1, the default v2 worker's version",
        ),
        (
            FakeStore(read_failure=UnknownHouseholdError("us has no household x")),
            FakeWorkers(),
            "unknown_household",
            "us has no household x",
        ),
        (
            FakeStore(read_failure=UnknownPolicyError("us has no policy y")),
            FakeWorkers(),
            "unknown_policy",
            "us has no policy y",
        ),
    ],
)
async def test_a_missing_row_or_function_is_refused_before_anything_is_written(
    store, workers, code, detail
):
    service, store, _, caller = _service(store, workers)

    with pytest.raises(HouseholdCalculationRefusedError) as raised:
        await service.submit(_request(), observability_context=None)

    assert (raised.value.code, raised.value.detail) == (code, detail)
    assert store.created == []
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "reasons"),
    [
        (
            FakeStore(default_year=None),
            [
                (
                    ("body", "household_id"),
                    "the household has no default year to calculate",
                    "household_without_default_year",
                )
            ],
        ),
        (
            FakeStore(document={"people": [{"id": "you"}]}),
            [
                (
                    ("body", "household_id"),
                    "the stored household's people or groups cannot be read: "
                    "KeyError('values')",
                    "household_unreadable",
                )
            ],
        ),
        (
            FakeStore(
                read_failure=MissingPolicyParametersError([CTC_AMOUNT, "gov.removed"])
            ),
            [
                (
                    ("body", "policy_id", name),
                    "the catalog version for .py 6.17.1 has no such parameter",
                    "missing_parameter",
                )
                for name in (CTC_AMOUNT, "gov.removed")
            ],
        ),
        (
            FakeStore(
                read_failure=ConflictingPolicyValuesError(
                    f"the policy sets {CTC_AMOUNT} twice for 2026-01-01.2026-12-31"
                )
            ),
            [
                (
                    ("body", "policy_id"),
                    f"the policy sets {CTC_AMOUNT} twice for 2026-01-01.2026-12-31",
                    "conflicting_policy_values",
                )
            ],
        ),
    ],
)
async def test_stored_data_that_cannot_be_calculated_is_unprocessable(store, reasons):
    service, store, _, caller = _service(store)

    with pytest.raises(HouseholdCalculationUnprocessableError) as raised:
        await service.submit(
            _request(policy_id=str(POLICY_ID)), observability_context=None
        )

    assert [
        (reason.loc, reason.msg, reason.type) for reason in raised.value.reasons
    ] == reasons
    assert store.created == []
    assert caller.calls == []


@pytest.mark.asyncio
async def test_what_the_household_contract_refuses_is_named_by_its_field():
    """A group no one belongs to and a policy value that is an object."""
    document = {
        **STORED_DOCUMENT,
        "family": [*STORED_DOCUMENT["family"], {"id": "nobody's family", "values": {}}],
    }
    service, store, _, caller = _service(
        FakeStore(
            document=document, reform={CTC_AMOUNT: {"2026-01-01.2200-12-31": {"a": 1}}}
        )
    )

    with pytest.raises(HouseholdCalculationUnprocessableError) as raised:
        await service.submit(
            _request(policy_id=str(POLICY_ID)), observability_context=None
        )

    locations = [reason.loc for reason in raised.value.reasons]
    assert (
        "body",
        "household_id",
        "household",
        "families",
        "nobody's family",
        "members",
    ) in locations
    assert any(
        location[:3] == ("body", "policy_id", "policies") for location in locations
    )
    assert store.created == []
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "workers", "code"),
    [
        (
            FakeStore(),
            FakeWorkers(VersionManifestUnavailableError("x")),
            "manifest_unavailable",
        ),
        (FakeStore(read_failure=DATABASE_ERROR), FakeWorkers(), "database_unavailable"),
        (
            FakeStore(create_failure=DATABASE_ERROR),
            FakeWorkers(),
            "database_unavailable",
        ),
    ],
)
async def test_an_unreadable_manifest_or_database_starts_nothing(store, workers, code):
    service, store, _, caller = _service(store, workers)

    with pytest.raises(HouseholdCalculationUnavailableError) as raised:
        await service.submit(_request(), observability_context=None)

    assert raised.value.code == code
    assert raised.value.household_job_id is None
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column_name", "detail"),
    [
        ("household_id", f"the household {HOUSEHOLD_ID} no longer exists"),
        ("policy_id", f"the policy {POLICY_ID} no longer exists"),
        (
            "tax_benefit_model_version_id",
            f"the catalog version {CATALOG_VERSION_ID} no longer exists",
        ),
        (None, "a row the household job names no longer exists"),
    ],
)
async def test_a_row_deleted_before_the_job_is_created_is_named(column_name, detail):
    """The database's foreign keys refuse the job, with SQLSTATE 23503."""
    service, _, _, caller = _service(
        FakeStore(create_failure=HouseholdJobReferenceError(column_name))
    )

    with pytest.raises(HouseholdCalculationRefusedError) as raised:
        await service.submit(
            _request(policy_id=str(POLICY_ID)), observability_context=None
        )

    assert (raised.value.code, raised.value.detail) == ("missing_reference", detail)
    assert caller.calls == []


@pytest.mark.asyncio
async def test_a_call_modal_refuses_fails_its_job_at_once():
    service, store, _, _ = _service(
        caller=FakeCaller(failure=ConnectionError("Modal is unreachable"))
    )

    with pytest.raises(HouseholdCalculationUnavailableError) as raised:
        await service.submit(_request(), observability_context=None)

    assert raised.value.code == "modal_call_refused"
    assert raised.value.household_job_id == JOB_ID
    assert store.failed == [
        (
            JOB_ID,
            "Modal did not accept the household function's call (ConnectionError).",
        )
    ]
    assert store.recorded == []


@pytest.mark.asyncio
async def test_a_call_modal_does_not_accept_in_time_fails_its_job():
    service, store, _, _ = _service(
        caller=FakeCaller(delay_seconds=1.0), call_start_timeout_seconds=0.01
    )

    with pytest.raises(HouseholdCalculationUnavailableError) as raised:
        await service.submit(_request(), observability_context=None)

    assert raised.value.code == "modal_call_refused"
    assert store.failed == [
        (
            JOB_ID,
            "Modal did not accept the household function's call (TimeoutError).",
        )
    ]


@pytest.mark.asyncio
async def test_a_call_whose_id_cannot_be_recorded_is_still_accepted():
    """The household function claims a pending job with no call ID itself."""
    service, store, _, _ = _service(FakeStore(record_failure=DATABASE_ERROR))

    accepted = await service.submit(_request(), observability_context=None)

    assert (accepted.id, accepted.status) == (JOB_ID, HouseholdJobStatus.PENDING)
    assert store.failed == []


@pytest.mark.asyncio
async def test_a_job_the_function_claimed_first_is_answered_as_it_stands():
    service, _, _, _ = _service(FakeStore(recorded_status=HouseholdJobStatus.RUNNING))

    accepted = await service.submit(_request(), observability_context=None)

    assert accepted.status is HouseholdJobStatus.RUNNING


def _record(job_id: UUID) -> HouseholdJobRecord:
    return HouseholdJobRecord(
        id=job_id,
        status=HouseholdJobStatus.SUCCEEDED,
        country="us",
        household_id=HOUSEHOLD_ID,
        policy_id=None,
        year=2026,
        request_data={},
        tax_benefit_model_version_id=CATALOG_VERSION_ID,
        country_package_version="2.2.1",
        policyengine_version="6.17.1",
        modal_call_id="fc-1",
        result={"status": "succeeded"},
        error_message=None,
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        completed_at=datetime(2026, 11, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_a_job_is_read_as_it_stands_and_an_unknown_one_as_none():
    service, _, _, _ = _service(FakeStore(stored_job=_record(JOB_ID)))

    assert await service.read(JOB_ID) == _record(JOB_ID)
    assert await service.read(uuid4()) is None


@pytest.mark.asyncio
async def test_a_job_that_cannot_be_read_is_unavailable():
    service, _, _, _ = _service(FakeStore(read_failure=DATABASE_ERROR))

    with pytest.raises(HouseholdCalculationUnavailableError) as raised:
        await service.read(JOB_ID)

    assert raised.value.code == "database_unavailable"
