"""Modal image and evidence handling shared by explicit testing benchmarks."""

from pathlib import Path

import modal


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
