"""Links to a run's stored files, offered for 30 days after they were written."""

from __future__ import annotations

from datetime import timedelta

import pytest
from policyengine_simulation_persistence import (
    OUTPUT_FILE_RETENTION,
    OutputFile,
    available_output_files,
    record_output_files,
)
from result_support import comparison_ready, result_stores
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

SHA256 = "ab" * 32
MICRODATA = OutputFile(
    role="microdata", uri="gs://simulation-artifacts/run/baseline.h5", sha256=SHA256
)
COMPARISON = OutputFile(
    role="comparison",
    uri="gs://simulation-artifacts/run/comparison.json",
    sha256=SHA256,
)


def test_a_run_offers_its_files_for_thirty_days(
    migrated_database, public_api_rows
) -> None:
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)
    written_at = stores.clock()
    with stores.transitions.engine.begin() as connection:
        record_output_files(
            connection,
            [MICRODATA],
            written_at=written_at,
            simulation_run_id=report_run.baseline_simulation_run_id,
        )
        record_output_files(
            connection, [COMPARISON], written_at=written_at, report_run_id=report_run.id
        )

    with stores.transitions.engine.connect() as connection:
        baseline_files = available_output_files(
            connection,
            now=written_at + OUTPUT_FILE_RETENTION - timedelta(seconds=1),
            simulation_run_id=report_run.baseline_simulation_run_id,
        )
        comparison_files = available_output_files(
            connection, now=written_at, report_run_id=report_run.id
        )
        expired = available_output_files(
            connection,
            now=written_at + OUTPUT_FILE_RETENTION,
            simulation_run_id=report_run.baseline_simulation_run_id,
        )

    assert [file.as_output_file() for file in baseline_files] == [MICRODATA]
    assert [file.as_output_file() for file in comparison_files] == [COMPARISON]
    assert baseline_files[0].written_at == written_at
    assert expired == []
    assert timedelta(days=30) == OUTPUT_FILE_RETENTION


def test_a_file_belongs_to_exactly_one_run(migrated_database, public_api_rows) -> None:
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    with (
        stores.transitions.engine.begin() as connection,
        pytest.raises(ValueError, match="exactly one run"),
    ):
        record_output_files(
            connection,
            [MICRODATA],
            written_at=stores.clock(),
            simulation_run_id=report_run.baseline_simulation_run_id,
            report_run_id=report_run.id,
        )


def test_a_run_links_each_file_once(migrated_database, public_api_rows) -> None:
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    with (
        pytest.raises(IntegrityError, match="uq_output_files"),
        stores.transitions.engine.begin() as connection,
    ):
        record_output_files(
            connection,
            [COMPARISON, COMPARISON],
            written_at=stores.clock(),
            report_run_id=report_run.id,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("sha256", "not-a-digest"), ("uri", "baseline.h5"), ("role", "")],
)
def test_a_malformed_file_link_is_refused(field_name, value) -> None:
    values = {"role": "microdata", "uri": "gs://bucket/file.h5", "sha256": SHA256}
    values[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        OutputFile(**values)
