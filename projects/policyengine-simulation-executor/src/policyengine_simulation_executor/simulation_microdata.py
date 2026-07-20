"""Extract computed output microdata from a simulation pair (map-reduce payload).

A region-group child emits its baseline + reform ``output_dataset.data`` — which
the output builder has already computed — so a coordinator can concatenate these
across groups and rebuild the national output with the *existing* builder,
without re-running the model. Only the additive fields (budget, geographic) plus
these per-household/person tables are needed to reconstruct every national field.
"""

from typing import Any

import pandas as pd

# Entity tables per country (person + group entities), in a stable order.
US_ENTITIES = ["person", "tax_unit", "spm_unit", "family", "marital_unit", "household"]
UK_ENTITIES = ["person", "benunit", "household"]


def country_entities(country: str) -> list[str]:
    return UK_ENTITIES if country.lower() == "uk" else US_ENTITIES


def extract_output_microdata(baseline, reform, entities: list[str]) -> dict[str, Any]:
    """Columnar dump of each simulation's computed ``output_dataset.data`` per
    entity, for baseline and reform.

    ``output_dataset.data`` is already populated (the output builder ran), so
    this only reads arrays — no model recompute. All columns (including the
    ``*_id`` link columns) are emitted so the coordinator's ``map_to_entity``
    stays valid on the reassembled national data.
    """

    def dump(simulation) -> dict[str, dict[str, list]]:
        data = simulation.output_dataset.data
        return {
            entity: pd.DataFrame(getattr(data, entity)).to_dict("list")
            for entity in entities
        }

    return {"entities": entities, "baseline": dump(baseline), "reform": dump(reform)}
