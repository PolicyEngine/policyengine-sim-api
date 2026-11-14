"""Rows and threads for a test of two transactions.

Most tests run in one transaction rolled back afterwards. A test of two
transactions needs rows each can see, so ``conftest``'s ``committed_catalog``
commits a catalog of a country no other test uses, and deletes it here, with
every row naming it, afterwards. Such a test runs each transaction in a
thread (``run_together``) and lets the first go on only once the second
waits for its rows (``wait_until_another_backend_waits_for``).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable

    from economy_rows import EconomyCatalog
    from sqlalchemy import Connection, Engine


def delete_catalog_rows(connection: Connection, economy: EconomyCatalog) -> None:
    """Delete every row of the catalog and the rows naming it, as the superuser."""
    parameters = {
        "model_id": economy.catalog.model_id,
        "version_id": economy.catalog.version_id,
    }
    for statement in (
        "DELETE FROM public.report_runs WHERE report_id IN "
        "(SELECT id FROM public.reports WHERE tax_benefit_model_id = :model_id)",
        "DELETE FROM public.budget_window_runs WHERE report_id IN "
        "(SELECT id FROM public.reports WHERE tax_benefit_model_id = :model_id)",
        "DELETE FROM public.reports WHERE tax_benefit_model_id = :model_id",
        "DELETE FROM public.simulation_runs "
        "WHERE tax_benefit_model_version_id = :version_id",
        "DELETE FROM public.simulations "
        "WHERE tax_benefit_model_version_id = :version_id",
        "DELETE FROM public.regions WHERE tax_benefit_model_version_id = :version_id",
        "DELETE FROM public.datasets WHERE tax_benefit_model_version_id = :version_id",
        "DELETE FROM public.tax_benefit_model_versions WHERE id = :version_id",
        "DELETE FROM public.tax_benefit_models WHERE id = :model_id",
    ):
        connection.execute(text(statement), parameters)


# How long a test waits for another transaction's lock or thread.
LOCK_WAIT_TIMEOUT_SECONDS = 10.0


def wait_until_another_backend_waits_for(engine: Engine, holder_pid: int) -> str:
    """Return, once a backend waits for a lock ``holder_pid`` holds, its statement.

    The statement is read in a transaction of its own once a waiting
    backend's pid is found: the view's copy of every backend's status is
    taken when the first read starts, a moment before ``pg_blocking_pids``
    runs.
    """
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting_pid = connection.execute(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE :holder_pid = ANY(pg_blocking_pids(pid)) LIMIT 1"
                ),
                {"holder_pid": holder_pid},
            ).scalar_one_or_none()
            connection.rollback()
            if waiting_pid is not None:
                statement = connection.execute(
                    text("SELECT query FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": waiting_pid},
                ).scalar_one()
                connection.rollback()
                return statement
            time.sleep(0.05)
    msg = "the second request never waited for the first one's rows"
    raise AssertionError(msg)


def run_together(
    works: tuple[Callable[[], None], ...], *, released: threading.Event
) -> None:
    """Run each in its own thread, wait for all, and re-raise the first error.

    A failing one sets ``released``, so no other waits on it for long.
    """
    errors: list[BaseException] = []

    def recording_errors(work: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                work()
            except BaseException as error:  # noqa: BLE001  # re-raised below
                errors.append(error)
                released.set()

        return run

    threads = [threading.Thread(target=recording_errors(work)) for work in works]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(LOCK_WAIT_TIMEOUT_SECONDS * 2)
    if errors:
        raise errors[0]
