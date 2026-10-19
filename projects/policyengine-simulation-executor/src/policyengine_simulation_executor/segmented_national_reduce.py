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

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import pandas as pd
from microdf import MicroDataFrame
from policyengine.core import Simulation
from policyengine_simulation_observability.stages import (
    SEGMENTED_NATIONAL_STAGES,
    Stage,
)

from policyengine_simulation_executor.national_region_outputs import (
    build_region_outputs,
)
from policyengine_simulation_executor.simulation_microdata import (
    rebuild_entity_frame,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    from policyengine.core.scoping_strategy import RowFilterStrategy
    from policyengine.tax_benefit_models.us.datasets import PolicyEngineUSDataset
    from policyengine_observability import ObservabilityRuntime

logger = logging.getLogger(__name__)


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
        msg = "No child outputs to reduce"
        raise ValueError(msg)
    payloads = []
    for index, child in enumerate(child_outputs):
        payload = child.get("_microdata")
        if not payload:
            msg = f"Child {index} returned no _microdata payload"
            raise ValueError(msg)
        payloads.append(payload)

    entities = list(payloads[0]["baseline"])
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


def _us_dataset_from_frames(
    frames: dict[str, pd.DataFrame], *, year: int
) -> PolicyEngineUSDataset:
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
    country_module: ModuleType,
    year: int,
    resolved_data_version: str | None = None,
    runtime: ObservabilityRuntime | None = None,
    regions: Mapping[str, RowFilterStrategy] | None = None,
) -> dict[str, Any]:
    """Reduce the children's microdata into the full national macro output dict.

    Runs the existing ``SimulationOutputBuilder`` over stand-in simulations
    with ``resolved_region_code=country`` so the national congressional
    district table populates. No model recompute occurs.

    ``regions`` maps region codes to their own row filters; each gets a
    block under ``output["regions"]`` built from the same national
    microdata (see ``national_region_outputs``). The key is absent when no
    block was built.
    """
    if country.lower() != "us":
        msg = f"No segmented-national reduce for country {country!r} (US only)"
        raise ValueError(msg)

    sides = concatenate_microdata(child_outputs)
    datasets = {
        side: _us_dataset_from_frames(frames, year=year)
        for side, frames in sides.items()
    }

    def stand_in(dataset: PolicyEngineUSDataset) -> PrecomputedSimulation:
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
        runtime=runtime,
    )
    output = builder.serialize()
    # The rebuilt in-memory dataset carries no artifact metadata, so the
    # builder's version fallbacks can misreport provenance; the children
    # loaded the real artifact, so their reported versions are authoritative.
    for key in ("model_version", "data_version"):
        values = sorted({str(child[key]) for child in child_outputs if child.get(key)})
        if not values:
            continue
        if len(values) > 1:
            logger.warning(
                "Segmented national children disagree on %s: %s", key, values
            )
        output[key] = values[0]
    if regions:
        span = (
            runtime.span(
                SEGMENTED_NATIONAL_STAGES.name(Stage.SEGMENTED_NATIONAL_REGION_OUTPUTS)
            )
            if runtime is not None
            else nullcontext()
        )
        with span:
            region_outputs = build_region_outputs(
                datasets,
                regions=regions,
                country=country,
                simulation_params=simulation_params,
                country_module=country_module,
                year=year,
                national_output=output,
                stand_in=stand_in,
            )
        if runtime is not None:
            runtime.set_context(
                region_block_count=len(region_outputs),
                region_block_omitted_count=len(regions) - len(region_outputs),
            )
        if region_outputs:
            output["regions"] = region_outputs
    return output
