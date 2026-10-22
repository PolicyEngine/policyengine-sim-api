"""Samples and builders for the result table tests.

The sample under ``fixtures/economy_results`` holds one complete economy
result per case the plan names: US national, state and congressional district;
UK national, nation, constituency and local authority; and a budget-window
year. Each is generated, sized like a production result, with values of
varied sign and magnitude (negative zero included), because no stored real
results could be read without starting new production calculations. An
exported real result can replace any file without a code change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_persistence import (
    EconomyResultLabels,
    GeographyLabel,
    NewReportRun,
    RunTrigger,
)
from report_support import economy_report, report_store
from run_support import economy_run
from simulation_support import economy_simulation, simulation_store
from transition_support import clocked_stores

if TYPE_CHECKING:
    from policyengine_simulation_persistence import ReportRunRecord

SAMPLE_DIRECTORY = Path(__file__).parent / "fixtures" / "economy_results"
SAMPLE_NAMES = sorted(path.stem for path in SAMPLE_DIRECTORY.glob("*.json"))


def load_sample(name: str) -> SingleYearMacroOutput:
    """Return one sample result, validated."""
    return SingleYearMacroOutput.model_validate(
        json.loads((SAMPLE_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))
    )


def labels_for(result: SingleYearMacroOutput) -> EconomyResultLabels:
    """Return the region catalog's labels for a result, taken from the result."""

    def labels(records, code_field: str, name_field: str):
        return {
            getattr(record, code_field): GeographyLabel(
                name=getattr(record, name_field), x=record.x, y=record.y
            )
            for record in (records.root if records is not None else [])
        }

    return EconomyResultLabels(
        model_version=result.model_version,
        data_version=result.data_version,
        constituencies=labels(
            result.constituency_impact, "constituency_code", "constituency_name"
        ),
        local_authorities=labels(
            result.local_authority_impact,
            "local_authority_code",
            "local_authority_name",
        ),
    )


def comparison_ready(stores, database, rows, *, baseline=None) -> ReportRunRecord:
    """Return an economy report run whose comparison step is running.

    Both simulation runs have succeeded. Pass ``baseline`` to reuse an
    existing current-law simulation, as a second report on it would.
    """
    simulations = simulation_store(database)
    baseline = baseline or simulations.create(economy_simulation(rows, policy_id=None))
    reform = simulations.create(economy_simulation(rows))
    report = report_store(database).create(economy_report(baseline, reform))
    runs = [
        stores.runs.start_simulation_run(economy_run(simulation, rows)).run
        for simulation in (baseline, reform)
    ]
    for run in runs:
        if run.status.value == "succeeded":
            continue
        call_id = f"fc-{run.id}"
        stores.transitions.claim_simulation_dispatch([run.id])
        stores.transitions.record_simulation_dispatched(
            [run.id], modal_call_id=call_id, first_check_at=stores.clock()
        )
        stores.transitions.complete_simulation_call(call_id, lambda *_: None)
    report_run = stores.runs.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=runs[0].id,
            reform_simulation_run_id=runs[1].id,
            trigger=RunTrigger.INITIAL,
        )
    )
    stores.transitions.claim_comparison_dispatch(report_run.id)
    stores.transitions.record_comparison_dispatched(
        report_run.id,
        modal_call_id=f"fc-comparison-{report_run.id}",
        first_check_at=stores.clock(),
    )
    return report_run


def result_stores(database):
    """Return the run store and transitions every result test uses."""
    return clocked_stores(database)
