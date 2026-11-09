"""Fixtures for the report run tests against a disposable Postgres."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from report_run_database import (
    SEED_URL_ENV,
    ReportRunSeeder,
    SeededReportRun,
    worker_database_url,
)
from sqlalchemy import create_engine

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def seeder() -> Iterator[ReportRunSeeder]:
    """Yield a seeder on the seed role's connection, or skip without a database."""
    worker_database_url()
    engine = create_engine(os.environ[SEED_URL_ENV])
    try:
        yield ReportRunSeeder(engine)
    finally:
        engine.dispose()


@pytest.fixture
def seeded_run(seeder: ReportRunSeeder) -> Iterator[SeededReportRun]:
    """Yield one freshly seeded report run, removed again after the test."""
    seeded = seeder.seed_report_run()
    try:
        yield seeded
    finally:
        seeder.remove(seeded)
