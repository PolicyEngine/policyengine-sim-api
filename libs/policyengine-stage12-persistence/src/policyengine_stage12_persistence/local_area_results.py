"""A local-area report run's area results as rows of the Public API's tables.

A run of a UK-wide or nation report on the local-area dataset calculates the
constituency and local-authority results alone, as a
``LocalAreaBreakdownOutput``. They become ``constituency_impacts`` and
``local_authority_impacts`` rows, as a national result's areas do, and one
``withheld_area_codes`` row per area disclosure control withheld, each keyed
by the report run and its two simulation runs. The run's own data version is
its only result column; it stores no budget, decile, poverty, inequality,
programme, district or labour-supply rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from policyengine_simulation_contract.macro_output import LocalAreaBreakdownOutput

from policyengine_stage12_persistence.report_run_results import (
    ReportRunResult,
    ResultRows,
    area_record_rows,
    constituency_impacts,
    local_authority_impacts,
    withheld_area_codes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_contract.stage12_execution import ReportRunIds


def local_area_result(
    output: Mapping[str, Any], *, run_ids: ReportRunIds
) -> ReportRunResult:
    """Validate a local-area result and return what it is stored as.

    Raises ValueError (a pydantic ValidationError for another kind of result)
    when it is not a local-area result, or does not list each level's
    withheld codes: without them, an area with no row could be withheld or
    never estimated.
    """
    areas = LocalAreaBreakdownOutput.model_validate(output)
    withheld = {
        "constituency": areas.constituency_withheld_codes,
        "local_authority": areas.local_authority_withheld_codes,
    }
    if any(codes is None for codes in withheld.values()):
        msg = "a local-area result must list each level's withheld codes"
        raise ValueError(msg)
    keys = {
        "report_run_id": run_ids.report_run_id,
        "baseline_simulation_run_id": run_ids.baseline_simulation_run_id,
        "reform_simulation_run_id": run_ids.reform_simulation_run_id,
    }
    return ReportRunResult(
        report_run_columns={"data_version": areas.data_version},
        rows=(
            ResultRows(
                constituency_impacts,
                area_record_rows(areas.constituency_impact, keys, "constituency"),
                shared_by_reports=False,
            ),
            ResultRows(
                local_authority_impacts,
                area_record_rows(areas.local_authority_impact, keys, "local_authority"),
                shared_by_reports=False,
            ),
            ResultRows(
                withheld_area_codes,
                tuple(
                    {"id": uuid4(), **keys, "area_type": area_type, "code": code}
                    for area_type, codes in withheld.items()
                    for code in codes or ()
                ),
                shared_by_reports=False,
            ),
        ),
    )
