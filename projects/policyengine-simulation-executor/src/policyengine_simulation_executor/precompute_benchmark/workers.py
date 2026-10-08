"""Implementations used only by the explicit Modal testing benchmark."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from policyengine.provenance.dataset_materialization import MaterializedDataset
from policyengine.tax_benefit_models.us.state_preparation import (
    USPartitionManifest,
    partition_certified_us_source,
    prepare_us_state_year,
)

from .parallel import PreparedTaskResult, StateYearTask
from .profiling import (
    Measurement,
    Profiler,
    installed_identity,
    verify_allocation_probe,
)

ROOT = Path("/benchmark")


def probe_impl(
    code_revision: str, source_sha256: str, *, fail: bool = False
) -> Measurement:
    profiler = Profiler(
        ROOT / ("failure-probe" if fail else "allocation-probe"),
        installed_identity(code_revision, source_sha256),
        cpu=1,
        memory_mib=2048,
    )
    try:
        with profiler:
            with profiler.phase("known-allocation-and-wait"):
                allocation = bytearray(64 * 1024 * 1024)
                time.sleep(0.6)
                if allocation[-1] != 0:
                    raise RuntimeError("Allocation probe was not initialized")
                if fail:
                    raise ValueError("Intentional benchmark failure probe")
    except ValueError:
        if not fail:
            raise
    report = profiler.measurement
    if report is None:
        raise RuntimeError("Probe did not record valid measurements")
    verify_allocation_probe(report, allocated_bytes=64 * 1024 * 1024, held_seconds=0.6)
    if report.status != ("failed" if fail else "completed"):
        raise RuntimeError("Probe completion status is incorrect")
    return report


def partition_impl(
    source: MaterializedDataset, code_revision: str
) -> tuple[USPartitionManifest, Measurement]:
    profiler = Profiler(
        ROOT / "partition-measurements",
        installed_identity(code_revision, source.sha256),
        cpu=2,
        memory_mib=4096,
        input_bytes=source.path.stat().st_size,
    )
    with profiler:
        with profiler.phase("hash-verify-load-partition-roundtrip"):
            manifest = partition_certified_us_source(source, ROOT / "states")
            profiler.output_bytes = sum(part.bytes for part in manifest.partitions)
            (ROOT / "partition-manifest.json").write_text(
                manifest.model_dump_json(indent=2)
            )
    if profiler.measurement is None:
        raise RuntimeError("Partition did not record valid measurements")
    return manifest, profiler.measurement


def prepare_impl(
    task: StateYearTask,
    code_revision: str,
    *,
    hold_seconds: float = 0,
    synchronize: Callable[[], None] | None = None,
) -> PreparedTaskResult:
    profiler = Profiler(
        ROOT / f"measurements-{task.partition.state_code.lower()}-{task.year}",
        installed_identity(code_revision, task.partition.parent.sha256),
        cpu=2,
        memory_mib=4096,
        input_bytes=task.partition.bytes,
    )
    with profiler:
        if synchronize is not None:
            with profiler.phase("testing-only-worker-barrier"):
                synchronize()
        if hold_seconds:
            # Testing-only controlled interval makes independent worker overlap
            # observable even when preparation of the tiny fixture takes <100 ms.
            with profiler.phase("testing-only-overlap-wait"):
                time.sleep(hold_seconds)
        with profiler.phase("verify-partition-prepare-year-roundtrip"):
            artifact = prepare_us_state_year(
                task.partition, task.year, ROOT / "prepared"
            )
            profiler.output_bytes = artifact.bytes
    if profiler.measurement is None:
        raise RuntimeError("Year preparation did not record valid measurements")
    return PreparedTaskResult(artifact=artifact, measurement=profiler.measurement)
