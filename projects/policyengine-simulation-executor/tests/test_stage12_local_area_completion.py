"""Tests for completing a local-area report run with its area results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from policyengine_simulation_executor.stage12_runtime.report_run_recording import (
    ReportRunRecorder,
)
from policyengine_stage12_persistence import RunStatus, RunWrite
from policyengine_stage12_persistence.local_area_results import local_area_result
from stage12_fake_report_runs import REPORT_RUN_IDS

MODAL_CALL_ID = "fc-local-area-coordinator"
AREA_OUTPUT = {
    "kind": "local_areas",
    "model_version": "2.174.1",
    "policyengine_version": "6.17.1",
    "dataset": "microcosm_uk_2024_25_dense",
    "data_version": "microcosm-uk-2024-25-dense-20261107T173210Z-38605444",
    "constituency_impact": [],
    "local_authority_impact": [],
    "constituency_withheld_codes": ["S14000045"],
    "local_authority_withheld_codes": [],
}


@dataclass
class _CompletionWriter:
    """Records each completion a recorder asks for."""

    completions: list[dict[str, Any]] = field(default_factory=list)

    def complete_report_run(
        self, run_ids, *, modal_call_id, completed_at, result=None
    ) -> RunWrite:
        self.completions.append(
            {"run_ids": run_ids, "modal_call_id": modal_call_id, "result": result}
        )
        return RunWrite(changed=True, status=RunStatus.SUCCEEDED)


def test_a_local_area_completion_stores_the_area_rows_under_the_calls_claim() -> None:
    writer = _CompletionWriter()
    recorder = ReportRunRecorder(
        writer=writer,  # pyright: ignore[reportArgumentType]
        run_ids=REPORT_RUN_IDS,
        modal_call_id=MODAL_CALL_ID,
    )

    written = recorder.complete_local_areas(AREA_OUTPUT)

    assert written == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    (completion,) = writer.completions
    assert completion["run_ids"] == REPORT_RUN_IDS
    assert completion["modal_call_id"] == MODAL_CALL_ID
    expected = local_area_result(AREA_OUTPUT, run_ids=REPORT_RUN_IDS)
    stored = completion["result"]
    assert stored.report_run_columns == expected.report_run_columns
    assert [
        (rows.table.name, [{**row, "id": None} for row in rows.rows])
        for rows in stored.rows
    ] == [
        (rows.table.name, [{**row, "id": None} for row in rows.rows])
        for rows in expected.rows
    ]
