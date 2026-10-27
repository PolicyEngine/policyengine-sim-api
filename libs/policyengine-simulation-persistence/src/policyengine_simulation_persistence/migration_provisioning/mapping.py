"""The file a run writes: each v1 report's v2 report ID, schema version 1.

The Public API's one-time migration command reads it and sets each account
row's v2 report ID from it. ``README.md`` next to this module describes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PositiveInt

if TYPE_CHECKING:
    from policyengine_simulation_persistence.migration_provisioning import (
        ProvisioningResult,
    )

REPORT_MAPPING_SCHEMA_VERSION = 1


class _MappingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MappedReport(_MappingModel):
    """A v1 report and the v2 report that holds its definition."""

    legacy_report_id: PositiveInt
    report_id: UUID
    # False when an earlier run had already created the report.
    created: bool


class FailedReport(_MappingModel):
    """A v1 report the run could not provision, and why."""

    legacy_report_id: PositiveInt
    reason: str


class ReportMappingFile(_MappingModel):
    """Every provisioned v1 report's v2 report ID, and every failed report."""

    schema_version: Literal[1]
    reports: list[MappedReport]
    failures: list[FailedReport]

    @classmethod
    def from_result(cls, result: ProvisioningResult) -> ReportMappingFile:
        """Describe a run's result, in the order of the export's definitions."""
        return cls(
            schema_version=REPORT_MAPPING_SCHEMA_VERSION,
            reports=[
                MappedReport(
                    legacy_report_id=provisioned.legacy_report_id,
                    report_id=provisioned.report_id,
                    created=provisioned.created,
                )
                for provisioned in result.reports
            ],
            failures=[
                FailedReport(
                    legacy_report_id=failure.legacy_report_id,
                    reason=failure.reason,
                )
                for failure in result.failures
            ],
        )
