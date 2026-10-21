"""New simulation and report definitions are checked before any write."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    NewReport,
    NewSimulation,
    ReportKind,
    SimulationKind,
)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("country", "USA"),
        ("country", "Us"),
        ("region_code", ""),
        ("region_code", " state/ca"),
        ("region_code", "x" * 256),
        ("dataset_name", ""),
        ("dataset_name", "populace us"),
        ("legacy_simulation_id", 0),
    ],
)
def test_malformed_simulation_definitions_are_rejected(field_name, value) -> None:
    values = {
        "kind": SimulationKind.ECONOMY,
        "country": "us",
        "region_code": "state/ca",
        field_name: value,
    }

    with pytest.raises(ValueError, match=field_name):
        NewSimulation(**values)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("country", "gb "),
        ("year", 1899),
        ("year", 2201),
        ("legacy_report_id", -4),
    ],
)
def test_malformed_report_definitions_are_rejected(field_name, value) -> None:
    values = {
        "kind": ReportKind.ECONOMY,
        "country": "us",
        "baseline_simulation_id": uuid4(),
        "reform_simulation_id": uuid4(),
        "year": 2026,
        field_name: value,
    }

    with pytest.raises(ValueError, match=field_name):
        NewReport(**values)


def test_report_options_must_be_a_json_object() -> None:
    with pytest.raises(TypeError, match="options"):
        NewReport(
            kind=ReportKind.ECONOMY,
            country="us",
            baseline_simulation_id=uuid4(),
            year=2026,
            options=["not", "an", "object"],  # type: ignore[arg-type]
        )
