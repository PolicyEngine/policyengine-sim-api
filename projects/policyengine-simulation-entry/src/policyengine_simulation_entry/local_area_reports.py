"""Whether an economy report gets a local-area part, and on which dataset.

A UK-wide or nation report that asks for ``breakdown: "local_areas"`` gets
its constituency and local-authority impacts from a local-area report run of
its own, on the dataset the .py release routes those regions to. The
default worker's UK entry in the v2 version manifest declares both whether
its coordinator runs such runs (``local_area_reports``, with the input
version it accepts) and that dataset, which the publisher computes in the
worker's own environment by the rule Stage 12's national aggregation uses
to leave the areas out (``uk_area_regions_routed``). So the service, the
national run and the worker decide alike on every release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_manifest import (
    LOCAL_AREA_REPORTS_VERSION,
)

from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
)

if TYPE_CHECKING:
    from policyengine_simulation_entry.economy_api_models import (
        EconomyReportRequest,
    )
    from policyengine_simulation_entry.economy_workers import EconomyWorker


def local_area_dataset_name(
    request: EconomyReportRequest, worker: EconomyWorker
) -> str | None:
    """Return the dataset the report's area part is calculated on, or None.

    None means no area part: the request asks for none, or the release
    routes the area regions to no dataset of their own, so the national run
    calculates both area levels itself. Raise EconomySubmissionRefusedError
    (``local_areas_unsupported``) when the request asks for local areas and
    the default worker declares no local-area report runs of the input
    version this service sends.
    """
    if request.breakdown is None:
        return None
    declared = worker.local_area_reports
    if declared is None or declared.input_version != LOCAL_AREA_REPORTS_VERSION:
        raise EconomySubmissionRefusedError(
            code="local_areas_unsupported",
            detail=(
                f"The default v2 worker, .py {worker.version}, cannot calculate "
                f"a {request.country} report's local areas."
            ),
        )
    return declared.dataset
