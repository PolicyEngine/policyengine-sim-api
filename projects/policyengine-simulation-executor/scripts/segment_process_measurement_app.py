"""Measure region groups run as processes in one container against separate containers.

Project 14 task 1.5 · 3. A segmented national run gives each of the 20 region
groups its own 8-CPU, 32 GiB ``run_simulation_segment`` container, and the
calculation in each runs in one process. This app runs the same groups both
ways and records wall time, CPU seconds and memory:

* ``containers``: one fresh container per group, as production does.
* ``small-containers``: the same, with each container requesting 1 CPU and
  6 GiB instead of 8 CPUs and 32 GiB.
* ``fork-N``: the groups split over as few fresh containers as hold at most N
  groups each; each container runs one process per group, forked after the
  container's first process has imported the US model, so the processes
  share that memory until they write to it.
* ``forkserver-N``: the same split, with the processes forked from a server
  process that imports the model first and runs no other threads.
* ``spawn-N``: the same split, with each process importing the model itself.

The image is the deployed runtime image's layers (the same code path the image
smoke uses) plus the model import the deployed image runs at build time, and a
layer that builds the measurement year's dataset and each measured group's
current-law baseline, as the deployed image's artifact layer provides them. So
each group computes its reform and loads its baseline, as production
segments do. Nothing here initialises telemetry, and no function reads Google
Cloud credentials or a database.

Usage, from projects/policyengine-simulation-executor::

    uv run modal run --env=testing scripts/segment_process_measurement_app.py
        --arms containers,fork-10 --group-count 20 --output results.json

(one command, written here on two lines).
"""

from __future__ import annotations

import json
import os
import pickle
import resource
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import modal
from policyengine_simulation_executor.national_partition import (
    US_NATIONAL_REGION_GROUPS,
)
from src.modal._image_setup import snapshot_models
from src.modal.app import build_runtime_simulation_image, hf_secret
from src.modal.static_runtime_files import add_static_runtime_files

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime

MEASUREMENT_YEAR = 2026
# Every group of the national partition: the containers arm is then a whole
# segmented national run.
MEASURED_GROUP_LIMIT = len(US_NATIONAL_REGION_GROUPS)
# A typical national reform: raise the Child Tax Credit's base amount.
MEASUREMENT_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3000}
}
CONTAINER_CPU = 8.0
CONTAINER_MEMORY_MEBIBYTES = 32768
# The small-containers arm: one group needs one CPU and under 3 GB.
SMALL_CONTAINER_CPU = 1.0
SMALL_CONTAINER_MEMORY_MEBIBYTES = 6144
SAMPLE_INTERVAL_SECONDS = 1.0
# Modal's list prices for Functions: per physical core second and per GiB
# second (modal.com/pricing, read 2026-11-24). Modal bills the larger of the
# request and the use, so these give the cost of a container that stays
# within its request.
CORE_SECOND_DOLLARS = 0.0000131
GIBIBYTE_SECOND_DOLLARS = 0.00000222


def container_second_dollars(cpu: float, memory_mebibytes: int) -> float:
    """Return the list price of one second of a container with this request."""
    return cpu * CORE_SECOND_DOLLARS + memory_mebibytes / 1024 * GIBIBYTE_SECOND_DOLLARS


CONTAINER_SECOND_DOLLARS = container_second_dollars(
    CONTAINER_CPU, CONTAINER_MEMORY_MEBIBYTES
)
SMALL_CONTAINER_SECOND_DOLLARS = container_second_dollars(
    SMALL_CONTAINER_CPU, SMALL_CONTAINER_MEMORY_MEBIBYTES
)
# Read once, when this module is imported: in a container that is when the
# container's Python process starts.
MODULE_IMPORTED_AT = time.time()

app = modal.App("policyengine-simulation-segment-process-measurement")

_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[1]) if modal.is_local() else "."


def measured_groups(group_count: int) -> list[list[str]]:
    """Return the first ``group_count`` groups of the national partition."""
    return [list(group) for group in US_NATIONAL_REGION_GROUPS[:group_count]]


def bake_measurement_inputs() -> None:
    """Build the measurement year's dataset and each measured group's baseline.

    Runs once, at image build time. The deployed image downloads both from
    the artifact store instead; computing them here with the executor's own
    code writes the same files to the same data folder.
    """
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )
    from policyengine_simulation_executor.precompute_planning import (  # noqa: PLC0415  # image build step: the model loads inside the build container
        cohort_resolution,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # image build step: the model loads inside the build container
        _build_simulation,
        _load_dataset,
        _resolve_dataset_selection,
    )

    for group in measured_groups(MEASURED_GROUP_LIMIT):
        started = time.monotonic()
        params, country_module, resolution = cohort_resolution(MEASUREMENT_YEAR, group)
        selection = _resolve_dataset_selection(params, region_resolution=resolution)
        dataset = _load_dataset(
            params, country_module=country_module, selection=selection
        )
        baseline = _build_simulation(
            params,
            dataset=dataset,
            dataset_selection=selection,
            policy=None,
            region=resolution,
        )
        configure_budgetary_impact_variables(baseline, baseline)
        baseline.ensure()
        outcome = getattr(baseline, "artifact_outcome", None)
        print(
            f"Baked the {'+'.join(group)} baseline ({outcome}) in "
            f"{time.monotonic() - started:.1f}s"
        )


measurement_image = (
    add_static_runtime_files(
        build_runtime_simulation_image().add_local_python_source(
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
            copy=True,
        ),
        uv_project_dir=_UV_PROJECT_DIR,
    )
    .run_function(snapshot_models)
    .run_function(
        bake_measurement_inputs,
        cpu=CONTAINER_CPU,
        memory=CONTAINER_MEMORY_MEBIBYTES,
        timeout=3600,
    )
)


class LocalAttributesRuntime:
    """An observability runtime that keeps run attributes in memory and exports nothing.

    No measurement reaches telemetry; the attributes the run sets, such as
    ``baseline_artifact``, stay readable here.
    """

    def __init__(self) -> None:
        """Start with no attributes."""
        self.attributes: dict[str, object] = {}

    def span(self, *_args: object, **_kwargs: object) -> nullcontext[None]:
        """Return an empty context."""
        return nullcontext()

    def operation(self, *_args: object, **_kwargs: object) -> nullcontext[None]:
        """Return an empty context."""
        return nullcontext()

    def set_context(self, **attributes: object) -> None:
        """Keep the attributes."""
        self.attributes.update(attributes)

    def event(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def record_exception(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def capture_context(self) -> dict[str, str]:
        """Return an empty context."""
        return {}


def group_request(group: list[str]) -> dict[str, Any]:
    """Return the request a segmented national parent sends one region group."""
    return {
        "country": "us",
        "scope": "macro",
        "time_period": str(MEASUREMENT_YEAR),
        "region_group": list(group),
        "reform": MEASUREMENT_REFORM,
        "_emit_microdata": True,
    }


def _process_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _peak_resident_mebibytes() -> float:
    # ru_maxrss is in kibibytes on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def run_group_measured(group: list[str]) -> dict[str, Any]:
    """Run one region group as ``run_simulation_segment`` does, and time each step.

    The result itself is pickled to measure what a worker would send back,
    and only its size is returned.
    """
    started_at = time.time()
    cpu_at_start = _process_cpu_seconds()
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # timed: in a fresh process this import loads the executor
        _country_module,
        run_simulation_impl,
    )

    _ = _country_module("us").model
    model_ready_at = time.time()
    runtime = LocalAttributesRuntime()
    result = run_simulation_impl(
        group_request(group), runtime=cast("ObservabilityRuntime", runtime)
    )
    calculated_at = time.time()
    result_bytes = len(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))
    finished_at = time.time()
    return {
        "group": "+".join(group),
        "process_id": os.getpid(),
        "started_at": started_at,
        "model_setup_seconds": round(model_ready_at - started_at, 2),
        "calculation_seconds": round(calculated_at - model_ready_at, 2),
        "result_pickle_seconds": round(finished_at - calculated_at, 2),
        "total_seconds": round(finished_at - started_at, 2),
        "cpu_seconds": round(_process_cpu_seconds() - cpu_at_start, 2),
        "process_peak_resident_mebibytes": round(_peak_resident_mebibytes(), 1),
        "result_mebibytes": round(result_bytes / 1024 / 1024, 1),
        "baseline_artifact": runtime.attributes.get("baseline_artifact"),
    }


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _all_process_cpu_seconds() -> float:
    """Sum CPU seconds over every process this container can see.

    Each process counts its own user and system time plus that of its
    children it has waited for, so a finished pool worker's time moves to its
    parent instead of vanishing from the sum.
    """
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    total_ticks = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        stat = _read_text(f"/proc/{entry.name}/stat")
        if stat is None:
            continue
        # The fields after the parenthesised command name, counted from the
        # state field: utime, stime, cutime and cstime are the 12th to 15th.
        fields_after_name = stat.rsplit(")", 1)[1].split()
        try:
            total_ticks += sum(int(field) for field in fields_after_name[11:15])
        except (IndexError, ValueError):
            continue
    return total_ticks / ticks_per_second


def _all_process_resident_mebibytes() -> float:
    """Sum resident memory over every process; forked pages count once per process."""
    total_kibibytes = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        status = _read_text(f"/proc/{entry.name}/status")
        for status_line in (status or "").splitlines():
            if status_line.startswith("VmRSS:") and status_line.split()[1].isdigit():
                total_kibibytes += int(status_line.split()[1])
    return total_kibibytes / 1024


def _all_process_proportional_mebibytes() -> float | None:
    """Sum proportional set size over every process, so shared pages count once.

    None when the kernel offers no ``smaps_rollup``.
    """
    total_kibibytes = 0
    found_any = False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        rollup = _read_text(f"/proc/{entry.name}/smaps_rollup")
        for rollup_line in (rollup or "").splitlines():
            if rollup_line.startswith("Pss:") and rollup_line.split()[1].isdigit():
                total_kibibytes += int(rollup_line.split()[1])
                found_any = True
    return total_kibibytes / 1024 if found_any else None


def _container_memory_mebibytes() -> float | None:
    """Return the container's memory use from its cgroup, when the cgroup shows it."""
    for path in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        value = _read_text(path)
        if value and value.isdigit():
            return int(value) / 1024 / 1024
    return None


class ContainerSampler:
    """Sample container-wide CPU use and memory once a second on a thread."""

    def __init__(self) -> None:
        """Prepare an empty sample list; ``start`` begins sampling."""
        self.samples: list[dict[str, float | None]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started_at = time.monotonic()

    def start(self) -> ContainerSampler:
        """Start sampling and return this sampler."""
        self._thread.start()
        return self

    def stop(self) -> list[dict[str, float | None]]:
        """Stop sampling and return the samples."""
        self._stop.set()
        self._thread.join(timeout=5)
        return self.samples

    def _run(self) -> None:
        previous_cpu = _all_process_cpu_seconds()
        previous_time = time.monotonic()
        while not self._stop.wait(SAMPLE_INTERVAL_SECONDS):
            now = time.monotonic()
            cpu = _all_process_cpu_seconds()
            self.samples.append(
                {
                    "elapsed_seconds": round(now - self._started_at, 1),
                    "cores_busy": round(
                        max(cpu - previous_cpu, 0.0) / (now - previous_time), 2
                    ),
                    "resident_mebibytes_summed": round(
                        _all_process_resident_mebibytes(), 1
                    ),
                    "proportional_mebibytes_summed": (
                        _all_process_proportional_mebibytes()
                    ),
                    "container_memory_mebibytes": _container_memory_mebibytes(),
                }
            )
            previous_cpu, previous_time = cpu, now


def summarize_samples(
    samples: list[dict[str, float | None]],
) -> dict[str, float | None]:
    """Return the peak and mean of the sampled CPU use and memory."""

    def values(key: str) -> list[float]:
        return [value for sample in samples if (value := sample.get(key)) is not None]

    busy_cores = values("cores_busy")
    return {
        "mean_cores_busy": round(sum(busy_cores) / len(busy_cores), 2)
        if busy_cores
        else None,
        "peak_cores_busy": max(busy_cores, default=None),
        "peak_resident_mebibytes_summed": max(
            values("resident_mebibytes_summed"), default=None
        ),
        "peak_proportional_mebibytes_summed": max(
            values("proportional_mebibytes_summed"), default=None
        ),
        "peak_container_memory_mebibytes": max(
            values("container_memory_mebibytes"), default=None
        ),
    }


def _schedulable_cpu_count() -> int | None:
    """Return how many CPUs this process may run on, where the platform says."""
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity is not None else None


def container_description() -> dict[str, Any]:
    """Describe the CPUs and memory limits this container sees."""
    return {
        "modal_task_id": os.environ.get("MODAL_TASK_ID"),
        "os_cpu_count": os.cpu_count(),
        "schedulable_cpus": _schedulable_cpu_count(),
        "cgroup_cpu_max": _read_text("/sys/fs/cgroup/cpu.max"),
        "cgroup_memory_max": _read_text("/sys/fs/cgroup/memory.max"),
        "module_imported_at": MODULE_IMPORTED_AT,
    }


@app.function(
    image=measurement_image,
    cpu=CONTAINER_CPU,
    memory=CONTAINER_MEMORY_MEBIBYTES,
    timeout=3600,
    retries=0,
    max_containers=MEASURED_GROUP_LIMIT,
    # Each call gets a fresh container, which stops when the call returns,
    # so every group pays the setup a new production segment container pays.
    single_use_containers=True,
    secrets=[hf_secret],
)
def run_group_in_own_container(group: list[str]) -> dict[str, Any]:
    """Run one group in this container, as a production segment worker does."""
    return _run_one_group_in_this_container(group)


@app.function(
    image=measurement_image,
    cpu=SMALL_CONTAINER_CPU,
    memory=SMALL_CONTAINER_MEMORY_MEBIBYTES,
    timeout=3600,
    retries=0,
    max_containers=MEASURED_GROUP_LIMIT,
    single_use_containers=True,
    secrets=[hf_secret],
)
def run_group_in_small_container(group: list[str]) -> dict[str, Any]:
    """Run one group as ``run_group_in_own_container`` does, with 1 CPU and 6 GiB."""
    return _run_one_group_in_this_container(group)


def _run_one_group_in_this_container(group: list[str]) -> dict[str, Any]:
    entered_at = time.time()
    sampler = ContainerSampler().start()
    measurement = run_group_measured(group)
    samples = sampler.stop()
    return {
        **container_description(),
        "entered_at": entered_at,
        "returned_at": time.time(),
        "groups": [measurement],
        "samples": samples,
        "sample_summary": summarize_samples(samples),
    }


def _preload_model() -> None:
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # timed: the fork arm loads the model before forking
        _country_module,
    )

    _ = _country_module("us").model


@app.function(
    image=measurement_image,
    cpu=CONTAINER_CPU,
    memory=CONTAINER_MEMORY_MEBIBYTES,
    timeout=3600,
    retries=0,
    max_containers=MEASURED_GROUP_LIMIT,
    single_use_containers=True,
    secrets=[hf_secret],
)
def run_groups_in_processes(
    groups: list[list[str]], start_method: str
) -> dict[str, Any]:
    """Run the groups in this one container, one process per group.

    ``start_method`` is ``fork`` (children forked from this process after it
    loads the model), ``forkserver`` (children forked from a server process
    that loads the model first and runs no other threads) or ``spawn`` (each
    child imports the model itself).
    """
    import multiprocessing  # noqa: PLC0415  # used only in the process arms

    entered_at = time.time()
    sampler = ContainerSampler().start()
    preload_seconds = 0.0
    if start_method == "fork":
        preload_started = time.monotonic()
        _preload_model()
        preload_seconds = time.monotonic() - preload_started
    context = multiprocessing.get_context(start_method)
    if start_method == "forkserver":
        context.set_forkserver_preload(
            [
                "policyengine.tax_benefit_models.us",
                "policyengine_simulation_executor.simulation_runtime",
                run_group_measured.__module__,
            ]
        )
    pool_started = time.monotonic()
    with context.Pool(processes=len(groups)) as pool:
        measurements = list(pool.imap_unordered(run_group_measured, groups))
    pool_seconds = time.monotonic() - pool_started
    samples = sampler.stop()
    return {
        **container_description(),
        "entered_at": entered_at,
        "returned_at": time.time(),
        "start_method": start_method,
        "process_count": len(groups),
        "parent_preload_seconds": round(preload_seconds, 2),
        "pool_seconds": round(pool_seconds, 2),
        "groups": measurements,
        "samples": samples,
        "sample_summary": summarize_samples(samples),
    }


def _parse_process_arm(arm: str) -> tuple[str, int]:
    start_method, _, count = arm.partition("-")
    if start_method not in {"fork", "forkserver", "spawn"} or not count.isdigit():
        msg = (
            f"Unknown arm {arm!r}: use containers, small-containers, fork-N, "
            "forkserver-N or spawn-N"
        )
        raise ValueError(msg)
    return start_method, int(count)


def _run_containers_arm(groups: list[list[str]], arm: str) -> dict[str, Any]:
    if arm == "small-containers":
        function, dollars = run_group_in_small_container, SMALL_CONTAINER_SECOND_DOLLARS
    else:
        function, dollars = run_group_in_own_container, CONTAINER_SECOND_DOLLARS
    started_at = time.time()
    calls = [(time.time(), function.spawn(group)) for group in groups]
    containers = []
    for spawned_at, call in calls:
        container = call.get()
        container["caller_spawned_at"] = spawned_at
        container["caller_received_at"] = time.time()
        containers.append(container)
    return {
        "arm": arm,
        "container_second_dollars": dollars,
        "caller_started_at": started_at,
        "caller_seconds": round(time.time() - started_at, 2),
        "containers": containers,
    }


def split_groups(
    groups: list[list[str]], groups_per_container: int
) -> list[list[list[str]]]:
    """Deal the groups over as few containers as hold ``groups_per_container`` each.

    Container sizes differ by at most one group.
    """
    container_count = -(-len(groups) // groups_per_container)
    return [groups[index::container_count] for index in range(container_count)]


def _run_process_arm(arm: str, groups: list[list[str]]) -> dict[str, Any]:
    start_method, groups_per_container = _parse_process_arm(arm)
    started_at = time.time()
    calls = [
        run_groups_in_processes.spawn(container_groups, start_method)
        for container_groups in split_groups(groups, groups_per_container)
    ]
    containers = [call.get() for call in calls]
    return {
        "arm": arm,
        "container_second_dollars": CONTAINER_SECOND_DOLLARS,
        "caller_started_at": started_at,
        "caller_seconds": round(time.time() - started_at, 2),
        "containers": containers,
    }


def print_summary(arm_results: list[dict[str, Any]]) -> None:
    """Print one row per arm: wall time, container time, cost, CPU seconds and memory.

    Container seconds run from the app module's import in each container to
    the call's return, so they leave out Modal's container start before
    Python runs.
    """
    print(
        "arm | containers | caller s | container s (sum) | list price $ | "
        "calc s (median) | group cpu s (sum) | peak memory MiB (max)"
    )
    for arm_result in arm_results:
        containers = arm_result["containers"]
        container_seconds = sum(
            container["returned_at"] - container["module_imported_at"]
            for container in containers
        )
        calculation_seconds = sorted(
            group["calculation_seconds"]
            for container in containers
            for group in container["groups"]
        )
        cpu_seconds = sum(
            group["cpu_seconds"]
            for container in containers
            for group in container["groups"]
        )
        peak_memory = max(
            (
                container["sample_summary"]["peak_container_memory_mebibytes"]
                or container["sample_summary"]["peak_resident_mebibytes_summed"]
                or 0
            )
            for container in containers
        )
        median = calculation_seconds[len(calculation_seconds) // 2]
        print(
            f"{arm_result['arm']} | {len(containers)} | "
            f"{arm_result['caller_seconds']:.0f} | {container_seconds:.0f} | "
            f"{container_seconds * arm_result['container_second_dollars']:.2f} | "
            f"{median:.0f} | "
            f"{cpu_seconds:.0f} | {peak_memory:.0f}"
        )


@app.local_entrypoint()
def main(
    arms: str = "containers,fork-10",
    group_count: int = MEASURED_GROUP_LIMIT,
    output: str = "segment-process-measurement.json",
) -> None:
    """Run each arm in turn on the same groups; write the measurements to ``output``."""
    if not 1 <= group_count <= MEASURED_GROUP_LIMIT:
        msg = (
            f"group_count must be 1 to {MEASURED_GROUP_LIMIT}: "
            "only those groups' baselines are baked"
        )
        raise ValueError(msg)
    groups = measured_groups(group_count)
    arm_results = []
    for arm in arms.split(","):
        print(f"Running arm {arm} on {len(groups)} groups")
        if arm in {"containers", "small-containers"}:
            arm_results.append(_run_containers_arm(groups, arm))
        else:
            arm_results.append(_run_process_arm(arm, groups))
        Path(output).write_text(json.dumps(arm_results, indent=2))
    print_summary(arm_results)
