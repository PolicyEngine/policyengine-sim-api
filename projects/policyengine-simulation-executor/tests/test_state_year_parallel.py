"""Credential-free checks of bounded dispatch and task identity."""

import threading
import time

import pytest
from policyengine.provenance.dataset_materialization import MaterializedDataset
from policyengine.tax_benefit_models.us.state_preparation import (
    EntityCounts,
    USPreparationIdentity,
    USStatePartition,
    USStateYearArtifact,
)

from policyengine_simulation_executor.precompute_benchmark.parallel import (
    PreparedTaskResult,
    StateYearTask,
    USStateYearPlan,
    prepare_state_years_parallel,
)
from policyengine_simulation_executor.precompute_benchmark.profiling import (
    BenchmarkIdentity,
    Measurement,
)


def plan(tmp_path, count=4):
    source = MaterializedDataset(
        data_package_name="fixture",
        repo_type="dataset",
        revision="fixture",
        source_uri="fixture://source",
        sha256="a" * 64,
        path=tmp_path / "source.h5",
    )
    partition = USStatePartition(
        parent=source,
        state_code="CA",
        state_fips=6,
        source_year=2024,
        path=tmp_path / "ca.h5",
        sha256="b" * 64,
        bytes=1,
        counts=EntityCounts(
            **dict.fromkeys(
                (
                    "person",
                    "household",
                    "tax_unit",
                    "spm_unit",
                    "family",
                    "marital_unit",
                ),
                1,
            )
        ),
    )
    return USStateYearPlan(
        tasks=tuple(
            StateYearTask(partition=partition, year=2025 + index)
            for index in range(count)
        )
    )


def result_for(task):
    return PreparedTaskResult(
        artifact=USStateYearArtifact(
            partition=task.partition,
            year=task.year,
            path=task.partition.path,
            sha256="c" * 64,
            bytes=1,
            counts=task.partition.counts,
            identity=USPreparationIdentity(
                package_versions={"fixture": "1"}, code_sha256="d" * 64
            ),
        ),
        measurement=Measurement(
            identity=BenchmarkIdentity(
                code_revision="fixture",
                package_versions={"fixture": "1"},
                source_sha256="a" * 64,
            ),
            status="completed",
            error=None,
            started_unix_seconds=time.time(),
            elapsed_seconds=0.1,
            cpu_seconds=0.01,
            requested_cpu=1,
            requested_memory_mib=128,
            sample_count=2,
            initial_rss_bytes=1,
            sampled_peak_rss_bytes=2,
            process_high_water_rss_bytes=2,
            container_memory_source=None,
            container_memory_unavailable_reason="fixture",
            sampled_peak_container_bytes=None,
            phases=(),
            input_bytes=1,
            output_bytes=1,
        ),
    )


class Worker:
    def __init__(self, *, wrong_year=False, fail=False):
        self.lock = threading.Lock()
        self.active = self.maximum = self.calls = 0
        self.wrong_year = wrong_year
        self.fail = fail

    def spawn(self, task):
        worker = self
        with self.lock:
            self.calls += 1

        class Call:
            def result(self):
                with worker.lock:
                    worker.active += 1
                    worker.maximum = max(worker.maximum, worker.active)
                try:
                    time.sleep(0.05)
                    if worker.fail:
                        raise RuntimeError("fixture worker failure")
                    return result_for(
                        task.model_copy(update={"year": 1900})
                        if worker.wrong_year
                        else task
                    )
                finally:
                    with worker.lock:
                        worker.active -= 1

        return Call()


def test_two_workers_overlap_without_exceeding_limit(tmp_path):
    worker = Worker()
    output = prepare_state_years_parallel(plan(tmp_path), worker, max_workers=2)
    assert worker.maximum == 2
    assert [result.artifact.year for result in output.tasks] == [2025, 2026, 2027, 2028]


@pytest.mark.parametrize("workers", [0, 11, True])
def test_invalid_concurrency_is_rejected(tmp_path, workers):
    with pytest.raises(ValueError, match="max_workers"):
        prepare_state_years_parallel(plan(tmp_path), Worker(), max_workers=workers)


def test_mismatched_task_response_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="different preparation task"):
        prepare_state_years_parallel(
            plan(tmp_path), Worker(wrong_year=True), max_workers=2
        )


def test_failure_does_not_retry_or_submit_remaining_tasks(tmp_path):
    worker = Worker(fail=True)
    with pytest.raises(RuntimeError, match="fixture worker failure"):
        prepare_state_years_parallel(plan(tmp_path, count=20), worker, max_workers=2)
    assert worker.calls == 2


def test_duplicate_state_year_is_rejected(tmp_path):
    task = plan(tmp_path).tasks[0]
    with pytest.raises(ValueError, match="unique"):
        USStateYearPlan(tasks=(task, task))
