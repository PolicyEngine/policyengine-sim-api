"""Where each part of an economy result is stored, and its catalog labels.

``RESULT_FIELD_STORAGE`` names the column (or catalog label) that holds each
field of ``SingleYearMacroOutput``; a test requires it to name every field.
``EconomyResultLabels`` carries what a stored result takes from the region
catalog when it is read: constituency and local-authority names and map
positions, by code. The writer (``economy_results``) and the reader
(``economy_result_reader``) share ``OPTIONAL_SECTIONS`` and ``SIDES``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.result_tables import (
    POVERTY_GROUPS,
    UK_AREA_DISCLOSURE_COLUMNS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


# The sections a result may leave null. Which of them a stored result holds is
# recorded on its report run, so a reader returns null exactly where the
# writer received null, including for empty lists.
OPTIONAL_SECTIONS = (
    "poverty_by_race",
    "wealth_decile",
    "intra_wealth_decile",
    "labor_supply_response",
    "constituency_impact",
    "local_authority_impact",
    "congressional_district_impact",
    "cliff_impact",
)
SIDES = ("baseline", "reform")


# Where each field of SingleYearMacroOutput is kept. "{key}" stands for a map
# key and "[]" for a list item; per-simulation figures are kept on the
# baseline's or the reform's own simulation run, keyed by the report run and
# the side too. Labels come from the catalog
# when the result is read. A test requires this to name every field.
REFUSED_OTHER_KIND = "refused by check_storable: a record of the other kind"
_GEOGRAPHY_FIELDS = (
    "average_household_income_change",
    "relative_household_income_change",
    "population",
    *UK_AREA_DISCLOSURE_COLUMNS,
)


def _poverty_storage() -> dict[str, str]:
    paths = {}
    for section, measures, groups in (
        ("poverty", ("poverty", "deep_poverty"), POVERTY_GROUPS["age"]),
        ("poverty_by_gender", ("poverty", "deep_poverty"), POVERTY_GROUPS["gender"]),
        ("poverty_by_race", ("poverty",), POVERTY_GROUPS["race"]),
    ):
        for measure in measures:
            for group in groups:
                for side in SIDES:
                    paths[f"{section}.{measure}.{group}.{side}"] = "poverty_rates.rate"
    return paths


def _geography_storage(
    section: str, own: str, other: str, table: str
) -> dict[str, str]:
    paths = {f"{section}[].{name}": f"{table}.{name}" for name in _GEOGRAPHY_FIELDS}
    paths[f"{section}[].{own}_code"] = f"{table}.{own}_code"
    paths[f"{section}[].{own}_name"] = "region catalog: name"
    paths[f"{section}[].x"] = "region catalog: map x"
    paths[f"{section}[].y"] = "region catalog: map y"
    paths[f"{section}[].{other}_code"] = REFUSED_OTHER_KIND
    paths[f"{section}[].{other}_name"] = REFUSED_OTHER_KIND
    return paths


RESULT_FIELD_STORAGE: dict[str, str] = {
    "spm_provenance": "report_runs.spm_provenance",
    "model_version": "report_runs.result_model_version",
    "data_version": "report_runs.result_data_version",
    **{
        f"budget.{name}": f"budget_impacts.{name}"
        for name in (
            "budgetary_impact",
            "tax_revenue_impact",
            "state_tax_revenue_impact",
            "benefit_spending_impact",
            "households",
            "baseline_net_income",
        )
    },
    **{
        f"detailed_budget.{{key}}.{name}": f"program_budget_impacts.{name}"
        for name in ("baseline", "reform", "difference")
    },
    **{
        f"{section}.{statistic}.{{key}}": f"decile_impacts.{statistic}_change"
        for section in ("decile", "wealth_decile")
        for statistic in ("average", "relative")
    },
    **{
        path: "intra_decile_impacts.share"
        for section in ("intra_decile", "intra_wealth_decile")
        for path in (f"{section}.deciles.{{key}}[]", f"{section}.all.{{key}}")
    },
    **{
        f"inequality.{name}.{side}": f"inequality.{name}"
        for name in ("gini", "top_10_pct_share", "top_1_pct_share")
        for side in SIDES
    },
    **_poverty_storage(),
    **{
        f"cliff_impact.{side}.{name}": f"cliff_impacts.{name}"
        for side in SIDES
        for name in ("cliff_gap", "cliff_share")
    },
    **{
        f"labor_supply_response.{path}": f"labor_supply_responses.{column}"
        for path, column in (
            ("substitution_lsr", "substitution_lsr"),
            ("income_lsr", "income_lsr"),
            ("relative_lsr.income", "relative_income_response"),
            ("relative_lsr.substitution", "relative_substitution_response"),
            ("total_change", "total_change"),
            ("revenue_change", "revenue_change"),
            ("hours.baseline", "baseline_hours"),
            ("hours.reform", "reform_hours"),
            ("hours.change", "hours_change"),
            ("hours.income_effect", "hours_income_effect"),
            ("hours.substitution_effect", "hours_substitution_effect"),
        )
    },
    **{
        f"labor_supply_response.decile.{statistic}.{effect}.{{key}}": (
            f"labor_supply_decile_impacts.{statistic}_{effect}_effect"
        )
        for statistic in ("average", "relative")
        for effect in ("income", "substitution")
    },
    **{
        f"congressional_district_impact.districts[].{name}": (
            f"congressional_district_impacts.{name}"
        )
        for name in (
            "district",
            "average_household_income_change",
            "relative_household_income_change",
            "winner_percentage",
            "loser_percentage",
            "no_change_percentage",
            "population",
        )
    },
    **_geography_storage(
        "constituency_impact", "constituency", "local_authority", "constituency_impacts"
    ),
    **_geography_storage(
        "local_authority_impact",
        "local_authority",
        "constituency",
        "local_authority_impacts",
    ),
}


@dataclass(frozen=True)
class GeographyLabel:
    """A constituency's or local authority's name and map position."""

    name: str
    x: int | None
    y: int | None


@dataclass(frozen=True)
class EconomyResultLabels:
    """What a stored result takes from the region catalog when it is read.

    Constituency and local-authority names and map positions, by code.
    """

    constituencies: Mapping[str, GeographyLabel] = field(default_factory=dict)
    local_authorities: Mapping[str, GeographyLabel] = field(default_factory=dict)
