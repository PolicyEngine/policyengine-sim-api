"""Geographic output segment builders."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd
from policyengine.data.uk_geography_assets import (
    CONSTITUENCY_ASSET_SPEC,
)

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

if TYPE_CHECKING:
    from policyengine.core import Simulation
    from policyengine.data.uk_geography_assets import UKGeographyAssetSpec
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata


def _required_uk_geography_lookup_csv_path(spec: UKGeographyAssetSpec) -> str:
    """Resolve one required lookup under the runtime GCP identity."""
    from policyengine.outputs.uk_geography_impact import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine's lookup resolver
        resolve_uk_geography_lookup_csv_path,
    )

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # import cycle: simulation_runtime imports this module through its output builder
        setup_gcp_credentials,
    )

    with setup_gcp_credentials():
        path = resolve_uk_geography_lookup_csv_path(
            spec,
            download_missing_assets=True,
        )
    if path is None:
        msg = (
            f"Required UK {spec.geography_type} lookup CSV "
            f"{spec.lookup_csv_filename!r} could not be resolved"
        )
        raise FileNotFoundError(msg)
    return path


def _complete_uk_geography_output(
    value: object,
    *,
    code_field: str,
    name_field: str,
) -> GeographicImpactOutput:
    """Require every UK geography record to contain lookup-owned metadata."""
    output = build_geographic_impact_output(value)
    if output is None or not output.root:
        msg = "UK geography output did not contain result records"
        raise ValueError(msg)
    for record in output.root:
        record_values = record.model_dump(mode="python")
        code = record_values.get(code_field)
        name = record_values.get(name_field)
        if not isinstance(code, str) or not code.strip():
            msg = f"UK geography output is missing {code_field}"
            raise ValueError(msg)
        if not isinstance(name, str) or not name.strip() or name == code:
            msg = f"UK geography {code!r} is missing a lookup name"
            raise ValueError(msg)
        if record_values.get("x") is None or record_values.get("y") is None:
            msg = f"UK geography {code!r} is missing lookup coordinates"
            raise ValueError(msg)
    return output


@lru_cache(maxsize=1)
def _policyengine_us_district_metadata() -> tuple[dict[int, str], frozenset[str]]:
    from policyengine.countries.us.data import (  # noqa: PLC0415  # policyengine's US data loads only for US district output
        AT_LARGE_STATES,
        US_STATE_FIPS,
    )

    return (
        {
            int(fips): state_abbreviation
            for state_abbreviation, fips in US_STATE_FIPS.items()
        },
        frozenset(AT_LARGE_STATES),
    )


def build_geographic_impact_output(value: object) -> GeographicImpactOutput | None:
    """Validate a list of per-area records (or a model dumping to one) into the output.

    Non-mapping items are dropped; None means ``value`` held no record list.
    """
    if isinstance(value, GeographicImpactOutput):
        return value
    records = _output_model_dump(value)
    if isinstance(records, list):
        return GeographicImpactOutput.model_validate(
            [dict(item) for item in records if isinstance(item, Mapping)]
        )
    if isinstance(value, list):
        return GeographicImpactOutput.model_validate(
            [dict(item) for item in value if isinstance(item, Mapping)]
        )
    return None


def build_congressional_district_impact_output(
    value: object,
) -> CongressionalDistrictImpactOutput | None:
    """Convert per-district records into public ``"ST-NN"`` district rows.

    Returns None when ``value`` holds no record list; raises ``ValueError`` when
    a record's state FIPS or district number cannot be determined.
    """
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
        msg = "Congressional district output is missing state FIPS"
        raise ValueError(msg)

    state_abbreviation_by_fips, at_large_states = _policyengine_us_district_metadata()
    state_abbreviation = state_abbreviation_by_fips.get(state_fips)
    if state_abbreviation is None:
        msg = f"Unknown state FIPS code: {state_fips}"
        raise ValueError(msg)
    if district_number is None:
        msg = "Congressional district output is missing district number"
        raise ValueError(msg)

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
    return normalized_region_code == "us" or normalized_region_code.startswith(
        ("state/", "region_group/")
    )


def build_congressional_district_impact(
    country: str,
    baseline: Simulation,
    reform: Simulation,
    *,
    region_code: str | None = None,
) -> CongressionalDistrictImpactOutput | None:
    """Compute US congressional district impacts for national, state or group runs.

    None for other countries or regions, and when the calculation fails (the
    failure is logged; SPM selection errors still propagate).
    """
    if country != "us":
        return None
    if not _should_build_us_congressional_district_impact(region_code):
        return None

    from policyengine.outputs.congressional_district_impact import (  # noqa: PLC0415  # bound per call, like this module's patched policyengine.outputs import above
        compute_us_congressional_district_impacts,
    )

    def compute_and_format() -> CongressionalDistrictImpactOutput | None:
        impact = compute_us_congressional_district_impacts(baseline, reform)
        return build_congressional_district_impact_output(
            getattr(impact, "district_results", None)
        )

    return _try_compute_output("congressional district impacts", compute_and_format)


def _output_household(simulation: Simulation) -> pd.DataFrame:
    return pd.DataFrame(simulation.output_dataset.data.household)


def _require_uk_area_column(
    household: pd.DataFrame, column: str, breakdown: str
) -> None:
    """Refuse a UK breakdown over a dataset that lacks its area codes.

    A breakdown is never dropped silently: a dataset without area codes has
    none of its own, and one built from a routed local-area dataset is a
    separate run.
    """
    if column not in household.columns:
        msg = (
            f"UK {breakdown} breakdowns need {column} on the simulated dataset, "
            "which carries no area codes; for such a dataset they need a run on "
            "the local-area dataset its bundle routes these regions to."
        )
        raise ValueError(msg)


def build_uk_constituency_impact(
    country: str, baseline: Simulation, reform: Simulation
) -> GeographicImpactOutput | None:
    """Compute UK parliamentary constituency impacts; None for other countries.

    Downloads the constituency lookup if needed and raises when the dataset
    lacks constituency codes or a record lacks lookup name or coordinates.
    """
    if country != "uk":
        return None
    _require_uk_area_column(
        _output_household(baseline), "constituency_code_oa", "constituency"
    )

    lookup_csv_path = _required_uk_geography_lookup_csv_path(CONSTITUENCY_ASSET_SPEC)
    impact = _output_module_function(
        "constituency_impact", "compute_uk_constituency_impacts"
    )(
        baseline,
        reform,
        constituency_csv_path=lookup_csv_path,
        download_missing_assets=False,
    )
    return _complete_uk_geography_output(
        getattr(impact, "constituency_results", None),
        code_field="constituency_code",
        name_field="constituency_name",
    )


def build_uk_local_authority_impact(
    country: str,
    baseline: Simulation,
    reform: Simulation,
    *,
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
) -> GeographicImpactOutput | None:
    """Compute UK local authority impacts, named and placed for the detected boundaries.

    None for other countries. For the UK, ``uk_local_authority_metadata`` is
    required, and a code outside its boundary version raises ``ValueError``.
    """
    if country != "uk":
        return None

    baseline_household = _output_household(baseline)
    _require_uk_area_column(baseline_household, "la_code_oa", "local-authority")
    if uk_local_authority_metadata is None:
        msg = "UK local-authority boundary metadata is required"
        raise ValueError(msg)

    from policyengine.outputs.uk_geography_impact import (  # noqa: PLC0415  # bound per call, like this module's patched policyengine.outputs import above
        compute_longwise_uk_geography_impacts,
    )

    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module loads only for UK local-authority output
        load_uk_local_authority_resources,
    )

    reform_household = _output_household(reform)
    numeric_records = compute_longwise_uk_geography_impacts(
        baseline_household=baseline_household,
        reform_household=reform_household,
        geography_column="la_code_oa",
        result_key_prefix="local_authority",
        lookup_csv_path=None,
    )
    boundary_version = uk_local_authority_metadata.boundary_version
    display_metadata = load_uk_local_authority_resources().metadata_for(
        boundary_version
    )
    records: list[dict[str, object]] = []
    for numeric_record in numeric_records:
        code = numeric_record.get("local_authority_code")
        if not isinstance(code, str) or not code:
            msg = "UK local-authority output contains an invalid code"
            raise ValueError(msg)
        metadata = display_metadata.get(code)
        if metadata is None:
            msg = (
                f"UK local-authority code {code!r} is not part of the detected "
                f"{boundary_version.value.upper()} boundary version"
            )
            raise ValueError(msg)
        records.append(
            {
                **numeric_record,
                "local_authority_name": metadata.name,
                "x": metadata.x,
                "y": metadata.y,
            }
        )
    return _complete_uk_geography_output(
        records,
        code_field="local_authority_code",
        name_field="local_authority_name",
    )
