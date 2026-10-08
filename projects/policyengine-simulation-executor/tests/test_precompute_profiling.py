"""Small, credential-free checks of benchmark instrumentation."""

import json
import time

import pytest

from policyengine_simulation_executor.precompute_benchmark.profiling import (
    BenchmarkIdentity,
    Measurement,
    Profiler,
    verify_allocation_probe,
)


def identity():
    return BenchmarkIdentity(
        code_revision="fixture",
        package_versions={"fixture": "1"},
        source_sha256="a" * 64,
    )


def test_measures_touched_allocation_and_elapsed_time(tmp_path):
    with Profiler(tmp_path, identity(), cpu=1, memory_mib=1024) as profiler:
        with profiler.phase("allocation"):
            allocation = bytearray(64 * 1024 * 1024)
            time.sleep(0.6)
            assert allocation[-1] == 0
    report = Measurement.model_validate_json(
        (tmp_path / "measurement.json").read_text()
    )
    verify_allocation_probe(report, allocated_bytes=len(allocation), held_seconds=0.6)
    assert report.phases[0].name == "allocation"
    assert report.cpu_seconds >= 0
    assert report.status == "completed"
    assert report.sample_count >= 5


def test_failure_preserves_valid_samples_and_partial_report(tmp_path):
    with pytest.raises(ValueError, match="intentional"):
        with Profiler(tmp_path, identity(), cpu=1, memory_mib=1024):
            time.sleep(0.2)
            raise ValueError("intentional")
    report = Measurement.model_validate_json(
        (tmp_path / "measurement.json").read_text()
    )
    assert report.status == "failed"
    assert report.error == "ValueError: intentional"
    assert report.sample_count > 0
    assert all(
        json.loads(line)["rss_bytes"] > 0
        for line in (tmp_path / "samples.jsonl").read_text().splitlines()
    )


def test_invalid_instrumentation_is_rejected(tmp_path):
    with Profiler(tmp_path, identity(), cpu=1, memory_mib=1024):
        time.sleep(0.2)
    report = Measurement.model_validate_json(
        (tmp_path / "measurement.json").read_text()
    )
    invalid = report.model_copy(
        update={"sampled_peak_rss_bytes": report.initial_rss_bytes}
    )
    with pytest.raises(RuntimeError, match="allocation"):
        verify_allocation_probe(
            invalid, allocated_bytes=64 * 1024 * 1024, held_seconds=0.1
        )
