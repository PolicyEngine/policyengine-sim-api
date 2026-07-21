"""Reduce region-group children into the national macro output.

The segmented national path runs one ``region_group`` child per partition
group (each with ``_emit_microdata``), then this module concatenates the
children's computed ``output_dataset`` microdata into full national entity
tables and runs the EXISTING ``SimulationOutputBuilder`` over stand-in
simulations. The model never re-runs: the reducers only read
``output_dataset.data``, which is per-household independent, so the reduce
reproduces the monolithic national output bit-for-bit (validated on staging:
group == sum of members at 2e-16; rebuilt output == direct output with zero
differing leaves once dtypes are preserved).

Two hazards this module exists to handle:

* ``Simulation.ensure()`` consults the in-process cache and disk, NOT the
  instance's ``output_dataset`` field — a bare assignment gets ignored and
  the model re-runs. ``PrecomputedSimulation`` overrides ``ensure`` to a
  no-op (a subclass, not a monkeypatch: warm worker processes must never be
  globally patched).
* The transported arrays must be cast back to their source dtypes
  (``rebuild_entity_frame``), or float32 widens to float64 and weighted
  aggregates drift ~1e-7 relative.
"""

from typing import Any

import pandas as pd
from microdf import MicroDataFrame

from policyengine.core import Simulation

from policyengine_simulation_executor.simulation_microdata import (
    rebuild_entity_frame,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)


class PrecomputedSimulation(Simulation):
    """A Simulation whose ``output_dataset`` is injected, never computed."""

    def ensure(self) -> None:
        """No-op: the output data is already attached (see module docstring)."""
        return


def concatenate_microdata(
    child_outputs: list[dict],
) -> dict[str, dict[str, pd.DataFrame]]:
    """Concatenate the children's microdata into national entity tables.

    Returns ``{side: {entity: DataFrame}}`` for ``baseline``/``reform``,
    concatenated in child order with source dtypes restored.
    """
    if not child_outputs:
        raise ValueError("No child outputs to reduce")
    payloads = []
    for index, child in enumerate(child_outputs):
        payload = child.get("_microdata")
        if not payload:
            raise ValueError(f"Child {index} returned no _microdata payload")
        payloads.append(payload)

    entities = payloads[0]["entities"]
    sides: dict[str, dict[str, pd.DataFrame]] = {}
    for side in ("baseline", "reform"):
        frames: dict[str, pd.DataFrame] = {}
        for entity in entities:
            parts = [
                rebuild_entity_frame(
                    payload[side][entity],
                    (payload.get("dtypes") or {}).get(side, {}).get(entity),
                )
                for payload in payloads
            ]
            frames[entity] = pd.concat(parts, ignore_index=True)
        sides[side] = frames
    return sides


def _us_dataset_from_frames(frames: dict[str, pd.DataFrame], *, year: int):
    """Wrap concatenated entity tables as an in-memory US dataset.

    Weights need no special handling: they ride as ``<entity>_weight``
    columns and ``MicroDataFrame(df, weights=...)`` re-derives them.
    """
    from policyengine.tax_benefit_models.us.datasets import (
        PolicyEngineUSDataset,
        USYearData,
    )

    fields = {}
    for entity, df in frames.items():
        weight_column = f"{entity}_weight"
        fields[entity] = (
            MicroDataFrame(df, weights=weight_column)
            if weight_column in df.columns
            else MicroDataFrame(df)
        )
    return PolicyEngineUSDataset(
        data=USYearData(**fields),
        year=year,
        filepath=None,
        name="segmented-national",
        description="Concatenated region-group output microdata",
    )


def build_national_output(
    child_outputs: list[dict],
    *,
    country: str,
    simulation_params: dict[str, Any],
    country_module,
    year: int,
    resolved_data_version: str | None = None,
) -> dict[str, Any]:
    """The reduce: children's microdata -> full national macro output dict.

    Runs the existing ``SimulationOutputBuilder`` over stand-in simulations
    with ``resolved_region_code=country`` so the national congressional
    district table populates. No model recompute occurs.
    """
    if country.lower() != "us":
        raise ValueError(
            f"No segmented-national reduce for country {country!r} (US only)"
        )

    sides = concatenate_microdata(child_outputs)
    datasets = {
        side: _us_dataset_from_frames(frames, year=year)
        for side, frames in sides.items()
    }

    def stand_in(dataset) -> PrecomputedSimulation:
        simulation = PrecomputedSimulation(
            dataset=dataset,
            tax_benefit_model_version=country_module.model,
            policy=None,
        )
        simulation.output_dataset = dataset
        return simulation

    builder = SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=datasets["baseline"],
        baseline=stand_in(datasets["baseline"]),
        reform=stand_in(datasets["reform"]),
        resolved_data_version=resolved_data_version,
        resolved_region_code=country,
    )
    return builder.serialize()
