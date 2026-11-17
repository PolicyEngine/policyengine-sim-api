"""Which dataset a report created with ``data_source: "local_area_dataset"`` runs on.

Such a UK-wide or nation report runs on the dataset the .py release routes
constituencies and local authorities to, and returns its full results and
both area levels. The default worker's UK entry in the v2 version manifest
declares both whether its coordinator runs such report runs
(``local_area_reports``, with the input version it accepts) and that
dataset, which the publisher computes in the worker's own environment by the
rule Stage 12's national aggregation uses to leave the areas out
(``uk_area_regions_routed``). So the service, the national runs and the
worker decide alike on every release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_manifest import (
    LOCAL_AREA_REPORTS_VERSION,
)

from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)

if TYPE_CHECKING:
    from policyengine_simulation_entry.economy_api_models import (
        EconomyReportRequest,
    )
    from policyengine_simulation_entry.economy_workers import EconomyWorker


def local_area_dataset_name(
    request: EconomyReportRequest, worker: EconomyWorker
) -> str | None:
    """Return the dataset a report on the local-area dataset runs on, or None.

    None means the request names no ``data_source``: the report runs on its
    region's dataset. Raise EconomySubmissionRefusedError
    (``local_areas_unsupported``, the service's state) when the default
    worker declares no local-area report runs of the input version this
    service sends, and EconomySubmissionUnprocessableError
    (``no_local_area_dataset``) when its release routes constituencies and
    local authorities to no dataset of their own: there, national reports
    calculate both area levels themselves.
    """
    if request.data_source is None:
        return None
    declared = worker.local_area_reports
    if declared is None or declared.input_version != LOCAL_AREA_REPORTS_VERSION:
        raise EconomySubmissionRefusedError(
            code="local_areas_unsupported",
            detail=(
                f"The default v2 worker, .py {worker.version}, cannot calculate "
                f"a {request.country} report on the local-area dataset."
            ),
        )
    if declared.dataset is None:
        raise EconomySubmissionUnprocessableError(
            code="no_local_area_dataset",
            reasons=(
                UnprocessableReason(
                    loc=("body", "data_source"),
                    msg=(
                        f".py {worker.policyengine_version} routes constituencies "
                        "and local authorities to no dataset of their own, so it "
                        "has no local-area dataset; its national reports "
                        "calculate both area levels"
                    ),
                    type="no_local_area_dataset",
                ),
            ),
        )
    return declared.dataset
