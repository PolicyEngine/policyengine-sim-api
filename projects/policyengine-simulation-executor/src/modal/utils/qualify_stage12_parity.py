"""Run one controlled Stage 12 parity qualification from canonical JSON files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from policyengine_simulation_executor.stage12_parity import NumericalTolerance
from policyengine_simulation_executor.stage12_qualification import (
    qualify_report_parity,
)


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _tolerances(path: Path | None) -> dict[str, NumericalTolerance]:
    if path is None:
        return {}
    value = _json_object(path)
    tolerances = {}
    for field, configuration in value.items():
        if not isinstance(configuration, dict):
            raise ValueError(f"tolerance for {field!r} must be an object")
        unknown = set(configuration) - {"absolute", "relative"}
        if unknown:
            raise ValueError(f"tolerance for {field!r} has unknown fields")
        tolerances[field] = NumericalTolerance(
            absolute=float(configuration.get("absolute", 0.0)),
            relative=float(configuration.get("relative", 0.0)),
        )
    return tolerances


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the existing combined executor with the Stage 12 independent "
            "simulation and aggregation path."
        )
    )
    parser.add_argument("--existing-request", type=Path, required=True)
    parser.add_argument("--report-input", type=Path, required=True)
    parser.add_argument("--simulation-tolerances", type=Path)
    parser.add_argument("--aggregate-tolerances", type=Path)
    arguments = parser.parse_args()

    receipt = qualify_report_parity(
        _json_object(arguments.existing_request),
        _json_object(arguments.report_input),
        simulation_tolerances=_tolerances(arguments.simulation_tolerances),
        aggregate_tolerances=_tolerances(arguments.aggregate_tolerances),
    )
    print(receipt.model_dump_json())


if __name__ == "__main__":
    main()
