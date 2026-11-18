"""Fixtures for the report run tests against a disposable Postgres."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from policyengine_simulation_persistence.engine import (
    create_session_factory,
    create_unpooled_engine,
)
from report_run_database import (
    SEED_URL_ENV,
    ReportRunSeeder,
    SeededHouseholdJob,
    SeededReportRun,
    worker_database_url,
)
from session_database import RUNTIME_ROLE, StatementLog, act_as_role
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session, sessionmaker


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


@pytest.fixture
def seeded_household_job(seeder: ReportRunSeeder) -> Iterator[SeededHouseholdJob]:
    """Yield one freshly seeded pending household job, removed after the test."""
    seeded = seeder.seed_household_job()
    try:
        yield seeded
    finally:
        seeder.remove_household_job(seeded)


@pytest.fixture
def worker_sessions() -> Iterator[sessionmaker[Session]]:
    """Yield a session factory connecting as the Modal worker role."""
    engine = create_unpooled_engine(worker_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def statements(worker_sessions: sessionmaker[Session]) -> Iterator[StatementLog]:
    """Record the worker sessions; fail if a statement bypassed them."""
    log = StatementLog(worker_sessions)
    yield log
    log.assert_every_statement_ran_on_a_session()


@pytest.fixture
def runtime_sessions() -> Iterator[sessionmaker[Session]]:
    """Yield a session factory acting as the comparison path's runtime role."""
    worker_database_url()
    engine = create_engine(os.environ[SEED_URL_ENV], poolclass=NullPool)
    act_as_role(engine, RUNTIME_ROLE)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()
