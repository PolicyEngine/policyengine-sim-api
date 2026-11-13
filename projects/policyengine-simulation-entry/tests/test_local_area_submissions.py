"""Economy reports that ask for their local areas, with the store and Modal faked.

A UK-wide or nation report with ``breakdown: "local_areas"`` is calculated
by the default worker only when its UK entry declares the local-area report
runs its coordinator runs (``local_area_reports``). The declaration's
dataset is the one the release routes constituencies and local authorities
to, or None when a national run calculates them itself.
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
from economy_service_fakes import (
    CATALOG,
    REPORT_BODY,
    FakeStore,
    FakeWorkers,
    service,
)
from policyengine_simulation_contract.stage12_manifest import LocalAreaReports
from policyengine_simulation_entry.economy_api_models import EconomyReportRequest
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
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
def test_local_areas_the_worker_cannot_run_are_refused_before_any_read(
    declaration,
) -> None:
    store = FakeStore(catalog=UK_CATALOG)

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit(
            service(store, workers=_workers_declaring(declaration)),
            _uk_report_request(breakdown="local_areas"),
        )

    assert refusal.value.code == "local_areas_unsupported"
    assert store.report_reads == []
    assert store.reports == []


def test_a_report_without_local_areas_needs_no_declaration() -> None:
    store = FakeStore(catalog=UK_CATALOG)

    _submit(service(store, workers=_workers_declaring(None)), _uk_report_request())

    assert store.local_area_reads == [None]
    assert len(store.reports) == 1


def test_local_areas_on_a_release_that_routes_none_are_the_national_runs() -> None:
    """The national run calculates both area levels, so no area part is made."""
    store = FakeStore(catalog=UK_CATALOG)
    declaration = LocalAreaReports(input_version=1, dataset=None)

    _submit(
        service(store, workers=_workers_declaring(declaration)),
        _uk_report_request(breakdown="local_areas"),
    )

    assert store.local_area_reads == [None]
    (report,) = store.reports
    # The breakdown is never stored.
    assert report.inputs == {"include_cliffs": True}


def test_the_catalog_is_asked_for_the_local_area_dataset_the_worker_routes_to() -> None:
    store = FakeStore(catalog=UK_CATALOG, local_area_dataset=CATALOG_LOCAL_AREA_DATASET)

    _submit(
        service(store, workers=_workers_declaring(ROUTED_LOCAL_AREAS)),
        _uk_report_request(breakdown="local_areas", dataset_name=NATIONAL_DATASET_NAME),
    )

    # Never the request's dataset_name (decision 4).
    assert store.local_area_reads == [LOCAL_AREA_DATASET.identity]


def test_a_catalog_whose_area_regions_use_another_dataset_is_refused() -> None:
    store = FakeStore(
        catalog=UK_CATALOG,
        read_failure=LocalAreaDatasetMismatchError(LOCAL_AREA_DATASET.identity),
    )

    with pytest.raises(EconomySubmissionRefusedError) as refusal:
        _submit(
            service(store, workers=_workers_declaring(ROUTED_LOCAL_AREAS)),
            _uk_report_request(breakdown="local_areas"),
        )

    assert refusal.value.code == "local_area_dataset_mismatch"
    # Written from the declaration, never from the error's text.
    assert LOCAL_AREA_DATASET.identity in refusal.value.detail
    assert store.reports == []
