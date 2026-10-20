"""The one shared factory for entity frames in output-builder tests.

policyengine holds each entity's output as a microdf ``MicroDataFrame`` weighted
by its ``<entity>_weight`` column, so ``.sum()`` on any column is already a
weighted sum. Builder tests build their entity frames here, so they weight
exactly as production does: #767 escaped because a builder test used plain
``pd.DataFrame``s, which carry no weights.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from microdf import MicroDataFrame

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def weighted_entity_frame(
    columns: Mapping[str, Sequence[object]], *, entity: str = "household"
) -> MicroDataFrame:
    """Return ``columns`` as a MicroDataFrame weighted by ``<entity>_weight``.

    Rows without a weight column get the weights 1, 2, 3 and so on. A given
    weight column must differ between rows, so that a total which ignores the
    weights can't match a weighted expectation by accident.
    """
    weight_column = f"{entity}_weight"
    frame = pd.DataFrame(dict(columns))
    if weight_column not in frame.columns:
        frame[weight_column] = [float(row + 1) for row in range(len(frame))]
    weights = frame[weight_column]
    if len(frame) > 1 and (weights == weights.iloc[0]).all():
        msg = (
            f"{weight_column} is the same on every row; give the rows different "
            "weights so that unweighted totals can't pass for weighted ones"
        )
        raise ValueError(msg)
    return MicroDataFrame(frame, weights=weight_column)
