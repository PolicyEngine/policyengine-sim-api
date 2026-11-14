"""Which succeeded simulation run a new report reuses, if any.

A report reuses the newest run of a simulation with the same policy, dynamic,
catalog version, dataset, region and year, that succeeded on the same country
package and .py versions, whose output file is in the output bucket, and that
a succeeded report run with the same options used. A run any report run
refused to reuse is never offered again.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    COMPLETED_AT,
    CURRENT_LAW,
    OPTIONS,
    OUTPUT_BUCKET_PREFIX,
    VERSIONS,
    EconomyCatalog,
    ReportRunOutcome,
    RunShape,
    StoredRun,
    insert_economy_catalog,
    insert_report_run_naming,
    insert_reusable_run,
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    ReportRunStatus,
    ReusableRun,
    RunVersions,
    SimulationChoice,
    SimulationStatus,
    find_reusable_run,
    find_reusable_run_of,
)
from public_api_rows import insert_dataset, insert_dynamic, insert_policy, insert_region

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema


@dataclass(frozen=True)
class Request:
    """What a new report's simulation asks for, as the reuse search sees it."""

    choice: SimulationChoice = CURRENT_LAW
    year: int = 2026
    versions: RunVersions = VERSIONS
    inputs: dict[str, Any] = field(default_factory=lambda: dict(OPTIONS))
    excluded_run_id: UUID | None = None


def _find(
    connection: Connection, economy: EconomyCatalog, request: Request | None = None
) -> ReusableRun | None:
    request = request or Request()
    return find_reusable_run(
        connection,
        choice=request.choice,
        population=economy.population(year=request.year),
        versions=request.versions,
        inputs=request.inputs,
        output_uri_prefix=OUTPUT_BUCKET_PREFIX,
        excluded_run_id=request.excluded_run_id,
    )


def _reusable(run: StoredRun) -> ReusableRun:
    return ReusableRun(
        simulation_id=run.simulation_id, simulation_run_id=run.simulation_run_id
    )


def test_a_succeeded_run_a_report_used_with_the_same_options_is_reused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        dynamic_id = insert_dynamic(connection, economy.catalog)
        choice = SimulationChoice(policy_id=policy_id, dynamic_id=dynamic_id)
        reused = insert_reusable_run(connection, economy, RunShape(choice=choice))
        act_as_the_service(connection)

        found = _find(connection, economy, Request(choice=choice))

    assert found == _reusable(reused)


def _another_policy(connection: Connection, economy: EconomyCatalog) -> dict:
    return {
        "choice": SimulationChoice(insert_policy(connection, economy.catalog), None)
    }


def _another_dynamic(connection: Connection, economy: EconomyCatalog) -> dict:
    return {
        "choice": SimulationChoice(None, insert_dynamic(connection, economy.catalog))
    }


# Each makes the request differ from the stored run in one way.
DIFFERENCES: dict[str, Callable[[Connection, EconomyCatalog], dict[str, Any]]] = {
    "another policy": _another_policy,
    "another dynamic": _another_dynamic,
    "another year": lambda _connection, _economy: {"year": 2027},
    "another .py version": lambda _connection, _economy: {
        "versions": replace(VERSIONS, policyengine_version="6.18.0")
    },
    "another country package version": lambda _connection, _economy: {
        "versions": replace(VERSIONS, country_package_version="1.765.0")
    },
    "other options": lambda _connection, _economy: {"inputs": {"include_cliffs": True}},
}


@pytest.mark.parametrize("difference", sorted(DIFFERENCES))
def test_a_run_differing_from_the_request_is_not_reused(
    public_api_database_url, difference: str
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        insert_reusable_run(connection, economy)
        request = DIFFERENCES[difference](connection, economy)
        act_as_the_service(connection)

        assert _find(connection, economy, Request(**request)) is None


def test_a_run_of_another_region_or_dataset_is_not_reused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        insert_reusable_run(connection, economy)
        other_dataset_id = insert_dataset(connection, economy.catalog, "acs_2024")
        other_region_id = insert_region(
            connection,
            economy.catalog,
            "state/ca",
            default_dataset_id=economy.dataset_id,
            region_type="state",
        )
        act_as_the_service(connection)

        for other in (
            replace(economy, dataset_id=other_dataset_id),
            replace(economy, region_id=other_region_id),
        ):
            assert _find(connection, other) is None


@pytest.mark.parametrize(
    "status",
    [SimulationStatus.PENDING, SimulationStatus.RUNNING, SimulationStatus.FAILED],
)
def test_an_unfinished_or_failed_run_is_not_reused(
    public_api_database_url, status: SimulationStatus
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        insert_reusable_run(connection, economy, RunShape(status=status))
        act_as_the_service(connection)

        assert _find(connection, economy) is None


def test_a_forced_run_is_never_reused(public_api_database_url) -> None:
    """Answer 3: a forced re-run's run is never named by another report run."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        forced = insert_reusable_run(connection, economy, RunShape(forced=True))
        act_as_the_service(connection)

        by_definition = _find(connection, economy)
        by_own_simulation = find_reusable_run_of(
            connection,
            simulation_id=forced.simulation_id,
            population=economy.population(),
            versions=VERSIONS,
            inputs=OPTIONS,
            output_uri_prefix=OUTPUT_BUCKET_PREFIX,
        )

    assert by_definition is None
    assert by_own_simulation is None


def test_a_run_whose_file_is_outside_the_output_bucket_is_not_reused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        insert_reusable_run(
            connection,
            economy,
            RunShape(
                output_uri="gs://policyengine-stage12-staging-artifacts/runs/output.parquet"
            ),
        )
        # A bucket whose name starts with the output bucket's is another bucket.
        insert_reusable_run(
            connection,
            economy,
            RunShape(
                output_uri=OUTPUT_BUCKET_PREFIX.rstrip("/") + "-old/output.parquet"
            ),
        )
        act_as_the_service(connection)

        assert _find(connection, economy) is None


@pytest.mark.parametrize(
    "report_run_status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.FAILED],
)
def test_a_run_no_succeeded_report_run_used_is_not_reused(
    public_api_database_url, report_run_status: ReportRunStatus
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        population = economy.population()
        stored = insert_simulation_run(connection, population)
        other_side = insert_simulation_run(
            connection, population, RunShape(status=SimulationStatus.FAILED)
        )
        insert_report_run_naming(
            connection,
            economy,
            (stored, other_side),
            ReportRunOutcome(status=report_run_status),
        )
        # A run no report run names at all, such as a simulation's own.
        insert_simulation_run(connection, population)
        act_as_the_service(connection)

        assert _find(connection, economy) is None


@pytest.mark.parametrize("refused_as", ["baseline", "reform"])
def test_a_run_a_report_run_refused_to_reuse_is_not_reused_again(
    public_api_database_url, refused_as: str
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reused = insert_reusable_run(connection, economy)
        other_side = insert_simulation_run(
            connection, economy.population(), RunShape(status=SimulationStatus.FAILED)
        )
        baseline, reform = (
            (reused, other_side) if refused_as == "baseline" else (other_side, reused)
        )
        insert_report_run_naming(
            connection,
            economy,
            (baseline, reform),
            ReportRunOutcome(
                status=ReportRunStatus.FAILED,
                error_message=(
                    "reused_simulation_output_incompatible: a planned column is missing"
                ),
            ),
        )
        act_as_the_service(connection)

        assert _find(connection, economy) is None


def test_a_run_whose_report_run_failed_for_another_reason_is_still_reused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reused = insert_reusable_run(connection, economy)
        other_side = insert_simulation_run(
            connection, economy.population(), RunShape(status=SimulationStatus.FAILED)
        )
        insert_report_run_naming(
            connection,
            economy,
            (reused, other_side),
            ReportRunOutcome(
                status=ReportRunStatus.FAILED,
                error_message=(
                    "simulation_failed: the reform's worker ran out of memory"
                ),
            ),
        )
        act_as_the_service(connection)

        assert _find(connection, economy) == _reusable(reused)


def test_the_newest_matching_run_is_reused_unless_excluded(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        older = insert_reusable_run(connection, economy)
        newer = insert_reusable_run(
            connection, economy, RunShape(completed_at=COMPLETED_AT + timedelta(days=1))
        )
        act_as_the_service(connection)

        newest = _find(connection, economy)
        without_newest = _find(
            connection, economy, Request(excluded_run_id=newer.simulation_run_id)
        )
        without_any = _find(connection, economy, Request(excluded_run_id=uuid4()))

    assert newest == _reusable(newer)
    assert without_newest == _reusable(older)
    assert without_any == _reusable(newer)
