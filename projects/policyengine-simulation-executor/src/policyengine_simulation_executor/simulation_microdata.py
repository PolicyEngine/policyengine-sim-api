"""Extract computed output microdata from a simulation pair (map-reduce payload).

A region-group child emits its baseline + reform ``output_dataset.data`` — which
the output builder has already computed — so a coordinator can concatenate these
across groups and rebuild the national output with the *existing* builder,
without re-running the model. Only the additive fields (budget, geographic) plus
these per-household/person tables are needed to reconstruct every national field.
"""

from typing import Any

import pandas as pd


def extract_output_microdata(baseline, reform) -> dict[str, Any]:
    """Columnar dump of each simulation's computed ``output_dataset.data`` per
    entity, for baseline and reform.

    The entity tables come from the model's own ``YearData.entity_data``
    property, so there is no hardcoded per-country entity list to drift.
    ``output_dataset.data`` is already populated (the output builder ran), so
    this only reads arrays — no model recompute. All columns (including the
    ``*_id`` link columns) are emitted so the coordinator's ``map_to_entity``
    stays valid on the reassembled national data.
    """

    def dump(simulation) -> dict[str, dict[str, list]]:
        entity_data = simulation.output_dataset.data.entity_data
        return {
            entity: pd.DataFrame(frame).to_dict("list")
            for entity, frame in entity_data.items()
        }

    baseline_dump = dump(baseline)
    return {
        "entities": list(baseline_dump),
        "baseline": baseline_dump,
        "reform": dump(reform),
    }
