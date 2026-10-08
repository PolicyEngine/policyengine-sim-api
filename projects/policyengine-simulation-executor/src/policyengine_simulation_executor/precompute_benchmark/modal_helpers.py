"""Modal image and evidence handling shared by explicit testing benchmarks."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import modal

from .profiling import StrictModel


def build_benchmark_image(analysis_repo: Path, project: Path) -> modal.Image:
    """Use the existing frozen image dependencies and the local prototype code."""
    return (
        modal.Image.debian_slim(python_version="3.13")
        .uv_sync(
            str(project),
            frozen=True,
            extra_options="--only-group modal-simulation-image",
        )
        .add_local_dir(
            analysis_repo / "src/policyengine",
            "/opt/prototype/policyengine",
            copy=True,
            ignore=["**/__pycache__/**"],
        )
        # Namespace parent avoids importing HTTP request-serving modules.
        .add_local_dir(
            project / "src/policyengine_simulation_executor/precompute_benchmark",
            "/opt/prototype/policyengine_simulation_executor/precompute_benchmark",
            copy=True,
            ignore=["**/__pycache__/**"],
        )
        .env({"PYTHONPATH": "/opt/prototype", "POLICYENGINE_SKIP_COUNTRY_IMPORTS": "1"})
    )


def download_evidence(volume: modal.Volume, output_dir: Path) -> None:
    """Preserve JSON reports and raw samples, without downloading large HDFs."""
    for entry in volume.iterdir("/", recursive=True):
        if entry.path.endswith((".json", ".jsonl")):
            destination = output_dir / entry.path.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                for chunk in volume.read_file(entry.path):
                    output.write(chunk)


class FailureRecord(StrictModel):
    error_type: str
    error_summary: str


def _record_failure(output_dir: Path, filename: str, error: Exception) -> None:
    (output_dir / filename).write_text(
        FailureRecord(
            error_type=type(error).__name__, error_summary=str(error)[:2000]
        ).model_dump_json(indent=2)
        + "\n"
    )


@contextmanager
def preserve_evidence(volume: modal.Volume, output_dir: Path) -> Iterator[None]:
    """Attempt evidence download on failure, preserving the original error.

    No retries or compute submissions occur here. A successful execution must
    still fail if evidence retrieval fails; an already-failed execution keeps
    its original exception and separately records retrieval failure.
    """
    try:
        yield
    except Exception as error:
        # Deliberate workflow boundary: record any execution failure without
        # converting it into success or substituting calculation inputs.
        _record_failure(output_dir, "execution-failure.json", error)
        try:
            download_evidence(volume, output_dir)
        except (modal.exception.Error, OSError) as retrieval_error:
            _record_failure(
                output_dir, "evidence-download-failure.json", retrieval_error
            )
            print(
                f"Evidence retrieval failed separately: {retrieval_error}", flush=True
            )
        raise
    else:
        download_evidence(volume, output_dir)
