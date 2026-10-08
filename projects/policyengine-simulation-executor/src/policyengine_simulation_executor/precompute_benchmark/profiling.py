"""Measure a benchmark using a separate process, including GIL-held intervals.

RSS is process memory, not container memory. Linux cgroup readings are reported
separately when accessible. Samples are flushed as they arrive so abrupt worker
failure still leaves useful evidence on a mounted output volume.
"""

from __future__ import annotations

import multiprocessing
import os
import resource
import sys
import time
from contextlib import contextmanager
from importlib.metadata import version
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Iterator, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkIdentity(StrictModel):
    code_revision: str
    package_versions: dict[str, str]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemorySample(StrictModel):
    elapsed_seconds: float = Field(ge=0)
    rss_bytes: int = Field(gt=0)
    container_bytes: int | None = Field(default=None, ge=0)


class Phase(StrictModel):
    name: str
    elapsed_seconds: float = Field(ge=0)


class Measurement(StrictModel):
    identity: BenchmarkIdentity
    status: Literal["completed", "failed"]
    error: str | None
    started_unix_seconds: float
    elapsed_seconds: float = Field(gt=0)
    cpu_seconds: float = Field(ge=0)
    requested_cpu: float = Field(gt=0)
    requested_memory_mib: int = Field(gt=0)
    sample_interval_seconds: float = Field(default=0.1, ge=0.1, le=0.1)
    sample_count: int = Field(gt=0)
    initial_rss_bytes: int = Field(gt=0)
    sampled_peak_rss_bytes: int = Field(gt=0)
    process_high_water_rss_bytes: int = Field(gt=0)
    container_memory_source: str | None
    container_memory_unavailable_reason: str | None
    sampled_peak_container_bytes: int | None
    phases: tuple[Phase, ...]
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)


def installed_identity(code_revision: str, source_sha256: str) -> BenchmarkIdentity:
    """Record installed versions, rather than hard-coded model or bundle pins."""
    return BenchmarkIdentity(
        code_revision=code_revision,
        source_sha256=source_sha256,
        package_versions={
            name: version(name)
            for name in (
                "policyengine",
                "policyengine-us",
                "policyengine-core",
                "spm-calculator",
            )
        },
    )


def _container_memory_path() -> Path | None:
    for candidate in (
        Path("/sys/fs/cgroup/memory.current"),
        Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            int(candidate.read_text())
        except (OSError, ValueError):
            continue
        return candidate
    return None


def _monitor(pid: int, samples_path: str, stopped: Event, ready: Event) -> None:
    process = psutil.Process(pid)
    started = time.perf_counter()
    last_progress = started
    container_path = _container_memory_path()
    with Path(samples_path).open("w", encoding="utf-8") as output:
        while True:
            container_bytes = (
                int(container_path.read_text()) if container_path else None
            )
            sample = MemorySample(
                elapsed_seconds=time.perf_counter() - started,
                rss_bytes=process.memory_info().rss,
                container_bytes=container_bytes,
            )
            output.write(sample.model_dump_json() + "\n")
            output.flush()
            ready.set()
            if time.perf_counter() - last_progress >= 5:
                print("benchmark_memory " + sample.model_dump_json(), flush=True)
                last_progress = time.perf_counter()
            if stopped.wait(0.1):
                return


class Profiler:
    """Persist active memory sampling and final success/failure measurements."""

    def __init__(
        self,
        output_dir: Path,
        identity: BenchmarkIdentity,
        *,
        cpu: float,
        memory_mib: int,
        input_bytes: int = 0,
    ):
        self.output_dir = output_dir
        self.identity = identity
        self.cpu = cpu
        self.memory_mib = memory_mib
        self.input_bytes = input_bytes
        self.output_bytes = 0
        self.phases: list[Phase] = []
        context = multiprocessing.get_context("spawn")
        self._stopped = context.Event()
        self._ready = context.Event()
        self._process = context.Process(
            target=_monitor,
            args=(
                os.getpid(),
                str(output_dir / "samples.jsonl"),
                self._stopped,
                self._ready,
            ),
        )
        self.measurement: Measurement | None = None

    def __enter__(self) -> Profiler:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if any(self.output_dir.iterdir()):
            raise FileExistsError(
                f"Measurement directory is not empty: {self.output_dir}"
            )
        self._process.start()
        if not self._ready.wait(15):
            self._process.terminate()
            self._process.join()
            raise RuntimeError("Independent benchmark memory sampler did not start")
        self._started = time.perf_counter()
        self._started_unix = time.time()
        self._cpu_started = time.process_time()
        return self

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            phase = Phase(name=name, elapsed_seconds=time.perf_counter() - started)
            self.phases.append(phase)
            (self.output_dir / "phases.json").write_text(
                "[" + ",".join(item.model_dump_json() for item in self.phases) + "]\n"
            )
            print("benchmark_phase " + phase.model_dump_json(), flush=True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        elapsed = time.perf_counter() - self._started
        cpu = time.process_time() - self._cpu_started
        self._stopped.set()
        self._process.join(timeout=10)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join()
            raise RuntimeError("Independent memory sampler failed to stop")
        if self._process.exitcode != 0:
            raise RuntimeError("Independent memory sampler failed")
        samples = [
            MemorySample.model_validate_json(line)
            for line in (self.output_dir / "samples.jsonl").read_text().splitlines()
        ]
        if not samples:
            raise RuntimeError("Independent memory sampler produced no measurements")
        high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        high_water_bytes = int(
            high_water if sys.platform == "darwin" else high_water * 1024
        )
        sampled_peak = max(sample.rss_bytes for sample in samples)
        # Different kernel APIs can disagree slightly in page accounting.
        if high_water_bytes + 8 * 1024 * 1024 < sampled_peak:
            raise RuntimeError(
                "RSS samples exceed independent process high-water value"
            )
        container_values = [
            sample.container_bytes
            for sample in samples
            if sample.container_bytes is not None
        ]
        container_path = _container_memory_path()
        self.measurement = Measurement(
            identity=self.identity,
            status="failed" if exc else "completed",
            error=f"{type(exc).__name__}: {exc}"[:1000] if exc else None,
            started_unix_seconds=self._started_unix,
            elapsed_seconds=elapsed,
            cpu_seconds=cpu,
            requested_cpu=self.cpu,
            requested_memory_mib=self.memory_mib,
            sample_count=len(samples),
            initial_rss_bytes=samples[0].rss_bytes,
            sampled_peak_rss_bytes=sampled_peak,
            process_high_water_rss_bytes=high_water_bytes,
            container_memory_source=str(container_path) if container_path else None,
            container_memory_unavailable_reason=None
            if container_path
            else "No readable cgroup memory counter; RSS is not container usage",
            sampled_peak_container_bytes=max(container_values)
            if container_values
            else None,
            phases=tuple(self.phases),
            input_bytes=self.input_bytes,
            output_bytes=self.output_bytes,
        )
        (self.output_dir / "measurement.json").write_text(
            self.measurement.model_dump_json(indent=2) + "\n"
        )
        return False


def verify_allocation_probe(
    report: Measurement, *, allocated_bytes: int, held_seconds: float
) -> None:
    """Reject instrumentation that misses known memory growth or a timed wait."""
    if report.elapsed_seconds < held_seconds or report.sample_count < 3:
        raise RuntimeError("Memory sampler missed the timed allocation interval")
    if (
        report.sampled_peak_rss_bytes - report.initial_rss_bytes
        < allocated_bytes * 0.75
    ):
        raise RuntimeError("Memory sampler missed the touched allocation")
