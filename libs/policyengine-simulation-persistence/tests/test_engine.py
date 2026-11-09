"""Connection settings for the simulation service's database, without a database."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import (
    create_simulation_engine,
    normalized_database_url,
)


def test_database_url_selects_psycopg_without_changing_the_secret_format() -> None:
    url = normalized_database_url(
        "postgresql://simulation_service:secret@db.example:5432/postgres"
    )

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "simulation_service"
    assert url.host == "db.example"
    assert url.database == "postgres"


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        " postgresql://user:secret@db.example/postgres",
        "postgres://user:secret@db.example/postgres",
        "postgresql+psycopg://user:secret@db.example/postgres",
        "postgresql://db.example/postgres",
        "postgresql://user:secret@db.example",
        "not a url",
    ],
)
def test_malformed_database_urls_are_rejected_without_echoing_them(
    database_url,
) -> None:
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        normalized_database_url(database_url)

    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None or "secret" not in str(error.value.__cause__)


def test_runtime_engine_never_prepares_server_side_statements(monkeypatch) -> None:
    captured_options = {}

    def capture_engine(url, **options):
        captured_options.update(options, url=url)
        return object()

    monkeypatch.setattr(
        "policyengine_simulation_persistence.engine.create_engine",
        capture_engine,
    )

    create_simulation_engine(
        "postgresql://simulation_service:secret@db.example/postgres"
    )

    assert captured_options["url"].drivername == "postgresql+psycopg"
    assert captured_options["connect_args"]["prepare_threshold"] is None
    assert captured_options["connect_args"]["connect_timeout"] > 0
    assert captured_options["pool_pre_ping"] is True
    assert captured_options["pool_size"] >= 1
