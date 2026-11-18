"""Standard-library log fields that reach the observability runtime's records."""

from __future__ import annotations

import logging
from uuid import UUID

import pytest
from policyengine_observability import (
    CustomLogDestination,
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    ServiceIdentity,
    configure,
)

ROW_ID = UUID("00000000-0000-4000-8000-00000000a001")


@pytest.fixture
def written_records():
    """Configure a runtime that keeps every record, standard-library ones too."""
    records: list[dict] = []

    class ListRecordWriter:
        def write(self, record: dict) -> None:
            records.append(record)

    runtime = configure(
        ObservabilityConfig(
            service=ServiceIdentity(
                name="policyengine-simulation-observability-test",
                namespace="policyengine.test",
                version="test",
                role="test",
            ),
            deployment=DeploymentIdentity(environment="test", platform="local"),
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="test-records",
                        writer_factory=ListRecordWriter,
                        delivery="inline",
                    ),
                ),
                capture_standard_library=True,
            ),
            otel=OTelConfig(enabled=False),
        )
    )
    yield records
    runtime.shutdown()


def _record_with_message(records: list[dict], message: str) -> dict:
    (record,) = [item for item in records if item.get("message") == message]
    return record


def test_fields_in_log_extra_reach_the_emitted_record(written_records) -> None:
    from policyengine_simulation_observability.log_records import (
        database_row_attributes,
        log_extra,
    )

    logging.getLogger("policyengine_simulation_entry.test").warning(
        "household_call_unconfirmed",
        extra=log_extra(
            error_type="ConnectionError",
            **database_row_attributes("household_jobs", ROW_ID),
        ),
    )

    record = _record_with_message(written_records, "household_call_unconfirmed")
    assert record["attributes"] == {
        "error_type": "ConnectionError",
        "database_table": "household_jobs",
        "database_row_id": str(ROW_ID),
    }


def test_log_extra_keeps_its_fields_on_the_standard_library_record(caplog) -> None:
    # Other handlers, and tests, read them there.
    from policyengine_simulation_observability.log_records import log_extra

    with caplog.at_level(logging.INFO):
        logging.getLogger("policyengine_simulation_entry.test").info(
            "budget_window_year_deferred", extra=log_extra(year=2027)
        )

    (record,) = caplog.records
    assert record.year == 2027
    assert record.policyengine_attributes == {"year": 2027}


def test_plain_extra_fields_are_not_emitted(written_records) -> None:
    # Why ``log_extra`` exists: the runtime's log handler keeps only
    # ``policyengine_attributes``.
    logging.getLogger("policyengine_simulation_entry.test").warning(
        "economy_state_unreadable", extra={"error_type": "OperationalError"}
    )

    record = _record_with_message(written_records, "economy_state_unreadable")
    assert "attributes" not in record


def test_a_household_key_is_dropped_even_in_policyengine_attributes(
    written_records,
) -> None:
    # Why rows are named by ``database_table`` and ``database_row_id``.
    from policyengine_simulation_observability.log_records import log_extra

    logging.getLogger("policyengine_simulation_entry.test").warning(
        "household_job_not_failed",
        extra=log_extra(household_job_id=str(ROW_ID), error_type="OperationalError"),
    )

    record = _record_with_message(written_records, "household_job_not_failed")
    assert record["attributes"] == {"error_type": "OperationalError"}


def test_a_field_named_like_a_log_record_attribute_is_emitted_without_raising(
    written_records,
) -> None:
    # ``logging`` refuses an ``extra`` key that names a LogRecord attribute,
    # and these calls sit in error handlers, where raising would turn a
    # handled failure into an unhandled one.
    from policyengine_simulation_observability.log_records import log_extra

    logging.getLogger("policyengine_simulation_entry.test").warning(
        "budget_window_not_started", extra=log_extra(name="window", module="entry")
    )

    record = _record_with_message(written_records, "budget_window_not_started")
    assert record["attributes"] == {"name": "window", "module": "entry"}
