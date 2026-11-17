"""The report run tests skip without their database, unless a job requires it.

The Public API schema job sets ``REPORT_RUN_REQUIRE_DATABASE_TESTS=1``, so a
database it failed to name fails the tests there instead of skipping them.
"""

from __future__ import annotations

import pytest
from report_run_database import (
    REQUIRE_DATABASE_ENV,
    SEED_URL_ENV,
    WORKER_URL_ENV,
    worker_database_url,
)


def _without_test_databases(monkeypatch) -> None:
    monkeypatch.delenv(WORKER_URL_ENV, raising=False)
    monkeypatch.delenv(SEED_URL_ENV, raising=False)


def test_the_tests_skip_without_a_database(monkeypatch) -> None:
    _without_test_databases(monkeypatch)
    monkeypatch.delenv(REQUIRE_DATABASE_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match=WORKER_URL_ENV):
        worker_database_url()


def test_a_job_that_requires_the_database_fails_without_one(monkeypatch) -> None:
    _without_test_databases(monkeypatch)
    monkeypatch.setenv(REQUIRE_DATABASE_ENV, "1")

    with pytest.raises(pytest.fail.Exception, match=WORKER_URL_ENV):
        worker_database_url()
