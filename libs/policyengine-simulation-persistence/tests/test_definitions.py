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


def test_simulation_options_must_be_a_json_object() -> None:
    with pytest.raises(TypeError, match="options must be a JSON object"):
        NewSimulation(
            kind=SimulationKind.HOUSEHOLD,
            country="us",
            household_id=uuid4(),
            options=["add_missing_variables"],  # type: ignore[arg-type]
        )


def test_a_kind_given_as_text_becomes_the_enum() -> None:
    simulation = NewSimulation(kind="economy", country="us", region_code="state/ca")  # type: ignore[arg-type]
    report = NewReport(
        kind="household",  # type: ignore[arg-type]
        country="us",
        baseline_simulation_id=uuid4(),
        year=2026,
    )

    assert simulation.kind is SimulationKind.ECONOMY
    assert report.kind is ReportKind.HOUSEHOLD


def test_an_unknown_simulation_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        NewSimulation(kind="microsimulation", country="us", region_code="state/ca")  # type: ignore[arg-type]


def test_an_unknown_report_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        NewReport(
            kind="microsimulation",  # type: ignore[arg-type]
            country="us",
            baseline_simulation_id=uuid4(),
            year=2026,
        )
