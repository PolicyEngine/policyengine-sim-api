"""Geographic output segment builders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    GeographicImpactOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _output_model_dump,
    _output_module_function,
    _try_compute_output,
)


def build_geographic_impact_output(value: Any) -> GeographicImpactOutput | None:
    if isinstance(value, GeographicImpactOutput):
        return value
    records = _output_model_dump(value)
    if isinstance(records, list):
        return GeographicImpactOutput(
            [dict(item) for item in records if isinstance(item, Mapping)]
        )
    if isinstance(value, list):
        return GeographicImpactOutput(
            [dict(item) for item in value if isinstance(item, Mapping)]
        )
    return None


def build_congressional_district_impact(
    country: str, baseline, reform
) -> GeographicImpactOutput | None:
    if country != "us":
        return None

    from policyengine.outputs.congressional_district_impact import (
        compute_us_congressional_district_impacts,
    )

    impact = _try_compute_output(
        "congressional district impacts",
        compute_us_congressional_district_impacts,
        baseline,
        reform,
    )
    return build_geographic_impact_output(
        getattr(impact, "district_results", None) if impact is not None else None
    )


def build_uk_constituency_impact(
    country: str, baseline, reform
) -> GeographicImpactOutput | None:
    if country != "uk":
        return None

    impact = _try_compute_output(
        "constituency impacts",
        _output_module_function(
            "constituency_impact", "compute_uk_constituency_impacts"
        ),
        baseline,
        reform,
    )
    if impact is None:
        return None
    return build_geographic_impact_output(getattr(impact, "constituency_results", None))


def build_uk_local_authority_impact(
    country: str, baseline, reform
) -> GeographicImpactOutput | None:
    if country != "uk":
        return None

    impact = _try_compute_output(
        "local authority impacts",
        _output_module_function(
            "local_authority_impact", "compute_uk_local_authority_impacts"
        ),
        baseline,
        reform,
    )
    if impact is None:
        return None
    return build_geographic_impact_output(
        getattr(impact, "local_authority_results", None)
    )
