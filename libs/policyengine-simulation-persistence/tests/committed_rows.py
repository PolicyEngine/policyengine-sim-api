"""Remove the rows a test of two transactions committed.

Most tests run in one transaction rolled back afterwards. A test of two
transactions needs rows each can see, so ``conftest``'s ``committed_catalog``
commits a catalog of a country no other test uses, and deletes it here, with
every row naming it, afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from economy_rows import EconomyCatalog
    from sqlalchemy import Connection


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
