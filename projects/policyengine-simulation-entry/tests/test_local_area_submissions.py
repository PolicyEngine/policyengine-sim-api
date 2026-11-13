"""Economy reports on the local-area dataset, with the store and Modal faked.

A UK-wide or nation report created with ``data_source: "local_area_dataset"``
is calculated on the dataset the default worker's UK entry declares for
local-area report runs (``local_area_reports``): the one the release routes
constituencies and local authorities to. A release that routes them to no
dataset of its own has none, and the request is refused.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from economy_fixtures import (
    LOCAL_AREA_DATASET,
    ROUTED_LOCAL_AREAS,
    local_area_worker_version,
)
from economy_rerun_fakes import (
    REPORT_ID,
    STORED_REPORT,
    FakeRerunStore,
    rerun_service,
)
from economy_service_fakes import (
    CATALOG,
    REPORT_BODY,
    FakeCaller,
    FakeStore,
    FakeWorkers,
    service,
)
from policyengine_simulation_contract.stage12_execution import ReportExecutionInput
from policyengine_simulation_contract.stage12_manifest import LocalAreaReports
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportRequest,
    ReportRerunRequest,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    LocalAreaDatasetMismatchError,
)

if TYPE_CHECKING:
    from policyengine_simulation_entry.economy_api_models import (
        EconomyReportCreated,
    )
    from policyengine_simulation_entry.economy_submissions import (
        EconomySubmissionService,
    )

# The UK worker's default dataset, which a UK-wide report calculates on.
NATIONAL_DATASET_NAME = "populace_uk_2024"
UK_CATALOG = replace(
    CATALOG,
    region=replace(CATALOG.region, code="uk"),
    dataset=CatalogDataset(id=CATALOG.dataset.id, name=NATIONAL_DATASET_NAME),
)
CATALOG_LOCAL_AREA_DATASET = CatalogDataset(
    id=UUID("00000000-0000-4000-8000-0000000000c7"), name=LOCAL_AREA_DATASET.identity
)


def _uk_report_request(**changes: Any) -> EconomyReportRequest:
    """Return a UK-wide report request, with no SPM settings."""
    body = {name: value for name, value in REPORT_BODY.items() if name != "spm"}
    return EconomyReportRequest.model_validate(
        {**body, "country": "uk", "region_code": "uk", **changes}
    )


def _local_area_report_request(**changes: Any) -> EconomyReportRequest:
    return _uk_report_request(data_source="local_area_dataset", **changes)


def _submit(
    submissions: EconomySubmissionService, request: EconomyReportRequest
) -> EconomyReportCreated:
    return asyncio.run(
        submissions.submit_report(
            request, request_id="observed", observability_context=None
        )
    )


def _workers_declaring(local_area_reports: LocalAreaReports | None) -> FakeWorkers:
    return FakeWorkers(worker_version=local_area_worker_version(local_area_reports))


def test_the_uk_worker_fixture_calculates_on_its_default_dataset() -> None:
    """The national dataset the tests' UK catalog names is the UK bundle's default."""
    worker = _workers_declaring(None).default_worker("uk")

    assert worker.country.default_dataset == NATIONAL_DATASET_NAME


@pytest.mark.parametrize(
    "declaration",
    [
        None,
        LocalAreaReports(input_version=2, dataset=LOCAL_AREA_DATASET.identity),
    ],
    ids=["no declaration", "another input version"],
)
def test_the_local_area_dataset_the_worker_cannot_run_is_refused_before_any_read(
    declaration,
) -> None:
    store = FakeStore(catalog=UK_CATALOG)

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit(
            service(store, workers=_workers_declaring(declaration)),
            _local_area_report_request(),
        )

    assert refusal.value.code == "local_areas_unsupported"
    assert store.report_reads == []
    assert store.reports == []


def test_a_report_without_a_data_source_needs_no_declaration() -> None:
    store = FakeStore(catalog=UK_CATALOG)

    _submit(service(store, workers=_workers_declaring(None)), _uk_report_request())

    assert store.local_area_reads == [None]
    (report,) = store.reports
    assert report.population.dataset_id == UK_CATALOG.dataset.id


def test_a_release_that_routes_no_areas_has_no_local_area_dataset() -> None:
    """Its national reports calculate both area levels themselves (decision 8)."""
    store = FakeStore(catalog=UK_CATALOG)
    declaration = LocalAreaReports(input_version=1, dataset=None)

    with pytest.raises(EconomySubmissionUnprocessableError) as refusal:
        _submit(
            service(store, workers=_workers_declaring(declaration)),
            _local_area_report_request(),
        )

    assert refusal.value.code == "no_local_area_dataset"
    (reason,) = refusal.value.reasons
    assert reason.loc == ("body", "data_source")
    assert "routes constituencies and local authorities to no dataset" in reason.msg
    assert store.report_reads == []
    assert store.reports == []


def test_the_report_is_calculated_on_the_local_area_dataset_the_worker_routes_to() -> (
    None
):
    store = FakeStore(catalog=UK_CATALOG, local_area_dataset=CATALOG_LOCAL_AREA_DATASET)
    caller = FakeCaller()

    _submit(
        service(store, workers=_workers_declaring(ROUTED_LOCAL_AREAS), caller=caller),
        _local_area_report_request(),
    )

    assert store.local_area_reads == [LOCAL_AREA_DATASET.identity]
    ((request, _),) = store.report_reads
    # The dataset comes from the declaration, never from the request.
    assert request.dataset_name is None
    (report,) = store.reports
    assert report.population.dataset_id == CATALOG_LOCAL_AREA_DATASET.id
    # An ordinary report: its options are the request's, cliffs included.
    assert report.inputs == {"include_cliffs": True}
    ((_, payload, _, _),) = caller.report_runs
    execution = ReportExecutionInput.model_validate(payload)
    for side in (execution.baseline, execution.reform):
        assert side.bundle.dataset.identity == LOCAL_AREA_DATASET.identity
        assert side.population.artifact.uri == LOCAL_AREA_DATASET.uri
        assert side.geography.region == "uk"
        assert side.options == {"include_cliffs": True}


def test_a_catalog_whose_area_regions_use_another_dataset_is_refused() -> None:
    store = FakeStore(
        catalog=UK_CATALOG,
        read_failure=LocalAreaDatasetMismatchError(LOCAL_AREA_DATASET.identity),
    )

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit(
            service(store, workers=_workers_declaring(ROUTED_LOCAL_AREAS)),
            _local_area_report_request(),
        )

    assert refusal.value.code == "local_area_dataset_mismatch"
    # Written from the declaration, never from the error's text.
    assert LOCAL_AREA_DATASET.identity in refusal.value.detail
    assert store.reports == []


def test_a_report_on_the_local_area_dataset_is_rerun_on_it() -> None:
    """A re-run reads the stored report's dataset by name, and takes no data_source."""
    stored = replace(
        STORED_REPORT,
        country="uk",
        region_code="uk",
        dataset_name=LOCAL_AREA_DATASET.identity,
        inputs={"include_cliffs": True},
    )
    store = FakeRerunStore(
        report_definition=stored,
        catalog=replace(UK_CATALOG, dataset=CATALOG_LOCAL_AREA_DATASET),
    )
    caller = FakeCaller()

    asyncio.run(
        rerun_service(
            store, workers=_workers_declaring(ROUTED_LOCAL_AREAS), caller=caller
        ).rerun_report(
            REPORT_ID,
            ReportRerunRequest(),
            request_id="observed",
            observability_context=None,
        )
    )

    ((request, _),) = store.rerun_reads
    assert (request.region_code, request.dataset_name) == (
        "uk",
        LOCAL_AREA_DATASET.identity,
    )
    ((_, payload, _, _),) = caller.report_runs
    execution = ReportExecutionInput.model_validate(payload)
    for side in (execution.baseline, execution.reform):
        assert side.bundle.dataset.identity == LOCAL_AREA_DATASET.identity
        assert side.geography.region == "uk"
