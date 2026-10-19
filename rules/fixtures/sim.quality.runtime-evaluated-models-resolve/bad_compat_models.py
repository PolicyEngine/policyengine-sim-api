"""Proof fixture: a model whose base ruff reads through a re-export.

The rule must reject it. This is compat_models.py before the fix: ruff reads
the base as the executor's simulation_macro_output re-export, which is not
listed, so it would move imports the model's annotations need.
"""

from __future__ import annotations

from policyengine_simulation_executor.simulation_macro_output import (
    SingleYearMacroOutput,
)


class EconomyComparison(SingleYearMacroOutput):
    """Legacy response schema name kept for generated clients."""
