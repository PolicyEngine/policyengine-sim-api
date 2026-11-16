"""What every budget-window record shares: its years and each year's runs.

A budget-window report compares the same baseline and reform in each year
of a window of 1 to 75 consecutive years. It is stored as an economy report
whose ``budget_window_size`` is the window's length, with one
``budget_window_runs`` row for each calculation and, for each year, a report
run naming that row and the year. Each year has its own baseline and reform
simulations, of that year. The report names its first year's simulations.

The simulation service finds or creates a window by key
(``keyed_window_records``) and re-runs a stored one
(``economy_rerun_records``). ``check_window_years`` refuses years that
aren't such a window's. ``WindowYearRuns`` is a year's report run and each
side's simulation run, as a found window's years and a re-run's years both
hold them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_persistence.tables import (
    LATEST_YEAR,
    MAX_BUDGET_WINDOW_YEARS,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


class WindowSide(Protocol):
    """A window year's side: its simulation and the run the year names."""

    @property
    def simulation_id(self) -> UUID:
        """The side's simulation."""
        ...

    @property
    def simulation_run_id(self) -> UUID:
        """The run of it the year names."""
        ...


class WindowYearRuns(Protocol):
    """A year of a window run: its year, its report run and each side.

    A found or created window's ``FoundWindowYear`` and a re-run's
    ``WindowRerunYear`` are both one.
    """

    @property
    def year(self) -> int:
        """The year."""
        ...

    @property
    def report_run_id(self) -> UUID:
        """The year's report run."""
        ...

    @property
    def baseline(self) -> WindowSide:
        """The baseline side."""
        ...

    @property
    def reform(self) -> WindowSide:
        """The reform side."""
        ...


def check_window_years(first_year: int, years: Sequence[int]) -> None:
    """Raise ValueError unless ``years`` are 1 to 75 in a row from ``first_year``.

    The last of them must also be no later than the tables' latest year.
    """
    expected = tuple(range(first_year, first_year + len(years)))
    if (
        not years
        or len(years) > MAX_BUDGET_WINDOW_YEARS
        or tuple(years) != expected
        or expected[-1] > LATEST_YEAR
    ):
        msg = (
            "a budget window's years must be 1 to "
            f"{MAX_BUDGET_WINDOW_YEARS} consecutive years from its first, "
            f"ending by {LATEST_YEAR}"
        )
        raise ValueError(msg)
