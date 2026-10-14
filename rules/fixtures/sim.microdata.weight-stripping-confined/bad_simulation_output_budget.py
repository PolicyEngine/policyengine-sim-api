"""Proof fixture: weights stripped before a total. The rule must reject it.

Summing the raw values of a weighted column gives an unweighted sample total,
the mirror image of #767.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from policyengine.core import Simulation


def baseline_net_income(baseline: Simulation) -> float:
    """Total household net income over the sample's records, ignoring weights."""
    household = baseline.output_dataset.data.household
    return float(np.asarray(household["household_net_income"]).sum())
