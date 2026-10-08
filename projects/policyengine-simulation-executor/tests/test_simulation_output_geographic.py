"""Unit tests for required UK geography metadata handling."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd
import pytest
from policyengine.data.uk_geography_assets import CONSTITUENCY_ASSET_SPEC
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)

from policyengine_simulation_executor import simulation_output_geographic


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


def _uk_simulation(
    code: str,
    income: float,
) -> SimpleNamespace:
    household = pd.DataFrame(
        {
            "la_code_oa": [code],
            "household_net_income": [income],
            "household_weight": [2.0],
        }
    )
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )


def test_required_uk_lookup_uses_gcp_credentials(monkeypatch) -> None:
    observed: list[str] = []

    def credentials():
        observed.append("credentials-entered")
        return nullcontext()

    def resolve(spec, **kwargs):
        assert spec is CONSTITUENCY_ASSET_SPEC
        assert kwargs == {"download_missing_assets": True}
        observed.append("lookup-resolved")
        return "/tmp/constituencies_2024.csv"

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

    assert result == "/tmp/constituencies_2024.csv"
    assert observed == ["credentials-entered", "lookup-resolved"]


def test_required_uk_lookup_rejects_missing_asset(monkeypatch) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.setup_gcp_credentials",
        nullcontext,
    )
    monkeypatch.setattr(
        "policyengine.outputs.uk_geography_impact.resolve_uk_geography_lookup_csv_path",
        lambda *args, **kwargs: None,
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


def test_complete_uk_geography_output_rejects_empty_result() -> None:
    with pytest.raises(ValueError, match="did not contain result records"):
        simulation_output_geographic._complete_uk_geography_output(
            [],
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


@pytest.mark.parametrize(
    ("boundary_version", "code", "expected_name"),
    [
        (UKLocalAuthorityBoundaryVersion.LAD22, "E07000026", "Allerdale"),
        (UKLocalAuthorityBoundaryVersion.LAD23, "E06000063", "Cumberland"),
    ],
)
def test_local_authority_output_uses_detected_boundary_metadata(
    monkeypatch,
    boundary_version: UKLocalAuthorityBoundaryVersion,
    code: str,
    expected_name: str,
) -> None:
    monkeypatch.setattr(
        simulation_output_geographic,
        "_required_uk_geography_lookup_csv_path",
        lambda _: pytest.fail("local-authority output must not resolve a GCS lookup"),
    )

    result = simulation_output_geographic.build_uk_local_authority_impact(
        "uk",
        _uk_simulation(code, 100.0),
        _uk_simulation(code, 110.0),
        uk_local_authority_metadata=UKLocalAuthorityMetadata(
            boundary_version=boundary_version
        ),
    )

    assert result is not None
    record = result.root[0].model_dump(mode="python")
    assert record["local_authority_code"] == code
    assert record["local_authority_name"] == expected_name
    assert isinstance(record["x"], int)
    assert isinstance(record["y"], int)
    assert record["average_household_income_change"] == 10.0


def test_local_authority_output_rejects_code_outside_boundary_version() -> None:
    with pytest.raises(
        ValueError, match="not part of the detected LAD22 boundary version"
    ):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk",
            _uk_simulation("E06000063", 100.0),
            _uk_simulation("E06000063", 110.0),
            uk_local_authority_metadata=UKLocalAuthorityMetadata(
                boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
            ),
        )


def test_local_authority_output_requires_detected_boundary_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        simulation_output_geographic,
        "_required_uk_geography_lookup_csv_path",
        lambda _: pytest.fail("local-authority output must not resolve a GCS lookup"),
    )

    with pytest.raises(ValueError, match="boundary metadata is required"):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk",
            _uk_simulation("E06000063", 100.0),
            _uk_simulation("E06000063", 110.0),
        )


def test_uk_geographic_outputs_are_never_dropped_for_missing_area_codes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        simulation_output_geographic,
        "_required_uk_geography_lookup_csv_path",
        lambda _: pytest.fail("a dataset without area codes needs no lookup"),
    )
    household = pd.DataFrame(
        {
            "region": ["LONDON"],
            "household_net_income": [100.0],
            "household_weight": [2.0],
        }
    )
    national = SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )

    with pytest.raises(
        ValueError, match="constituency breakdowns need constituency_code_oa"
    ):
        simulation_output_geographic.build_uk_constituency_impact(
            "uk", national, national
        )
    with pytest.raises(ValueError, match="local-authority breakdowns need la_code_oa"):
        simulation_output_geographic.build_uk_local_authority_impact(
            "uk",
            national,
            national,
            uk_local_authority_metadata=UKLocalAuthorityMetadata(
                boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
            ),
        )
