"""Typed scope and requested-resource estimates for the approved ACS pilot.

This is not an invoice calculator or a provider-enforced budget. Rates were
checked at https://modal.com/pricing on 2026-10-08; actual usage, startup, storage,
and platform-managed replacement containers can change the billed amount.
"""

from __future__ import annotations

import math

from policyengine.provenance.dataset_materialization import (
    DatasetSource,
    MaterializedDataset,
)
from policyengine.tax_benefit_models.us.state_preparation import USPartitionManifest
from pydantic import Field

from .parallel import StateYearTask, USStateYearPlan
from .profiling import Measurement, StrictModel

DATASET_NAME = "populace_us_2024_acs_local"
APPROVED_BUDGET_USD = 20.0
STARTUP_ALLOWANCE_SECONDS = 300
CPU_PRICE_PER_CORE_SECOND = 0.0000131
MEMORY_PRICE_PER_GIB_SECOND = 0.00000222


class ExecutionResources(StrictModel):
    cpu: float = Field(gt=0)
    memory_mib: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)


PARTITION_RESOURCES = ExecutionResources(
    cpu=8, memory_mib=128 * 1024, timeout_seconds=3600
)
PREPARATION_RESOURCES = ExecutionResources(
    cpu=8, memory_mib=64 * 1024, timeout_seconds=3600
)


class PilotPartitionResult(StrictModel):
    manifest: USPartitionManifest
    measurement: Measurement


def build_pilot_plan(manifest: USPartitionManifest) -> USStateYearPlan:
    """Select exactly California/Utah for 2025–2027, pairing states per year."""
    partitions = {part.state_code: part for part in manifest.partitions}
    return USStateYearPlan(
        tasks=tuple(
            StateYearTask(partition=partitions[state], year=year)
            for year in (2025, 2026, 2027)
            for state in ("CA", "UT")
        )
    )


def require_certified_pilot_source(
    selected: DatasetSource, expected_sha256: str
) -> MaterializedDataset:
    """Do not accept an unmanaged source or a different bundled artifact."""
    source = selected.bundle_dataset
    if source is None:
        raise ValueError("The pilot requires a bundle-certified source")
    if source.sha256 != expected_sha256:
        raise ValueError("Worker and submitter disagree on the certified SHA-256")
    return source


def requested_resource_cost(
    resources: ExecutionResources, elapsed_seconds: float
) -> float:
    """Estimate compute charges at requested resources, not actual billing."""
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("Elapsed seconds must be finite and nonnegative")
    return elapsed_seconds * (
        resources.cpu * CPU_PRICE_PER_CORE_SECOND
        + resources.memory_mib / 1024 * MEMORY_PRICE_PER_GIB_SECOND
    )


def require_pilot_budget(
    budget_usd: float, *, partition_elapsed_seconds: float | None = None
) -> float:
    """Reserve all six one-hour worker requests before dispatching any of them.

    Include a five-minute startup allowance per call. Before partitioning,
    reserve its full timeout; afterwards substitute the observed invocation
    interval, including waiting and startup. This deliberately overestimates
    ordinary execution but cannot impose an absolute provider billing ceiling.
    """
    if not math.isfinite(budget_usd) or not 0 < budget_usd <= APPROVED_BUDGET_USD:
        raise ValueError("Pilot budget must be positive and no greater than $20")
    partition_seconds = (
        PARTITION_RESOURCES.timeout_seconds
        if partition_elapsed_seconds is None
        else partition_elapsed_seconds
    )
    estimate = requested_resource_cost(
        PARTITION_RESOURCES, partition_seconds + STARTUP_ALLOWANCE_SECONDS
    ) + 6 * requested_resource_cost(
        PREPARATION_RESOURCES,
        PREPARATION_RESOURCES.timeout_seconds + STARTUP_ALLOWANCE_SECONDS,
    )
    if estimate > budget_usd:
        raise ValueError(
            f"Pilot requested-resource estimate ${estimate:.2f} exceeds "
            f"approved budget ${budget_usd:.2f}; do not dispatch"
        )
    return estimate
