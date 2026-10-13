"""Distributional output segment builders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_executor.simulation_macro_output import (
    DecileOutput,
    IntraDecileOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _collection_records,
    _number,
    _try_compute_output,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation

INTRA_DECILE_COLUMNS = {
    "Lose more than 5%": "lose_more_than_5pct",
    "Lose less than 5%": "lose_less_than_5pct",
    "No change": "no_change",
    "Gain less than 5%": "gain_less_than_5pct",
    "Gain more than 5%": "gain_more_than_5pct",
}


def build_decile(analysis: object) -> DecileOutput:
    """Return the income-decile changes from the analysis's ``decile_impacts``."""
    return build_decile_output(getattr(analysis, "decile_impacts", None))


def build_decile_output(collection: object) -> DecileOutput:
    """Key absolute and relative changes by decile number (``"1"``..``"10"``).

    Rows with a decile of 0 or below are dropped; missing values become 0.0.
    """
    if isinstance(collection, DecileOutput):
        return collection
    average: dict[str, float] = {}
    relative: dict[str, float] = {}
    for row in sorted(
        _collection_records(collection),
        key=lambda item: _number(item.get("decile")),
    ):
        decile = int(_number(row.get("decile")))
        if decile <= 0:
            continue
        key = str(decile)
        average[key] = _number(row.get("absolute_change"))
        relative[key] = _number(row.get("relative_change"))
    return DecileOutput(average=average, relative=relative)


def build_intra_decile_output(
    baseline: Simulation, reform: Simulation
) -> IntraDecileOutput:
    """Compute household net income winners and losers within each income decile.

    A calculation failure is logged and yields empty deciles with zero overall
    shares rather than failing the request; SPM selection errors propagate.
    """
    from policyengine.outputs.intra_decile_impact import (
        compute_intra_decile_impacts,
    )

    collection = _try_compute_output(
        "intra-decile impacts",
        compute_intra_decile_impacts,
        baseline,
        reform,
        income_variable="household_net_income",
        entity="household",
    )
    return build_intra_decile_output_from_collection(collection)


def build_wealth_decile(country: str, wealth_decile: object) -> DecileOutput | None:
    """Return wealth-decile changes for the UK; None for other countries."""
    if country != "uk":
        return None
    return build_decile_output(wealth_decile)


def build_intra_wealth_decile(
    country: str, intra_wealth_decile: object
) -> IntraDecileOutput | None:
    """Return within-wealth-decile winners and losers for the UK; None elsewhere."""
    if country != "uk":
        return None
    return build_intra_decile_output_from_collection(intra_wealth_decile)


def build_intra_decile_output_from_collection(
    collection: object,
) -> IntraDecileOutput:
    """Reshape intra-decile rows into per-category decile lists plus overall shares.

    The ``all`` share for each category is the unweighted mean across deciles.
    """
    if isinstance(collection, IntraDecileOutput):
        return collection
    deciles: dict[str, list[float]] = {label: [] for label in INTRA_DECILE_COLUMNS}
    all_values: dict[str, float] = {label: 0.0 for label in INTRA_DECILE_COLUMNS}
    rows = [
        row
        for row in sorted(
            _collection_records(collection),
            key=lambda item: _number(item.get("decile")),
        )
        if int(_number(row.get("decile"))) > 0
    ]

    for label, column in INTRA_DECILE_COLUMNS.items():
        values = [_number(row.get(column)) for row in rows]
        deciles[label] = values
        all_values[label] = sum(values) / len(values) if values else 0.0
    return IntraDecileOutput(deciles=deciles, all=all_values)
