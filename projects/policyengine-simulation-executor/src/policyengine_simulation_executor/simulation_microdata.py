"""Extract computed output microdata from a simulation pair (map-reduce payload).

A region-group child emits its baseline + reform ``output_dataset.data`` — which
the output builder has already computed — so a coordinator can concatenate these
across groups and rebuild the national output with the *existing* builder,
without re-running the model. Only the additive fields (budget, geographic) plus
these per-household/person tables are needed to reconstruct every national field.

The payload carries each column's source dtype: the model's arrays are float32,
and a plain ``to_dict("list")`` round-trip widens them to float64, which shifts
weighted aggregates by ~1e-7 relative (e.g. $0.49 on a $509M budget). Rebuilding
with ``rebuild_entity_frame`` restores the source dtypes so the reduce is
bit-exact against a direct run.
"""

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def extract_output_microdata(baseline, reform) -> dict[str, Any]:
    """Columnar dump of each simulation's computed ``output_dataset.data`` per
    entity, for baseline and reform, plus per-column source dtypes.

    The entity tables come from the model's own ``YearData.entity_data``
    property, so there is no hardcoded per-country entity list to drift.
    ``output_dataset.data`` is already populated (the output builder ran), so
    this only reads arrays — no model recompute. All columns (including the
    ``*_id`` link columns) are emitted so the coordinator's ``map_to_entity``
    stays valid on the reassembled national data.
    """

    def dump(
        simulation,
    ) -> tuple[dict[str, dict[str, list]], dict[str, dict[str, str]]]:
        columns: dict[str, dict[str, list]] = {}
        dtypes: dict[str, dict[str, str]] = {}
        for entity, frame in simulation.output_dataset.data.entity_data.items():
            df = pd.DataFrame(frame)
            columns[entity] = df.to_dict("list")
            dtypes[entity] = {col: str(dtype) for col, dtype in df.dtypes.items()}
        return columns, dtypes

    baseline_columns, baseline_dtypes = dump(baseline)
    reform_columns, reform_dtypes = dump(reform)
    return {
        "baseline": baseline_columns,
        "reform": reform_columns,
        "dtypes": {"baseline": baseline_dtypes, "reform": reform_dtypes},
    }


def rebuild_entity_frame(
    columns: dict[str, list], dtypes: dict[str, str] | None
) -> pd.DataFrame:
    """Rebuild one transported entity table, casting each column back to its
    source dtype (float32 stays float32 — see module docstring). A missing
    or uncastable dtype falls back to pandas' inferred dtype rather than
    failing the reduce, but LOUDLY: that fallback breaks the bit-exactness
    guarantee (float64 widening, ~1e-7 aggregate drift).
    """
    df = pd.DataFrame(columns)
    if dtypes is None:
        logger.warning(
            "Transported microdata carries no dtypes; rebuilding with "
            "inferred dtypes — the reduce is no longer bit-exact against a "
            "monolithic run (version-skewed child?)"
        )
        return df
    for col, dtype in dtypes.items():
        if col in df.columns and str(df[col].dtype) != dtype:
            try:
                df[col] = df[col].astype(dtype)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Could not restore dtype %s for transported column %r "
                    "(%s); keeping inferred dtype %s — bit-exactness is not "
                    "guaranteed for aggregates over this column",
                    dtype,
                    col,
                    type(exc).__name__,
                    df[col].dtype,
                )
    return df
