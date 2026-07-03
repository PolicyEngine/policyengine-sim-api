"""Poverty output segment builders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    AgePovertyOutput,
    BaselineReformValue,
    GenderPovertyOutput,
    PovertyByGenderOutput,
    PovertyByRaceOutput,
    PovertyModuleOutputs,
    PovertyOutput,
    RacePovertyOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _collection_records,
    _number,
    _poverty_module_function,
    _try_compute_output,
)

US_POVERTY_TYPES = {
    "spm": "poverty",
    "spm_deep": "deep_poverty",
}

UK_POVERTY_TYPES = {
    "relative_bhc": "poverty",
    "absolute_bhc": "deep_poverty",
}


def _empty_baseline_reform_value() -> dict[str, float]:
    return {"baseline": 0.0, "reform": 0.0}


def _empty_age_poverty() -> dict[str, dict[str, float]]:
    return {
        "child": _empty_baseline_reform_value(),
        "adult": _empty_baseline_reform_value(),
        "senior": _empty_baseline_reform_value(),
        "all": _empty_baseline_reform_value(),
    }


def _empty_gender_poverty() -> dict[str, dict[str, float]]:
    return {
        "male": _empty_baseline_reform_value(),
        "female": _empty_baseline_reform_value(),
    }


def _poverty_type(country: str, row: Mapping[str, Any]) -> str | None:
    poverty_type = str(row.get("poverty_type") or "").lower()
    if country == "us":
        return US_POVERTY_TYPES.get(poverty_type)
    return UK_POVERTY_TYPES.get(poverty_type)


def _fill_poverty_block(
    *,
    country: str,
    output: dict[str, dict[str, dict[str, float]]],
    baseline_records: Iterable[Mapping[str, Any]],
    reform_records: Iterable[Mapping[str, Any]],
    default_group: str,
) -> None:
    for side, records in (("baseline", baseline_records), ("reform", reform_records)):
        for row in records:
            poverty_type = _poverty_type(country, row)
            if poverty_type is None:
                continue
            if poverty_type not in output:
                continue
            group = str(row.get("filter_group") or default_group).lower()
            if group not in output[poverty_type]:
                continue
            output[poverty_type][group][side] = _number(row.get("rate"))


def _age_poverty_output(values: dict[str, dict[str, float]]) -> AgePovertyOutput:
    return AgePovertyOutput(
        child=BaselineReformValue(**values["child"]),
        adult=BaselineReformValue(**values["adult"]),
        senior=BaselineReformValue(**values["senior"]),
        all=BaselineReformValue(**values["all"]),
    )


def _gender_poverty_output(
    values: dict[str, dict[str, float]],
) -> GenderPovertyOutput:
    return GenderPovertyOutput(
        male=BaselineReformValue(**values["male"]),
        female=BaselineReformValue(**values["female"]),
    )


def _race_poverty_output(values: dict[str, dict[str, float]]) -> RacePovertyOutput:
    return RacePovertyOutput(
        white=BaselineReformValue(**values["white"]),
        black=BaselineReformValue(**values["black"]),
        hispanic=BaselineReformValue(**values["hispanic"]),
        other=BaselineReformValue(**values["other"]),
    )


def build_poverty_outputs(country: str, baseline, reform, analysis: Any):
    prefix = "us" if country == "us" else "uk"
    baseline_poverty_by_age = _try_compute_output(
        "baseline poverty by age",
        _poverty_module_function(f"calculate_{prefix}_poverty_by_age"),
        baseline,
    )
    reform_poverty_by_age = _try_compute_output(
        "reform poverty by age",
        _poverty_module_function(f"calculate_{prefix}_poverty_by_age"),
        reform,
    )
    baseline_poverty_by_gender = _try_compute_output(
        "baseline poverty by gender",
        _poverty_module_function(f"calculate_{prefix}_poverty_by_gender"),
        baseline,
    )
    reform_poverty_by_gender = _try_compute_output(
        "reform poverty by gender",
        _poverty_module_function(f"calculate_{prefix}_poverty_by_gender"),
        reform,
    )
    baseline_poverty_by_race = None
    reform_poverty_by_race = None
    if country == "us":
        baseline_poverty_by_race = _try_compute_output(
            "baseline poverty by race",
            _poverty_module_function("calculate_us_poverty_by_race"),
            baseline,
        )
        reform_poverty_by_race = _try_compute_output(
            "reform poverty by race",
            _poverty_module_function("calculate_us_poverty_by_race"),
            reform,
        )
    return PovertyModuleOutputs(
        poverty=build_poverty_output(
            country=country,
            baseline=getattr(analysis, "baseline_poverty", None),
            reform=getattr(analysis, "reform_poverty", None),
            baseline_by_age=baseline_poverty_by_age,
            reform_by_age=reform_poverty_by_age,
        ),
        poverty_by_gender=build_poverty_by_gender_output(
            country=country,
            baseline_by_gender=baseline_poverty_by_gender,
            reform_by_gender=reform_poverty_by_gender,
        ),
        poverty_by_race=(
            build_poverty_by_race_output(
                baseline_by_race=baseline_poverty_by_race,
                reform_by_race=reform_poverty_by_race,
            )
            if country == "us"
            else None
        ),
    )


def build_poverty_output(
    *,
    country: str,
    baseline: Any,
    reform: Any,
    baseline_by_age: Any,
    reform_by_age: Any,
) -> PovertyOutput:
    if isinstance(baseline, PovertyOutput):
        return baseline
    result = {
        "poverty": _empty_age_poverty(),
        "deep_poverty": _empty_age_poverty(),
    }
    _fill_poverty_block(
        country=country,
        output=result,
        baseline_records=_collection_records(baseline),
        reform_records=_collection_records(reform),
        default_group="all",
    )
    _fill_poverty_block(
        country=country,
        output=result,
        baseline_records=_collection_records(baseline_by_age),
        reform_records=_collection_records(reform_by_age),
        default_group="all",
    )
    return PovertyOutput(
        poverty=_age_poverty_output(result["poverty"]),
        deep_poverty=_age_poverty_output(result["deep_poverty"]),
    )


def build_poverty_by_gender_output(
    *,
    country: str,
    baseline_by_gender: Any,
    reform_by_gender: Any,
) -> PovertyByGenderOutput:
    if isinstance(baseline_by_gender, PovertyByGenderOutput):
        return baseline_by_gender
    result = {
        "poverty": _empty_gender_poverty(),
        "deep_poverty": _empty_gender_poverty(),
    }
    _fill_poverty_block(
        country=country,
        output=result,
        baseline_records=_collection_records(baseline_by_gender),
        reform_records=_collection_records(reform_by_gender),
        default_group="all",
    )
    return PovertyByGenderOutput(
        poverty=_gender_poverty_output(result["poverty"]),
        deep_poverty=_gender_poverty_output(result["deep_poverty"]),
    )


def build_poverty_by_race_output(
    *,
    baseline_by_race: Any,
    reform_by_race: Any,
) -> PovertyByRaceOutput:
    if isinstance(baseline_by_race, PovertyByRaceOutput):
        return baseline_by_race
    result = {
        "poverty": {
            "white": _empty_baseline_reform_value(),
            "black": _empty_baseline_reform_value(),
            "hispanic": _empty_baseline_reform_value(),
            "other": _empty_baseline_reform_value(),
        }
    }
    _fill_poverty_block(
        country="us",
        output=result,
        baseline_records=_collection_records(baseline_by_race),
        reform_records=_collection_records(reform_by_race),
        default_group="all",
    )
    return PovertyByRaceOutput(poverty=_race_poverty_output(result["poverty"]))
