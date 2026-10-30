"""Write time, read time and storage of the result sample, as measurements.

Each sample result is written and read back through the runtime role, and
the times and the result tables' sizes are printed. Nothing depends on them:
the numbers are recorded, with no thresholds. The same measurements against
the staging database are an operator step.
"""

from __future__ import annotations

import time

import pytest
from policyengine_simulation_persistence import (
    EconomyComparisonRecorder,
    read_economy_comparison,
)
from policyengine_simulation_persistence.result_tables import ECONOMY_RESULT_TABLES
from public_api_support import superuser_engine
from result_support import (
    SAMPLE_NAMES,
    comparison_ready,
    labels_for,
    load_sample,
    result_stores,
)
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _table_bytes(database) -> int:
    engine = superuser_engine(database.superuser_url)
    try:
        with engine.connect() as connection:
            return sum(
                connection.execute(
                    text("SELECT pg_total_relation_size(:table)"),
                    {"table": f"simulation.{table.name}"},
                ).scalar_one()
                for table in ECONOMY_RESULT_TABLES
            )
    finally:
        engine.dispose()


def test_measure_writing_and_reading_every_sample(
    migrated_database, public_api_rows, capsys
) -> None:
    stores = result_stores(migrated_database)
    recorder = EconomyComparisonRecorder()
    empty_bytes = _table_bytes(migrated_database)
    lines = []
    for name in SAMPLE_NAMES:
        result = load_sample(name)
        report_run = comparison_ready(stores, migrated_database, public_api_rows)
        started = time.perf_counter()
        stores.transitions.complete_comparison(
            report_run.id,
            lambda connection, record, result=result: recorder.write(
                connection, record, result
            ),
        )
        written = time.perf_counter()
        with stores.transitions.engine.connect() as connection:
            rebuilt = read_economy_comparison(
                connection, report_run.id, labels_for(result)
            )
        read = time.perf_counter()
        assert rebuilt == result
        lines.append(
            f"  {name}: write {1000 * (written - started):.1f} ms, "
            f"read {1000 * (read - written):.1f} ms"
        )
    stored_bytes = _table_bytes(migrated_database) - empty_bytes
    with capsys.disabled():
        print("\nresult tables measurements:")  # noqa: T201  # the measurement this test records
        for measurement in lines:
            print(measurement)  # noqa: T201  # the measurement this test records
        print(  # noqa: T201  # the measurement this test records
            f"  storage for {len(SAMPLE_NAMES)} results: {stored_bytes / 1024:.0f} KiB"
        )
