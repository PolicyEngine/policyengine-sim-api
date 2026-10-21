"""Unit tests for required UK geography metadata handling."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest
from fixtures.weighted_frames import weighted_entity_frame
from policyengine.data.uk_geography_assets import CONSTITUENCY_ASSET_SPEC
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor import simulation_output_geographic

CONSTITUENCY_CSV_PATH = "/tmp/constituencies_2024.csv"


def _complete_constituency_record() -> dict[str, object]:
    return {
        "constituency_code": "E14001063",
        "constituency_name": "Aldershot",
        "x": 56,
        "y": -40,
        "average_household_income_change": -150.95,
        "relative_household_income_change": -0.0026,
        "population": 40_530.65,
    }


def test_required_uk_lookup_uses_gcp_credentials(monkeypatch) -> None:
    observed: list[str] = []

    def credentials():
        observed.append("credentials-entered")
        return nullcontext()

    def resolve(spec, **kwargs):
        assert spec is CONSTITUENCY_ASSET_SPEC
        assert kwargs == {"download_missing_assets": True}
        observed.append("lookup-resolved")
        return CONSTITUENCY_CSV_PATH

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.setup_gcp_credentials",
        credentials,
    )
    monkeypatch.setattr(
        "policyengine.outputs.uk_geography_impact.resolve_uk_geography_lookup_csv_path",
        resolve,
    )

    result = simulation_output_geographic._required_uk_geography_lookup_csv_path(
        CONSTITUENCY_ASSET_SPEC
    )

    assert result == CONSTITUENCY_CSV_PATH
    assert observed == ["credentials-entered", "lookup-resolved"]


def test_required_uk_lookup_rejects_missing_asset(monkeypatch) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.setup_gcp_credentials",
        nullcontext,
    )
    monkeypatch.setattr(
        "policyengine.outputs.uk_geography_impact.resolve_uk_geography_lookup_csv_path",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(FileNotFoundError, match="constituency lookup CSV"):
        simulation_output_geographic._required_uk_geography_lookup_csv_path(
            CONSTITUENCY_ASSET_SPEC
        )


def test_complete_uk_geography_output_accepts_lookup_metadata() -> None:
    result = simulation_output_geographic._complete_uk_geography_output(
        [_complete_constituency_record()],
        code_field="constituency_code",
        name_field="constituency_name",
    )

    record = result.root[0].model_dump(mode="python")
    assert record["constituency_name"] == "Aldershot"
    assert record["x"] == 56
    assert record["y"] == -40


def test_complete_uk_geography_output_accepts_an_empty_list() -> None:
    # Disclosure control may withhold every area of a level.
    result = simulation_output_geographic._complete_uk_geography_output(
        [],
        code_field="constituency_code",
        name_field="constituency_name",
    )

    assert result.root == []


def test_complete_uk_geography_output_rejects_a_missing_list() -> None:
    with pytest.raises(ValueError, match="did not contain a list"):
        simulation_output_geographic._complete_uk_geography_output(
            None,
            code_field="constituency_code",
            name_field="constituency_name",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("constituency_name", "E14001063", "missing a lookup name"),
        ("constituency_name", "", "missing a lookup name"),
        ("x", None, "missing lookup coordinates"),
        ("y", None, "missing lookup coordinates"),
    ],
)
def test_complete_uk_geography_output_rejects_fallback_metadata(
    field: str,
    value: object,
    message: str,
) -> None:
    record = _complete_constituency_record()
    record[field] = value

    with pytest.raises(ValueError, match=message):
        simulation_output_geographic._complete_uk_geography_output(
            [record],
            code_field="constituency_code",
            name_field="constituency_name",
        )


# Disclosure-controlled UK breakdowns. The households below sit in one region
# and two LAD23 local authorities. Each published seat rests on 30 survey
# households, well above the floor of 10; THIN has 3, so it is withheld, and
# the check then withholds one more seat so that subtraction from the region
# or from its local authority cannot recover it. NEGATIVE has a negative total
# baseline income, the Cardiff West case: its relative change is undefined, so
# the seat is left out although its average change passes.
NORTH_WEST = "E12000002"
CUMBERLAND = "E06000063"
WESTMORLAND = "E06000064"
SEATS = {
    "E14000001": (CUMBERLAND, 30),
    "E14000002": (CUMBERLAND, 30),
    "E14000003": (WESTMORLAND, 30),
    "E14000004": (WESTMORLAND, 30),
    "E14000005": (WESTMORLAND, 30),
}
THIN = "E14000006"
NEGATIVE = "E14000007"
LAD23 = UKLocalAuthorityMetadata(boundary_version=UKLocalAuthorityBoundaryVersion.LAD23)


def _uk_disclosure_columns(*, with_source_ids: bool = True) -> dict[str, list]:
    """Return the households' columns, with baseline and reformed incomes."""
    rng = np.random.default_rng(7)
    rows: list[tuple[str, str, float, float]] = []
    for seat, (authority, households) in SEATS.items():
        for _ in range(households):
            income = float(rng.uniform(20_000, 60_000))
            rows.append((seat, authority, income, -float(rng.uniform(50, 150))))
    rows.extend((THIN, WESTMORLAND, 30_000.0, -100.0) for _ in range(3))
    for index in range(30):
        income = -40_000.0 if index < 20 else 30_000.0
        rows.append((NEGATIVE, WESTMORLAND, income, -float(rng.uniform(50, 150))))
    columns: dict[str, list[object]] = {
        "household_id": list(range(len(rows))),
        "constituency_code_oa": [row[0] for row in rows],
        "la_code_oa": [row[1] for row in rows],
        "region_code_oa": [NORTH_WEST] * len(rows),
        "household_weight": [float(rng.uniform(50, 150)) for _ in rows],
    }
    if with_source_ids:
        columns["source_household_id"] = list(range(len(rows)))
    columns["baseline_income"] = [row[2] for row in rows]
    columns["reformed_income"] = [row[2] + row[3] for row in rows]
    return columns


def _uk_disclosure_simulations(
    *, with_source_ids: bool = True
) -> tuple[SimpleNamespace, SimpleNamespace]:
    columns = _uk_disclosure_columns(with_source_ids=with_source_ids)
    baseline_income = columns.pop("baseline_income")
    reformed_income = columns.pop("reformed_income")
    baseline = weighted_entity_frame(
        {**columns, "household_net_income": baseline_income}
    )
    reform = weighted_entity_frame({**columns, "household_net_income": reformed_income})

    def simulation(household):
        return SimpleNamespace(
            output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
        )

    return simulation(baseline), simulation(reform)


@pytest.fixture(scope="module")
def uk_disclosure() -> simulation_output_geographic.UKGeographyDisclosureOutput:
    baseline, reform = _uk_disclosure_simulations()
    output = simulation_output_geographic.compute_uk_geography_disclosure_output(
        "uk", baseline, reform, year=2026
    )
    assert output is not None
    return output


@pytest.fixture
def constituency_lookup(tmp_path, monkeypatch) -> None:
    lookup = tmp_path / "constituencies_2024.csv"
    codes = [*SEATS, THIN, NEGATIVE]
    lookup.write_text(
        "code,name,x,y\n"
        + "".join(
            f"{code},Seat {index},{index},{-index}\n"
            for index, code in enumerate(codes)
        )
    )
    monkeypatch.setattr(
        simulation_output_geographic,
        "_required_uk_geography_lookup_csv_path",
        lambda spec: str(lookup),
    )


def test_uk_disclosure_withholds_thin_and_undefined_seats(uk_disclosure) -> None:
    seats = uk_disclosure.level("constituency")

    assert THIN not in seats
    assert NEGATIVE not in seats
    # Complementary suppression: with THIN withheld, at least one more seat
    # of its region and its local authority is withheld too.
    assert len(seats) < len(SEATS)
    assert set(seats) < set(SEATS)


def test_uk_disclosure_publishes_rounded_changes_with_intervals(
    uk_disclosure,
) -> None:
    seats = uk_disclosure.level("constituency")

    assert seats
    for change in seats.values():
        assert change.tier in {"reported", "approximate"}
        assert change.average_lower <= change.average <= change.average_upper
        assert change.relative_lower <= change.relative <= change.relative_upper
        # Every household loses between 50 and 150 pounds.
        assert -150 <= change.average <= -50
        assert change.relative < 0
        assert change.population > 0


def test_uk_disclosure_population_is_the_weighted_household_count(
    uk_disclosure,
) -> None:
    columns = _uk_disclosure_columns()
    expected: dict[str, float] = {}
    for code, weight in zip(
        columns["constituency_code_oa"], columns["household_weight"], strict=True
    ):
        expected[code] = expected.get(code, 0.0) + weight

    for code, change in uk_disclosure.level("constituency").items():
        assert change.population == pytest.approx(expected[code])


def test_uk_constituency_output_lists_only_published_seats(
    uk_disclosure, constituency_lookup
) -> None:
    result = simulation_output_geographic.build_uk_constituency_impact(
        "uk", uk_disclosure
    )

    assert result is not None
    records = [record.model_dump(mode="json") for record in result.root]
    published = uk_disclosure.level("constituency")
    assert [record["constituency_code"] for record in records] == [
        code for code in SEATS if code in published
    ]
    for record in records:
        change = published[record["constituency_code"]]
        assert record["constituency_name"].startswith("Seat ")
        assert record["average_household_income_change"] == change.average
        assert record["relative_household_income_change"] == change.relative
        assert record["disclosure_tier"] == change.tier
        assert record["average_household_income_change_lower"] == (change.average_lower)
        assert record["relative_household_income_change_upper"] == (
            change.relative_upper
        )


def test_uk_local_authority_output_uses_detected_boundary_metadata(
    uk_disclosure, monkeypatch
) -> None:
    monkeypatch.setattr(
        simulation_output_geographic,
        "_required_uk_geography_lookup_csv_path",
        lambda _: pytest.fail("local-authority output must not resolve a GCS lookup"),
    )

    result = simulation_output_geographic.build_uk_local_authority_impact(
        "uk", uk_disclosure, uk_local_authority_metadata=LAD23
    )

    assert result is not None
    records = {
        record.model_dump(mode="json")["local_authority_code"]: record.model_dump(
            mode="json"
        )
        for record in result.root
    }
    assert set(records) == set(uk_disclosure.level("local_authority"))
    assert records
    for code, record in records.items():
        assert record["local_authority_name"] in {
            "Cumberland",
            "Westmorland and Furness",
        }
        assert isinstance(record["x"], int)
        assert isinstance(record["y"], int)
        assert record["average_household_income_change"] < 0
        assert record["population"] == (
            uk_disclosure.level("local_authority")[code].population
        )


def test_uk_local_authority_output_rejects_code_outside_boundary_version(
    uk_disclosure,
) -> None:
    with pytest.raises(
        ValueError, match="not part of the detected LAD22 boundary version"
    ):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk",
            uk_disclosure,
            uk_local_authority_metadata=UKLocalAuthorityMetadata(
                boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
            ),
        )


def test_uk_local_authority_output_requires_detected_boundary_metadata(
    uk_disclosure,
) -> None:
    with pytest.raises(ValueError, match="boundary metadata is required"):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk", uk_disclosure
        )


def test_uk_outputs_require_the_disclosure_output() -> None:
    with pytest.raises(ValueError, match="disclosure-controlled"):
        simulation_output_geographic.build_uk_constituency_impact("uk", None)
    with pytest.raises(ValueError, match="disclosure-controlled"):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk", None, uk_local_authority_metadata=LAD23
        )


def test_uk_outputs_are_none_for_other_countries() -> None:
    baseline, reform = _uk_disclosure_simulations()

    assert (
        simulation_output_geographic.compute_uk_geography_disclosure_output(
            "us", baseline, reform, year=2026
        )
        is None
    )
    assert simulation_output_geographic.build_uk_constituency_impact("us", None) is None
    assert (
        simulation_output_geographic.build_uk_local_authority_impact("us", None) is None
    )


def test_uk_outputs_list_no_area_when_every_area_is_withheld(
    constituency_lookup,
) -> None:
    empty = simulation_output_geographic.UKGeographyDisclosureOutput()

    constituencies = simulation_output_geographic.build_uk_constituency_impact(
        "uk", empty
    )
    authorities = simulation_output_geographic.build_uk_local_authority_impact(
        "uk", empty, uk_local_authority_metadata=LAD23
    )

    assert constituencies is not None
    assert constituencies.root == []
    assert authorities is not None
    assert authorities.root == []


def test_uk_disclosure_requires_survey_household_ids() -> None:
    baseline, reform = _uk_disclosure_simulations(with_source_ids=False)

    with pytest.raises(ValueError, match="need source_household_id"):
        simulation_output_geographic.compute_uk_geography_disclosure_output(
            "uk", baseline, reform, year=2026
        )


def test_uk_geographic_outputs_are_never_dropped_for_missing_area_codes(
    monkeypatch,
) -> None:
    household = weighted_entity_frame(
        {
            "region": ["LONDON", "LONDON"],
            "source_household_id": [1, 2],
            "household_net_income": [100.0, 200.0],
            "household_weight": [2.0, 3.0],
        }
    )
    national = SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )

    with pytest.raises(
        ValueError, match="constituency breakdowns need constituency_code_oa"
    ):
        simulation_output_geographic.compute_uk_geography_disclosure_output(
            "uk", national, national, year=2026
        )


def _finding(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "blocking": True,
            "code": "recoverable_remainder",
            "family": "change",
            "reform": "reform",
            "year": 2026,
            "level": "constituency",
            "suggested": None,
            "group": None,
            "group_level": None,
            **fields,
        }
    )


class _FakeRelease:
    def __init__(self) -> None:
        self.withheld: list[tuple[str, str, str]] = []

    def withhold(self, *, level, area_code, reform, year, outcome) -> None:
        self.withheld.append((level, area_code, outcome))


class _FakeImpact:
    """A disclosure result whose findings clear once the named cells are withheld."""

    def __init__(self, findings, *, clears_after: set[tuple[str, str]]) -> None:
        self.release = _FakeRelease()
        self.findings = findings
        self._clears_after = clears_after
        self.public = None

    def refresh(self):
        withheld = {
            (level, code)
            for level, code, outcome in self.release.withheld
            if outcome == "average_change"
        }
        if self._clears_after <= withheld:
            self.findings = []
            self.public = SimpleNamespace(rows=[])
        return self.public


AREAS = {"constituency": {"S1": 1.0, "S2": 1.0}, "local_authority": {"L1": 2.0}}


def test_findings_are_resolved_by_withholding_published_cells() -> None:
    impact = _FakeImpact(
        [
            _finding(suggested="S2"),
            # A containment finding without a suggestion names its group.
            _finding(level="constituency", group="L1", group_level="local_authority"),
        ],
        clears_after={("constituency", "S2"), ("local_authority", "L1")},
    )

    public = simulation_output_geographic._resolved_public_table(
        impact, AREAS, year=2026
    )

    assert public is not None
    change_cells = [
        cell for cell in impact.release.withheld if cell[2] == "average_change"
    ]
    assert sorted(change_cells) == [
        ("constituency", "S2", "average_change"),
        ("local_authority", "L1", "average_change"),
    ]
    # The shares are never published, so every area's are withheld up front.
    share_cells = {
        cell for cell in impact.release.withheld if cell[2] != "average_change"
    }
    assert share_cells == {
        (level, code, outcome)
        for level, codes in AREAS.items()
        for code in codes
        for outcome in ("share_gaining", "share_losing")
    }


def test_reference_cells_are_never_withheld_and_publish_nothing(caplog) -> None:
    # A finding that only a region cell could resolve: regions are public
    # elsewhere, so the breakdowns publish no area at all.
    impact = _FakeImpact(
        [_finding(code="recoverable_parent", level="region", group="E12000002")],
        clears_after={("region", "E12000002")},
    )

    with caplog.at_level("WARNING"):
        public = simulation_output_geographic._resolved_public_table(
            impact, AREAS, year=2026
        )

    assert public is None
    assert all(level != "region" for level, _, _ in impact.release.withheld)
    assert "recoverable_parent" in caplog.text
    assert "E12000002" not in caplog.text
