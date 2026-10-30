"""Spans and version provenance shared by the simulation output builders."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    Stage,
)

from policyengine_simulation_executor.release_bundle import get_country_release_bundle

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    from policyengine_observability import ObservabilityRuntime


def output_span(
    runtime: ObservabilityRuntime | None, name: str
) -> AbstractContextManager[object]:
    """Open a span named ``name``, or nothing when no runtime is given."""
    if runtime is None:
        return nullcontext()
    return runtime.span(name)


def output_model_version(
    country_module: ModuleType, *, runtime: ObservabilityRuntime | None
) -> str:
    """Return the country model version that computed an output."""
    with output_span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_MODEL_VERSION)):
        return str(getattr(country_module.model, "version", ""))


def output_data_version(
    country: str,
    *,
    runtime: ObservabilityRuntime | None,
    resolved: str | None,
    simulation_params: Mapping[str, Any] | None = None,
    dataset: object = None,
) -> str:
    """Return the data version an output reports.

    In order: the ``resolved`` revision of the dataset the run loaded, a
    ``data_version`` request parameter, the dataset's own metadata, and the
    release's data version; an empty string when none is available.
    """
    with output_span(runtime, ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_DATA_VERSION)):
        if resolved:
            return str(resolved)
        if simulation_params is not None and simulation_params.get("data_version"):
            return str(simulation_params["data_version"])
        metadata = getattr(dataset, "metadata", {}) or {}
        for key in ("data_version", "version"):
            value = metadata.get(key)
            if value is not None:
                return str(value)
        try:
            return get_country_release_bundle(country).data_version
        except ValueError:
            return ""
