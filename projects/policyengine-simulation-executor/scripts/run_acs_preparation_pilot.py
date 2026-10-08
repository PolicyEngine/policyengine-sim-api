"""Run only the approved full-source partition and six CA/UT preparation jobs.

Testing environment only. No production precompute, manifest publication,
credentials, deployments, or national reference builds. Retain the unique
volume for later approved benchmarks so the source is not downloaded again.
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import subprocess
import time
from pathlib import Path

import modal
from policyengine.provenance.manifest import get_release_manifest
from pydantic import JsonValue, TypeAdapter

from policyengine_simulation_executor.precompute_benchmark.modal_helpers import (
    build_benchmark_image,
    preserve_evidence,
)
from policyengine_simulation_executor.precompute_benchmark.parallel import (
    ModalStateYearWorker,
    prepare_state_years_parallel,
)
from policyengine_simulation_executor.precompute_benchmark.pilot import (
    DATASET_NAME,
    PARTITION_RESOURCES,
    PREPARATION_RESOURCES,
    PilotPartitionResult,
    build_pilot_plan,
    requested_resource_cost,
    require_pilot_budget,
)


def run(
    analysis_repo: Path, output_dir: Path, suffix: str, *, budget_usd: float = 20
) -> None:
    """Execute exactly one partition plus six jobs, then end the application."""
    if not re.fullmatch("[a-z0-9]{6}", suffix):
        raise ValueError("Modal names require one six-character lowercase suffix")
    reserved_estimate = require_pilot_budget(budget_usd)
    reference = get_release_manifest("us").datasets[DATASET_NAME]
    expected_sha256 = reference.sha256
    if expected_sha256 is None or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ValueError("The installed bundle must certify the ACS source SHA-256")
    output_dir.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    code_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    image = build_benchmark_image(analysis_repo, project)
    app = modal.App(f"policyengine-acs-preparation-pilot-{suffix}")
    volume_name = f"policyengine-acs-preparation-data-{suffix}"
    modal.Volume.objects.create(volume_name, environment_name="testing")
    volume = modal.Volume.from_name(volume_name, environment_name="testing")
    resources: dict[str, JsonValue] = {
        "environment": "testing",
        "app_name": app.name,
        "volume_name": volume_name,
        "suffix": suffix,
        "code_revision": code_revision,
        "source_sha256": expected_sha256,
        "source_reference": reference.model_dump(mode="json"),
        "partition_resources": PARTITION_RESOURCES.model_dump(mode="json"),
        "preparation_resources": PREPARATION_RESOURCES.model_dump(mode="json"),
        "maximum_preparation_workers": 2,
        "budget_usd": budget_usd,
        "reserved_requested_resource_estimate_usd": reserved_estimate,
    }

    def save_resources() -> None:
        (output_dir / "resources.json").write_text(
            TypeAdapter(dict[str, JsonValue]).dump_json(resources, indent=2).decode()
        )

    save_resources()

    @app.function(
        name=f"partition_certified_acs_{suffix}",
        serialized=True,
        image=image,
        cpu=(PARTITION_RESOURCES.cpu, PARTITION_RESOURCES.cpu),
        memory=(PARTITION_RESOURCES.memory_mib, PARTITION_RESOURCES.memory_mib),
        volumes={"/benchmark": volume},
        timeout=PARTITION_RESOURCES.timeout_seconds,
        retries=0,
        max_containers=1,
        single_use_containers=True,
        scaledown_window=2,
    )
    def partition_source() -> str:
        from policyengine_simulation_executor.precompute_benchmark.workers import (
            full_partition_impl,
        )

        try:
            return full_partition_impl(expected_sha256, code_revision).model_dump_json()
        finally:
            volume.commit()

    @app.function(
        name=f"prepare_acs_state_year_{suffix}",
        serialized=True,
        image=image,
        cpu=(PREPARATION_RESOURCES.cpu, PREPARATION_RESOURCES.cpu),
        memory=(PREPARATION_RESOURCES.memory_mib, PREPARATION_RESOURCES.memory_mib),
        volumes={"/benchmark": volume},
        timeout=PREPARATION_RESOURCES.timeout_seconds,
        retries=0,
        max_containers=2,
        single_use_containers=True,
        scaledown_window=2,
    )
    def prepare_state_year(payload: str) -> str:
        from policyengine_simulation_executor.precompute_benchmark.parallel import (
            StateYearTask,
        )
        from policyengine_simulation_executor.precompute_benchmark.workers import (
            prepare_impl,
        )

        volume.reload()
        task = StateYearTask.model_validate_json(payload)
        try:
            # No artificial holds or readiness synchronization on real data.
            return prepare_impl(
                task, code_revision, resources=PREPARATION_RESOURCES
            ).model_dump_json()
        finally:
            volume.commit()

    with preserve_evidence(volume, output_dir):
        with modal.enable_output(), app.run(environment_name="testing"):
            resources["app_id"] = app.app_id
            save_resources()
            started = time.perf_counter()
            partition = PilotPartitionResult.model_validate_json(
                partition_source.remote()
            )
            partition_interval = time.perf_counter() - started
            (output_dir / "partition-result.json").write_text(
                partition.model_dump_json(indent=2)
            )
            # Recheck the six full-timeout reservations before any preparation.
            require_pilot_budget(
                budget_usd, partition_elapsed_seconds=partition_interval
            )
            started = time.perf_counter()
            result = prepare_state_years_parallel(
                build_pilot_plan(partition.manifest),
                ModalStateYearWorker(prepare_state_year),
                max_workers=2,
            )
            preparation_interval = time.perf_counter() - started
            (output_dir / "prepared-results.json").write_text(
                result.model_dump_json(indent=2)
            )
            summary: dict[str, JsonValue] = {
                "status": "completed",
                "partition_invocation_seconds": partition_interval,
                "parallel_preparation_interval_seconds": preparation_interval,
                "prepared_state_year_count": len(result.tasks),
                "requested_resource_estimate_excluding_image_build_usd": (
                    requested_resource_cost(PARTITION_RESOURCES, partition_interval)
                    + 2
                    * requested_resource_cost(
                        PREPARATION_RESOURCES, preparation_interval
                    )
                ),
                "not_an_invoice_or_enforced_billing_ceiling": True,
            }
            (output_dir / "pilot-summary.json").write_text(
                TypeAdapter(dict[str, JsonValue]).dump_json(summary, indent=2).decode()
            )
    print(f"Pilot completed. Evidence: {output_dir}; retained volume: {volume_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budget-usd", type=float, default=20)
    parser.add_argument(
        "--suffix",
        default="".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
        ),
    )
    args = parser.parse_args()
    run(args.analysis_repo, args.output_dir, args.suffix, budget_usd=args.budget_usd)
