"""Characterization tests for live column type checks in schema qualification."""

from __future__ import annotations

import pytest
from policyengine_stage12_persistence.validation import (
    Stage12RuntimeAccessError,
    _require_column_type,
)
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

STATUS_ENUM = ENUM("pending", "running", name="comparison_status")


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        pytest.param(
            STATUS_ENUM,
            ENUM("pending", "running", name="comparison_status"),
            id="same-enum",
        ),
        pytest.param(UUID(), UUID(), id="uuid"),
        pytest.param(JSON(), JSON(), id="json"),
        pytest.param(JSON(), JSONB(), id="jsonb-for-json"),
        pytest.param(
            DateTime(timezone=True), DateTime(timezone=True), id="aware-datetime"
        ),
        pytest.param(String(64), String(64), id="same-length-string"),
        pytest.param(String(), Text(), id="unbounded-text-for-string"),
        pytest.param(Integer(), Integer(), id="integer"),
        pytest.param(Integer(), BigInteger(), id="biginteger-for-integer"),
        pytest.param(Boolean(), String(), id="unchecked-type"),
    ],
)
def test_matching_column_types_pass(expected, actual):
    _require_column_type(expected, actual, column="table.column")


@pytest.mark.parametrize(
    ("expected", "actual", "message"),
    [
        pytest.param(
            STATUS_ENUM,
            String(),
            "table.column has an unexpected type",
            id="enum-as-string",
        ),
        pytest.param(
            STATUS_ENUM,
            ENUM("pending", "running", name="other_status"),
            "table.column has an unexpected enum",
            id="enum-name",
        ),
        pytest.param(
            STATUS_ENUM,
            ENUM("running", "pending", name="comparison_status"),
            "table.column has an unexpected enum",
            id="enum-order",
        ),
        pytest.param(
            STATUS_ENUM,
            ENUM("pending", name="comparison_status"),
            "table.column has an unexpected enum",
            id="enum-values",
        ),
        pytest.param(
            UUID(), String(), "table.column has an unexpected type", id="uuid"
        ),
        pytest.param(JSON(), Text(), "table.column has an unexpected type", id="json"),
        pytest.param(
            DateTime(timezone=True),
            DateTime(),
            "table.column has an unexpected type",
            id="naive-datetime",
        ),
        pytest.param(
            DateTime(timezone=True),
            String(),
            "table.column has an unexpected type",
            id="datetime",
        ),
        pytest.param(
            String(64),
            String(32),
            "table.column has an unexpected type",
            id="string-length",
        ),
        pytest.param(
            String(64),
            Integer(),
            "table.column has an unexpected type",
            id="string",
        ),
        pytest.param(
            Integer(), String(), "table.column has an unexpected type", id="integer"
        ),
    ],
)
def test_mismatched_column_types_are_rejected(expected, actual, message):
    with pytest.raises(Stage12RuntimeAccessError) as error:
        _require_column_type(expected, actual, column="table.column")

    assert str(error.value) == message
