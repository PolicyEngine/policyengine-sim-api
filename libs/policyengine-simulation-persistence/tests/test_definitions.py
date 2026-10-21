"""New simulation and report definitions are checked before any write."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import NewSimulation, SimulationKind


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
