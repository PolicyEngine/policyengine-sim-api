"""Proof fixture: a model whose base ruff reads at a listed path.

The rule must accept it. This is compat_models.py after the fix: the base is
imported from the contract module that defines it, the path ruff.toml lists.
"""

from __future__ import annotations

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput


class EconomyComparison(SingleYearMacroOutput):
    """Legacy response schema name kept for generated clients."""
