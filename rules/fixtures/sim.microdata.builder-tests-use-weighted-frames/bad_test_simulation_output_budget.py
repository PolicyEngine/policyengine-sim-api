"""Proof fixture: builder-test households built outside the weighted factory.

The rule must reject it. ``plain_simulation`` is how the builder test that
missed #767 built its households: a plain ``pd.DataFrame`` carries no weights,
so summing ``household_weight`` through it counted households correctly while
production's weighted frames returned the sum of squared weights.
``hand_weighted_simulation`` weights its frame, but by hand rather than through
the one shared factory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from microdf import MicroDataFrame


def plain_simulation(columns: dict[str, list[float]]) -> SimpleNamespace:
    """Build a simulation whose household output is a plain DataFrame."""
    household = pd.DataFrame(columns)
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )


def hand_weighted_simulation(columns: dict[str, list[float]]) -> SimpleNamespace:
    """Build a simulation whose household output is weighted by hand."""
    household = MicroDataFrame(pd.DataFrame(columns), weights="household_weight")
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )
