"""Builders for the migration provisioning tests: exported v1 report definitions."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from policyengine_simulation_persistence.migration_provisioning import (
    ReportDefinitionsFile,
)

US_REFORM_POLICY_ID = UUID("00000000-0000-4000-8000-000000000001")
US_HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-000000000002")


def economy_simulation_definition(
    legacy_simulation_id: int, **overrides: object
) -> dict[str, Any]:
    """Return a current-law California economy simulation, as exported."""
    definition: dict[str, Any] = {
        "legacy_simulation_id": legacy_simulation_id,
        "policy_id": None,
        "region_code": "state/ca",
        "dataset_name": None,
    }
    definition.update(overrides)
    return definition


def household_simulation_definition(
    legacy_simulation_id: int, **overrides: object
) -> dict[str, Any]:
    """Return a current-law simulation of the US household, as exported."""
    definition: dict[str, Any] = {
        "legacy_simulation_id": legacy_simulation_id,
        "policy_id": None,
        "household_id": str(US_HOUSEHOLD_ID),
    }
    definition.update(overrides)
    return definition


def economy_report_definition(
    legacy_report_id: int = 4512, **overrides: object
) -> dict[str, Any]:
    """Return a 2026 US economy report of a reform in California, as exported."""
    definition: dict[str, Any] = {
        "kind": "economy",
        "legacy_report_id": legacy_report_id,
        "country": "us",
        "year": 2026,
        "options": {},
        "baseline": economy_simulation_definition(9001),
        "reform": economy_simulation_definition(
            9002, policy_id=str(US_REFORM_POLICY_ID)
        ),
    }
    definition.update(overrides)
    return definition


def household_report_definition(
    legacy_report_id: int = 4513, **overrides: object
) -> dict[str, Any]:
    """Return a 2025 report of the US household under current law, as exported."""
    definition: dict[str, Any] = {
        "kind": "household",
        "legacy_report_id": legacy_report_id,
        "country": "us",
        "year": 2025,
        "options": {},
        "baseline": household_simulation_definition(9003),
        "reform": None,
    }
    definition.update(overrides)
    return definition


def definitions_json(*report_definitions: dict[str, Any], schema_version=1) -> str:
    """Return an export file holding ``report_definitions``."""
    return json.dumps(
        {"schema_version": schema_version, "reports": list(report_definitions)}
    )


def parse_definitions(*report_definitions: dict[str, Any]) -> ReportDefinitionsFile:
    """Parse an export file holding ``report_definitions``."""
    return ReportDefinitionsFile.model_validate_json(
        definitions_json(*report_definitions)
    )
