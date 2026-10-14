"""Proof fixture: a total that keeps its weights. The rule must accept it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from policyengine.core import Simulation


def baseline_net_income(baseline: Simulation) -> float:
    """Total household net income, weighted by the household frame's weights."""
    household = baseline.output_dataset.data.household
    return float(household["household_net_income"].sum())
