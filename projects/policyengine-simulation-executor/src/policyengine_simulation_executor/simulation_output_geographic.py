"""Geographic output segment builders."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    CongressionalDistrictImpactOutput,
    CongressionalDistrictImpactRecord,
    GeographicImpactOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _number,
    _output_model_dump,
    _output_module_function,
    _try_compute_output,
)


@lru_cache(maxsize=1)
def _policyengine_us_district_metadata() -> tuple[dict[int, str], frozenset[str]]:
    from policyengine.countries.us.data import AT_LARGE_STATES, US_STATE_FIPS

    return (
        {
            int(fips): state_abbreviation
            for state_abbreviation, fips in US_STATE_FIPS.items()
        },
        frozenset(AT_LARGE_STATES),
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


def build_congressional_district_impact_output(
    value: Any,
) -> CongressionalDistrictImpactOutput | None:
    if isinstance(value, CongressionalDistrictImpactOutput):
        return value
    records = _output_model_dump(value)
    if not isinstance(records, list):
        records = value if isinstance(value, list) else None
    if not isinstance(records, list):
        return None

    return CongressionalDistrictImpactOutput(
        districts=[
            _build_congressional_district_record(record)
            for record in records
            if isinstance(record, Mapping)
        ]
    )


def _build_congressional_district_record(
    record: Mapping[str, Any],
) -> CongressionalDistrictImpactRecord:
    return CongressionalDistrictImpactRecord(
        district=_public_congressional_district_code(record),
        average_household_income_change=_number(
            record.get("average_household_income_change")
        ),
        relative_household_income_change=_number(
            record.get("relative_household_income_change")
        ),
        winner_percentage=_number(record.get("winner_percentage")),
        loser_percentage=_number(record.get("loser_percentage")),
        no_change_percentage=_number(record.get("no_change_percentage")),
        population=_number(record.get("population")),
    )


def _public_congressional_district_code(record: Mapping[str, Any]) -> str:
    state_fips = _integer_record_value(record, "state_fips")
    district_number = _integer_record_value(record, "district_number")
    geoid = _integer_record_value(record, "district_geoid")
    if geoid is not None:
        if state_fips is None:
            state_fips = geoid // 100
        if district_number is None:
            district_number = geoid % 100
    if state_fips is None:
        raise ValueError("Congressional district output is missing state FIPS")

    state_abbreviation_by_fips, at_large_states = _policyengine_us_district_metadata()
    state_abbreviation = state_abbreviation_by_fips.get(state_fips)
    if state_abbreviation is None:
        raise ValueError(f"Unknown state FIPS code: {state_fips}")
    if district_number is None:
        raise ValueError("Congressional district output is missing district number")

    public_district_number = (
        1 if state_abbreviation in at_large_states else district_number
    )
    return f"{state_abbreviation}-{public_district_number:02d}"


def _integer_record_value(record: Mapping[str, Any], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _should_build_us_congressional_district_impact(region_code: str | None) -> bool:
    if region_code is None:
        return True
    normalized_region_code = region_code.lower()
    return normalized_region_code == "us" or normalized_region_code.startswith("state/")


def build_congressional_district_impact(
    country: str, baseline, reform, *, region_code: str | None = None
) -> CongressionalDistrictImpactOutput | None:
    if country != "us":
        return None
    if not _should_build_us_congressional_district_impact(region_code):
        return None

    from policyengine.outputs.congressional_district_impact import (
        compute_us_congressional_district_impacts,
    )

    def compute_and_format() -> CongressionalDistrictImpactOutput | None:
        impact = compute_us_congressional_district_impacts(baseline, reform)
        return build_congressional_district_impact_output(
            getattr(impact, "district_results", None)
        )

    return _try_compute_output("congressional district impacts", compute_and_format)


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
