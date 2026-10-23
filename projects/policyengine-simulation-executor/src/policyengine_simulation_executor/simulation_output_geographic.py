"""Geographic output segment builders."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from policyengine.data.uk_geography_assets import (
    CONSTITUENCY_ASSET_SPEC,
)
from policyengine.disclosure import UNASSIGNED, DisclosureError
from policyengine.disclosure.areas import normalise_area_codes
from policyengine.disclosure.schema import (
    APPROXIMATE,
    AVERAGE_CHANGE,
    FAMILY_CHANGE,
    FAMILY_GAINING,
    FAMILY_LOSING,
    RECOVERABLE_PARENT,
    RELATIVE_CHANGE,
    REPORTED,
    UNCHECKED_OVERLAP,
)

from policyengine_simulation_executor.simulation_macro_output import (
    CongressionalDistrictImpactOutput,
    CongressionalDistrictImpactRecord,
    GeographicImpactOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _entity_data,
    _number,
    _output_model_dump,
    _try_compute_output,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation
    from policyengine.data.uk_geography_assets import UKGeographyAssetSpec
    from policyengine.disclosure import PublicTable
    from policyengine.outputs.uk_geography_disclosure import UKGeographyDisclosure
    from policyengine_simulation_contract.macro_output import (
        UKGeographyDisclosureTier,
    )
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

logger = logging.getLogger(__name__)


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
    """Require every UK geography record to contain lookup-owned metadata.

    An empty list is valid: disclosure control may withhold every area.
    """
    output = build_geographic_impact_output(value)
    if output is None:
        msg = "UK geography output did not contain a list of result records"
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
    from policyengine.countries.us.data import (
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


# The household output column that holds each published UK level's area code.
UK_DISCLOSURE_AREA_COLUMNS: dict[str, str] = {
    "constituency": "constituency_code_oa",
    "local_authority": "la_code_oa",
}
UK_DISCLOSURE_BREAKDOWN_NAMES: dict[str, str] = {
    "constituency": "constituency",
    "local_authority": "local-authority",
}
# The survey household each row was copied or cloned from. The count floor
# counts support by this id, so copies of one survey household count once.
UK_SOURCE_HOUSEHOLD_ID_COLUMN = "source_household_id"
# Bootstrap replicates behind each 90% interval: the disclosure layer's
# minimum. They set the interval and the rounding, never whether a cell is
# published, and the layer's default of 1,000 would add minutes to every UK run.
UK_DISCLOSURE_REPLICATES = 200
UK_DISCLOSURE_REFORM_LABEL = "reform"
# Each round withholds at least one more cell, so this bounds the check runs.
UK_DISCLOSURE_MAX_ROUNDS = 25
# The output publishes the change family only. The share families are
# withheld before the check, so the check treats them as secret, which they
# are, and never asks for cells to protect shares nobody publishes.
_UNPUBLISHED_FAMILY_OUTCOMES = (FAMILY_GAINING, FAMILY_LOSING)
_FAMILY_OUTCOME = {
    FAMILY_CHANGE: AVERAGE_CHANGE,
    FAMILY_GAINING: FAMILY_GAINING,
    FAMILY_LOSING: FAMILY_LOSING,
}


@dataclass(frozen=True)
class UKAreaIncomeChange:
    """One published UK area's disclosure-controlled household income change."""

    tier: UKGeographyDisclosureTier
    average: float
    average_lower: float
    average_upper: float
    relative: float
    relative_lower: float
    relative_upper: float
    population: float

    def record_fields(self) -> dict[str, object]:
        """Return the fields this change contributes to an area record."""
        return {
            "average_household_income_change": self.average,
            "relative_household_income_change": self.relative,
            "population": self.population,
            "disclosure_tier": self.tier,
            "average_household_income_change_lower": self.average_lower,
            "average_household_income_change_upper": self.average_upper,
            "relative_household_income_change_lower": self.relative_lower,
            "relative_household_income_change_upper": self.relative_upper,
        }


@dataclass(frozen=True)
class UKGeographyDisclosureOutput:
    """The published areas of each UK level, keyed by area code.

    An area the disclosure check withholds is absent.
    """

    areas: Mapping[str, Mapping[str, UKAreaIncomeChange]] = field(default_factory=dict)

    def level(self, level: str) -> Mapping[str, UKAreaIncomeChange]:
        """Return the published areas of one level."""
        return self.areas.get(level, {})


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


def _require_uk_source_household_ids(household: pd.DataFrame) -> None:
    """Refuse UK area outputs when support cannot be counted by survey household."""
    if UK_SOURCE_HOUSEHOLD_ID_COLUMN not in household.columns:
        msg = (
            f"UK constituency and local-authority breakdowns need "
            f"{UK_SOURCE_HOUSEHOLD_ID_COLUMN} on the simulated dataset: disclosure "
            "control counts the survey households behind each area by it."
        )
        raise ValueError(msg)


def _normalised_area_code(value: object) -> str:
    """Return an area code as the disclosure layer keys it."""
    return str(normalise_area_codes([value])[0])


def _uk_area_household_counts(household: pd.DataFrame, column: str) -> dict[str, float]:
    """Return the weighted number of households in each area of one level.

    ``household`` is the weighted output frame, so a grouped count is the sum
    of household weights in each area. Areas without weight are left out, as
    the disclosure layer leaves them out.
    """
    counts = household.groupby(column)["household_net_income"].count()
    by_code: dict[str, float] = {}
    for raw_code, count in counts.items():
        code = _normalised_area_code(raw_code)
        if code == UNASSIGNED:
            continue
        by_code[code] = by_code.get(code, 0.0) + float(cast("float", count))
    return {code: count for code, count in by_code.items() if count > 0}


# For each pair of overlapping published levels (inner, outer): the inner
# areas that lie wholly inside each outer area, as the release reads them.
_UKAreasInside = Mapping[tuple[str, str], Mapping[str, tuple[str, ...]]]


def _uk_areas_inside(household: pd.DataFrame) -> _UKAreasInside:
    """Return, for each level, the areas of the other level wholly inside each area.

    An inner area lies wholly inside an outer one when every weighted
    household of it has that outer area, as the disclosure release decides.
    A containment finding without a suggested cell names only the outer area,
    and these are the cells that resolve it.
    """
    (first, first_column), (second, second_column) = UK_DISCLOSURE_AREA_COLUMNS.items()
    counts = household.groupby([first_column, second_column], dropna=False)[
        "household_net_income"
    ].count()
    pairs: set[tuple[str, str]] = set()
    for key, count in counts.items():
        # Grouping by two columns keys each count by a pair of area codes.
        if not isinstance(key, tuple):
            msg = f"UK area pair counts are keyed by {key!r}, not a pair of codes"
            raise TypeError(msg)
        first_raw, second_raw = key
        if float(cast("float", count)) > 0:
            pairs.add(
                (_normalised_area_code(first_raw), _normalised_area_code(second_raw))
            )
    inside: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}
    for inner_level, outer_level, inner_at, outer_at in (
        (first, second, 0, 1),
        (second, first, 1, 0),
    ):
        outers: dict[str, set[str]] = {}
        for pair in pairs:
            if pair[inner_at] != UNASSIGNED:
                outers.setdefault(pair[inner_at], set()).add(pair[outer_at])
        by_outer: dict[str, list[str]] = {}
        for inner, met in outers.items():
            outer = next(iter(met)) if len(met) == 1 else UNASSIGNED
            if outer != UNASSIGNED:
                by_outer.setdefault(outer, []).append(inner)
        inside[(inner_level, outer_level)] = {
            outer: tuple(sorted(inners)) for outer, inners in by_outer.items()
        }
    return inside


def _withhold_unpublished_families(
    impact: UKGeographyDisclosure,
    area_codes: Mapping[str, Mapping[str, float]],
    *,
    year: int,
) -> None:
    release = impact.release
    for level, codes in area_codes.items():
        for code in codes:
            for outcome in _UNPUBLISHED_FAMILY_OUTCOMES:
                try:
                    release.withhold(
                        level=level,
                        area_code=code,
                        reform=UK_DISCLOSURE_REFORM_LABEL,
                        year=year,
                        outcome=outcome,
                    )
                except DisclosureError:
                    # Not an area of the release, for example one whose
                    # households all have zero weight.
                    continue


def _cells_resolving_findings(
    impact: UKGeographyDisclosure,
    published_levels: tuple[str, ...],
    inside: _UKAreasInside,
) -> set[tuple[str, str, str, str, int]]:
    """Return the published cells to withhold for the blocking findings.

    A finding names a cell to withhold, or else the group it is about. When a
    withheld area is the sum of the public areas of the other level inside
    it, those areas are withheld. Only cells of published levels are
    withheld: regions and the nation are references whose numbers are public
    elsewhere, so withholding them here would only hide the risk.
    """
    cells: set[tuple[str, str, str, str, int]] = set()
    for finding in impact.findings or []:
        if not finding.blocking or finding.family is None:
            continue
        if finding.reform is None or finding.year is None:
            continue
        outcome = _FAMILY_OUTCOME.get(finding.family)
        if outcome is None:
            continue
        key = (outcome, finding.reform, finding.year)
        if finding.suggested and finding.level in published_levels:
            cells.add((finding.level, finding.suggested, *key))
        elif not finding.group or finding.group_level not in published_levels:
            continue
        elif finding.code == RECOVERABLE_PARENT and finding.level in published_levels:
            members = inside.get((finding.level, finding.group_level), {})
            cells.update(
                (finding.level, member, *key)
                for member in members.get(finding.group, ())
            )
        else:
            cells.add((finding.group_level, finding.group, *key))
    return cells


def _resolved_public_table(
    impact: UKGeographyDisclosure,
    area_codes: Mapping[str, Mapping[str, float]],
    *,
    year: int,
    inside: _UKAreasInside,
) -> PublicTable | None:
    """Withhold cells until no finding blocks, and return the public table.

    Returns None when the findings cannot be resolved by withholding published
    cells; then nothing at these levels may be published.
    """
    published_levels = tuple(area_codes)
    _withhold_unpublished_families(impact, area_codes, year=year)
    public = impact.refresh()
    withheld: set[tuple[str, str, str, str, int]] = set()
    for _ in range(UK_DISCLOSURE_MAX_ROUNDS):
        if public is not None:
            return public
        cells = _cells_resolving_findings(impact, published_levels, inside) - withheld
        if not cells:
            break
        for level, code, outcome, reform, cell_year in sorted(cells):
            try:
                impact.release.withhold(
                    level=level,
                    area_code=code,
                    reform=reform,
                    year=cell_year,
                    outcome=outcome,
                )
            except DisclosureError:
                # Not a cell of the release. The finding stays, so the loop
                # ends without progress and nothing is published.
                continue
        withheld |= cells
        public = impact.refresh()
    if public is not None:
        return public
    blocking = sorted(
        {finding.code for finding in impact.findings or [] if finding.blocking}
    )
    # Findings carry area codes; the log names only their kinds.
    logger.warning(
        "UK geography disclosure findings remain after withholding %d cells "
        "(%s), so no constituency or local-authority area is published",
        len(withheld),
        ", ".join(blocking),
    )
    return None


def _published_area_changes(
    public: PublicTable,
    level: str,
    populations: Mapping[str, float],
) -> dict[str, UKAreaIncomeChange]:
    """Return the areas of one level whose average and relative change are public.

    An area is published only when both changes are: the contract has a value
    for each. A relative change is withheld on its own, for example, when the
    area's baseline income is not positive.
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in public.rows:
        if row.level != level or row.outcome not in {AVERAGE_CHANGE, RELATIVE_CHANGE}:
            continue
        if row.tier not in {REPORTED, APPROXIMATE} or row.value is None:
            continue
        if row.lo is None or row.hi is None:
            continue
        rows.setdefault(row.area_code, {})[row.outcome] = row
    changes: dict[str, UKAreaIncomeChange] = {}
    for code, outcomes in rows.items():
        average = outcomes.get(AVERAGE_CHANGE)
        relative = outcomes.get(RELATIVE_CHANGE)
        if average is None or relative is None or code not in populations:
            continue
        tier: UKGeographyDisclosureTier = (
            APPROXIMATE if APPROXIMATE in {average.tier, relative.tier} else REPORTED
        )
        changes[code] = UKAreaIncomeChange(
            tier=tier,
            average=float(average.value),
            average_lower=float(average.lo),
            average_upper=float(average.hi),
            relative=float(relative.value),
            relative_lower=float(relative.lo),
            relative_upper=float(relative.hi),
            population=populations[code],
        )
    return changes


def compute_uk_geography_disclosure_output(
    country: str,
    baseline: Simulation,
    reform: Simulation,
    *,
    year: int,
) -> UKGeographyDisclosureOutput | None:
    """Compute disclosure-controlled constituency and local-authority changes.

    None for other countries. Both levels go through one policyengine release,
    nested in regions and the nation, so the check sees what subtraction
    between them recovers. Raises when the dataset lacks area codes or survey
    household ids.
    """
    if country != "uk":
        return None
    household = _entity_data(baseline, "household")
    _entity_data(reform, "household")
    for level, column in UK_DISCLOSURE_AREA_COLUMNS.items():
        _require_uk_area_column(household, column, UK_DISCLOSURE_BREAKDOWN_NAMES[level])
    _require_uk_source_household_ids(household)

    from policyengine.disclosure import (  # noqa: PLC0415  # bound per call, like this module's other policyengine.outputs imports
        DisclosureSettings,
    )
    from policyengine.outputs.uk_geography_disclosure import (  # noqa: PLC0415  # bound per call, like this module's other policyengine.outputs imports
        compute_uk_geography_disclosure,
    )

    area_codes = {
        level: _uk_area_household_counts(household, column)
        for level, column in UK_DISCLOSURE_AREA_COLUMNS.items()
    }
    impact = compute_uk_geography_disclosure(
        baseline,
        reform,
        year=year,
        levels=tuple(UK_DISCLOSURE_AREA_COLUMNS),
        reform=UK_DISCLOSURE_REFORM_LABEL,
        settings=DisclosureSettings(n_replicates=UK_DISCLOSURE_REPLICATES),
        # Constituencies and local authorities overlap without nesting. The
        # release checks each area against the areas inside and around it;
        # sim-api publishes both levels from every UK run, so it accepts that
        # other differences between them stay unchecked.
        acknowledge=(UNCHECKED_OVERLAP,),
    )
    public = _resolved_public_table(
        impact, area_codes, year=year, inside=_uk_areas_inside(household)
    )
    if public is None:
        return UKGeographyDisclosureOutput()
    return UKGeographyDisclosureOutput(
        areas={
            level: _published_area_changes(public, level, area_codes[level])
            for level in UK_DISCLOSURE_AREA_COLUMNS
        }
    )


def _uk_constituency_lookup(
    lookup_csv_path: str,
) -> dict[str, tuple[str, int | None, int | None]]:
    """Return each constituency's name and map coordinates, in lookup order."""
    lookup = pd.read_csv(lookup_csv_path)
    if "code" not in lookup.columns:
        msg = (
            "UK constituency lookup CSV must contain a 'code' column: "
            f"{lookup_csv_path}"
        )
        raise ValueError(msg)
    entries: dict[str, tuple[str, int | None, int | None]] = {}
    for row in lookup.to_dict("records"):
        code = _normalised_area_code(row.get("code"))
        if not code:
            continue
        name = _normalised_area_code(row.get("name")) or code
        entries[code] = (
            name,
            _lookup_coordinate(row.get("x")),
            _lookup_coordinate(row.get("y")),
        )
    return entries


def _lookup_coordinate(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if math.isfinite(number) else None


def _require_uk_disclosure_output(
    disclosure: UKGeographyDisclosureOutput | None, breakdown: str
) -> UKGeographyDisclosureOutput:
    if disclosure is None:
        msg = (
            f"UK {breakdown} output needs the disclosure-controlled UK geography output"
        )
        raise ValueError(msg)
    return disclosure


def build_uk_constituency_impact(
    country: str, disclosure: UKGeographyDisclosureOutput | None
) -> GeographicImpactOutput | None:
    """Return the published UK parliamentary constituencies; None for other countries.

    Areas that disclosure control withholds are left out. Downloads the
    constituency lookup if needed and raises when a published area lacks a
    lookup name or coordinates.
    """
    if country != "uk":
        return None
    changes = _require_uk_disclosure_output(disclosure, "constituency").level(
        "constituency"
    )
    lookup = _uk_constituency_lookup(
        _required_uk_geography_lookup_csv_path(CONSTITUENCY_ASSET_SPEC)
    )
    ordered = [code for code in lookup if code in changes] + sorted(
        set(changes) - set(lookup)
    )
    records: list[dict[str, object]] = []
    for code in ordered:
        name, x, y = lookup.get(code, (code, None, None))
        records.append(
            {
                "constituency_code": code,
                "constituency_name": name,
                "x": x,
                "y": y,
                **changes[code].record_fields(),
            }
        )
    return _complete_uk_geography_output(
        records,
        code_field="constituency_code",
        name_field="constituency_name",
    )


def build_uk_local_authority_impact(
    country: str,
    disclosure: UKGeographyDisclosureOutput | None,
    *,
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
) -> GeographicImpactOutput | None:
    """Return the published UK local authorities, named and placed for the boundaries.

    None for other countries. For the UK, ``uk_local_authority_metadata`` is
    required, and a code outside its boundary version raises ``ValueError``.
    Areas that disclosure control withholds are left out.
    """
    if country != "uk":
        return None
    changes = _require_uk_disclosure_output(disclosure, "local-authority").level(
        "local_authority"
    )
    if uk_local_authority_metadata is None:
        msg = "UK local-authority boundary metadata is required"
        raise ValueError(msg)

    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module loads only for UK local-authority output
        load_uk_local_authority_resources,
    )

    boundary_version = uk_local_authority_metadata.boundary_version
    display_metadata = load_uk_local_authority_resources().metadata_for(
        boundary_version
    )
    records: list[dict[str, object]] = []
    for code in sorted(changes):
        metadata = display_metadata.get(code)
        if metadata is None:
            msg = (
                f"UK local-authority code {code!r} is not part of the detected "
                f"{boundary_version.value.upper()} boundary version"
            )
            raise ValueError(msg)
        records.append(
            {
                "local_authority_code": code,
                "local_authority_name": metadata.name,
                "x": metadata.x,
                "y": metadata.y,
                **changes[code].record_fields(),
            }
        )
    return _complete_uk_geography_output(
        records,
        code_field="local_authority_code",
        name_field="local_authority_name",
    )
