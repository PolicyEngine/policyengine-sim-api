"""Credential-free checks of the explicitly approved six-job ACS pilot."""

from pathlib import Path

import pytest
from policyengine.countries.us.data.states import US_STATE_FIPS
from policyengine.provenance.dataset_materialization import (
    DatasetSource,
    MaterializedDataset,
)
from policyengine.tax_benefit_models.us.state_preparation import (
    EntityCounts,
    USPartitionManifest,
    USStatePartition,
)

from policyengine_simulation_executor.precompute_benchmark.pilot import (
    PARTITION_RESOURCES,
    PREPARATION_RESOURCES,
    build_pilot_plan,
    requested_resource_cost,
    require_certified_pilot_source,
    require_pilot_budget,
)


def source() -> MaterializedDataset:
    return MaterializedDataset(
        data_package_name="fixture",
        repo_type="dataset",
        revision="fixture",
        source_uri="fixture://source",
        sha256="a" * 64,
        path=Path("/fixture/source.h5"),
    )


def test_plan_contains_only_approved_states_and_years():
    counts = EntityCounts(
        person=1, household=1, tax_unit=1, spm_unit=1, family=1, marital_unit=1
    )
    manifest = USPartitionManifest(
        source=source(),
        source_year=2024,
        counts=EntityCounts(**{key: 51 for key in EntityCounts.model_fields}),
        partitions=tuple(
            USStatePartition(
                parent=source(),
                state_code=code,
                state_fips=fips,
                source_year=2024,
                path=Path(f"/fixture/{code}.h5"),
                sha256="b" * 64,
                bytes=1,
                counts=counts,
            )
            for code, fips in US_STATE_FIPS.items()
        ),
    )
    plan = build_pilot_plan(manifest)
    assert [(task.partition.state_code, task.year) for task in plan.tasks] == [
        (state, year) for year in (2025, 2026, 2027) for state in ("CA", "UT")
    ]
    assert all(task.partition.parent == manifest.source for task in plan.tasks)


def test_budget_reserves_requested_resources_for_timeouts_and_startup():
    estimate = require_pilot_budget(20)
    # Published rates checked 2026-10-08: $0.0000131/core/s and
    # $0.00000222/GiB/s. One partition + six preparations, 3600 s each,
    # with a separate 300 s startup allowance per invocation.
    assert estimate == pytest.approx((1.400256 + 6 * 0.888768) * 3900 / 3600)
    assert requested_resource_cost(PARTITION_RESOURCES, 3600) == pytest.approx(1.400256)
    assert requested_resource_cost(PREPARATION_RESOURCES, 3600) == pytest.approx(
        0.888768
    )
    with pytest.raises(ValueError, match="budget"):
        require_pilot_budget(1)
    with pytest.raises(ValueError, match="budget"):
        require_pilot_budget(20, partition_elapsed_seconds=100_000)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), 20.01])
def test_budget_cannot_be_invalid_or_exceed_approval(budget):
    with pytest.raises(ValueError, match="budget"):
        require_pilot_budget(budget)


def test_certified_source_retains_its_bundle_identity():
    expected = source()
    selected = DatasetSource(
        source_uri=expected.source_uri,
        path=str(expected.path),
        bundle_dataset=expected,
    )
    assert require_certified_pilot_source(selected, "a" * 64) == expected


def test_unmanaged_or_wrong_bundle_source_is_rejected():
    with pytest.raises(ValueError, match="certified"):
        require_certified_pilot_source(
            DatasetSource(source_uri="fixture://source", path="/fixture/source.h5"),
            "a" * 64,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        require_certified_pilot_source(
            DatasetSource(
                source_uri=source().source_uri,
                path=str(source().path),
                bundle_dataset=source(),
            ),
            "b" * 64,
        )
